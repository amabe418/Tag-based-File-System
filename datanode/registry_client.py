"""
Cliente para registrar DataNode con el MetaNameNode
Maneja el registro automático y envío de heartbeats del DataNode
Obtiene la URL del MetaNameNode líder desde el Registry
"""
import requests
import threading
import time
import os
import socket
from typing import Optional
import sys
from pathlib import Path

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import generate_service_token

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:9000")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))  # segundos
DATANODE_PORT = int(os.getenv("DATANODE_PORT", "8001"))
DATANODE_ID = os.getenv("DATANODE_ID", None)  # ID del datanode


def get_hostname():
    """Obtiene el hostname del contenedor/servidor"""
    try:
        return socket.gethostname()
    except:
        return "localhost"


def get_server_ip():
    """Obtiene la IP del servidor"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = socket.gethostname()  # Usar hostname si no se puede obtener IP externa
        finally:
            s.close()
        return ip
    except Exception:
        return None


def generate_datanode_id():
    """Genera un ID único para el datanode"""
    if DATANODE_ID:
        return DATANODE_ID
    hostname = get_hostname()
    return f"{hostname}-datanode"


def get_namenode_leader_url() -> Optional[str]:
    """
    Obtiene la URL del MetaNameNode líder consultando el Registry.
    
    Returns:
        URL del MetaNameNode líder, o None si no se puede obtener
    """
    registry_urls = [url.strip() for url in REGISTRY_URL.split(",") if url.strip()]
    
    datanode_id = generate_datanode_id()
    for registry_url in registry_urls:
        try:
            # Obtener lista de servidores activos del registry
            # Usar autenticación de servicio para esta petición
            try:
                token = generate_service_token(datanode_id, "service")
            except Exception as e:
                print(f"[REGISTRY_CLIENT] Error generando token para consulta registry: {e}")
                token = os.getenv("DATANODE_SERVICE_TOKEN", "datanode-service-token")
            
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{registry_url}/servers/active"
            print(f"[REGISTRY_CLIENT] Consultando registry: {url}")
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 401 or response.status_code == 403:
                print(f"[REGISTRY_CLIENT] ❌ Error de autenticación consultando registry {registry_url}/servers/active: status={response.status_code}, response={response.text}")
            
            response.raise_for_status()
            servers = response.json()
            print(f"[REGISTRY_CLIENT] ✓ Servidores obtenidos del registry: {len(servers)} servidores")
            
            # Buscar un MetaNameNode activo (server_id contiene "namenode")
            for server in servers:
                server_id = server.get("server_id", "").lower()
                if "namenode" in server_id:
                    server_url = server.get("url", "")
                    
                    # Construir URL completa (el url ya debería incluir el puerto o usar el default)
                    if not server_url.startswith("http"):
                        # Si no tiene http, construir con el puerto conocido del namenode
                        server_url = f"http://{server_url}:8010"
                    
                    # Consultar el endpoint / del namenode para obtener el líder
                    try:
                        print(f"[REGISTRY_CLIENT] Consultando namenode: {server_url}/")
                        namenode_response = requests.get(f"{server_url}/", timeout=5)
                        
                        if namenode_response.status_code == 401 or namenode_response.status_code == 403:
                            print(f"[REGISTRY_CLIENT] ❌ Error de autenticación consultando namenode {server_url}/: status={namenode_response.status_code}, response={namenode_response.text}")
                        
                        namenode_response.raise_for_status()
                        namenode_data = namenode_response.json()
                        
                        # Si este namenode es el líder, usar su URL
                        if namenode_data.get("is_leader"):
                            return server_url
                        
                        # Si no es líder, obtener leader_url
                        leader_url = namenode_data.get("leader_url")
                        if leader_url:
                            return leader_url
                    except Exception as e:
                        print(f"[REGISTRY_CLIENT] Error consultando namenode {server_url}: {e}")
                        continue  # Intentar con otro namenode
            
        except requests.RequestException as e:
            print(f"[REGISTRY_CLIENT] Error consultando registry {registry_url}: {e}")
            continue
    
    return None


class DataNodeRegistryClient:
    """Cliente para registrar el DataNode con el MetaNameNode"""
    
    def __init__(self, registry_url: str = REGISTRY_URL):
        self.datanode_id = generate_datanode_id()
        print(f"[REGISTRY_CLIENT] DataNode ID generado: {self.datanode_id}")
        self.registry_urls = [url.strip() for url in registry_url.split(",") if url.strip()]
        self.datanode_port = DATANODE_PORT
        self.datanode_url = os.getenv("NODE_ID", get_hostname())  # Nombre del servicio Docker
        self.datanode_ip = get_server_ip()
        self.heartbeat_thread = None
        self.running = False
        self.registered = False
        self.namenode_url = None
        self._last_error_time = {}
        self._error_cooldown = 60
    
    def _get_service_token(self) -> str:
        """Obtiene un token de servicio para autenticación"""
        try:
            return generate_service_token(self.datanode_id, "service")
        except Exception as e:
            print(f"[REGISTRY_CLIENT] Error generando token de servicio: {e}")
            # Fallback: usar token pre-compartido si está disponible
            return os.getenv("DATANODE_SERVICE_TOKEN", "datanode-service-token")
    
    def _try_registry_request(self, method: str, endpoint: str, **kwargs):
        """Intenta hacer una petición a cualquiera de los registries disponibles"""
        # Agregar token de servicio a los headers
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        kwargs["headers"]["Authorization"] = f"Bearer {self._get_service_token()}"
        
        for registry_url in self.registry_urls:
            try:
                url = f"{registry_url}{endpoint}"
                response = requests.request(method, url, timeout=5, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                print(f"[REGISTRY_CLIENT] Error con registry {registry_url}: {e}")
                continue
        raise requests.RequestException("No se pudo conectar con ningún registry")
    
    def register(self) -> bool:
        """
        Registra el DataNode con el MetaNameNode.
        Primero obtiene la URL del líder desde el Registry, luego se registra.
        
        Returns:
            True si se registró correctamente, False en caso de error
        """
        try:
            # Obtener URL del MetaNameNode líder
            self.namenode_url = get_namenode_leader_url()
            if not self.namenode_url:
                print(f"[REGISTRY_CLIENT] No se pudo obtener URL del MetaNameNode líder")
                return False
            
            print(f"[REGISTRY_CLIENT] MetaNameNode líder encontrado: {self.namenode_url}")
            
            # Obtener información de almacenamiento
            from datanode.storage import get_storage_info
            storage_info = get_storage_info()
            
            # Registrar con el MetaNameNode (con token de servicio)
            token = self._get_service_token()
            response = requests.post(
                f"{self.namenode_url}/datanodes/register",
                json={
                    "node_id": self.datanode_id,
                    "url": f"http://{self.datanode_url}:{self.datanode_port}",
                    "port": self.datanode_port,
                    "ip": self.datanode_ip,
                    "total_space": storage_info["total_space"],
                    "free_space": storage_info["free_space"]
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            response.raise_for_status()
            
            self.registered = True
            ip_info = f" (IP: {self.datanode_ip})" if self.datanode_ip else ""
            print(f"[REGISTRY_CLIENT] DataNode registrado: {self.datanode_id} -> {self.datanode_url}:{self.datanode_port}{ip_info}")
            return True
            
        except requests.RequestException as e:
            print(f"[REGISTRY_CLIENT] Error al registrar DataNode: {e}")
            return False
    
    def send_heartbeat(self) -> bool:
        """
        Envía un heartbeat al MetaNameNode con información actualizada.
        
        Returns:
            True si se envió correctamente, False en caso de error
        """
        if not self.namenode_url:
            # Intentar obtener la URL del líder nuevamente
            self.namenode_url = get_namenode_leader_url()
            if not self.namenode_url:
                return False
        
        try:
            # Obtener información de almacenamiento actualizada
            from datanode.storage import get_storage_info
            storage_info = get_storage_info()
            
            # Enviar heartbeat con token de servicio
            token = self._get_service_token()
            url = f"{self.namenode_url}/datanodes/{self.datanode_id}/heartbeat"
            print(f"[REGISTRY_CLIENT] Enviando heartbeat: datanode_id={self.datanode_id}, url={url}")
            
            response = requests.post(
                url,
                json={
                    "free_space": storage_info["free_space"],
                    "total_space": storage_info["total_space"]
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=5
            )
            
            if response.status_code == 401 or response.status_code == 403:
                print(f"[REGISTRY_CLIENT] ❌ Error de autenticación en heartbeat: status={response.status_code}, response={response.text}")
            
            response.raise_for_status()
            print(f"[REGISTRY_CLIENT] ✓ Heartbeat enviado exitosamente")
            return True
            
        except requests.RequestException as e:
            status_code = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
            error_detail = getattr(e.response, 'text', str(e)) if hasattr(e, 'response') else str(e)
            print(f"[REGISTRY_CLIENT] ❌ Error enviando heartbeat: status={status_code}, error={error_detail}, datanode_id={self.datanode_id}")
            # Si falla, intentar re-registrarse
            self.registered = False
            self.namenode_url = None
            return False
    
    def start_heartbeat(self):
        """Inicia el hilo de heartbeats periódicos"""
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            return
        
        self.running = True
        
        def heartbeat_loop():
            while self.running:
                if self.registered:
                    self.send_heartbeat()
                else:
                    # Intentar registrar si no está registrado
                    self.register()
                time.sleep(HEARTBEAT_INTERVAL)
        
        self.heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        print(f"[REGISTRY_CLIENT] Hilo de heartbeats iniciado (intervalo: {HEARTBEAT_INTERVAL}s)")
    
    def stop_heartbeat(self):
        """Detiene el hilo de heartbeats"""
        self.running = False
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        print(f"[REGISTRY_CLIENT] Hilo de heartbeats detenido")


# Instancia global del cliente
registry_client = DataNodeRegistryClient()

