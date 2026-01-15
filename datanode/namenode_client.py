"""
Cliente para registrar DataNode con el MetaNameNode usando DNS de Docker.
Maneja el registro automático y envío de heartbeats del DataNode.
Obtiene la URL del MetaNameNode líder directamente via DNS.
"""
import requests
import threading
import time
import os
import socket
from typing import Optional, List
import sys
from pathlib import Path

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import generate_service_token

# Configuración - usar DNS de Docker en lugar del Registry
NAMENODE_SERVICE = os.getenv("NAMENODE_SERVICE", "namenode")
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
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
            ip = socket.gethostname()
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


def discover_namenodes_dns() -> List[str]:
    """
    Descubre namenodes usando resolución DNS de Docker.
    
    Returns:
        Lista de URLs de namenodes descubiertos
    """
    try:
        addr_info = socket.getaddrinfo(
            NAMENODE_SERVICE, 
            NAMENODE_PORT,
            proto=socket.IPPROTO_TCP
        )
        
        ips = set()
        for info in addr_info:
            ip = info[4][0]
            ips.add(ip)
        
        urls = [f"http://{ip}:{NAMENODE_PORT}" for ip in ips]
        
        if urls:
            print(f"[NAMENODE_CLIENT] DNS descubrió {len(urls)} namenodes")
        
        return urls
        
    except socket.gaierror as e:
        print(f"[NAMENODE_CLIENT] Error DNS resolviendo '{NAMENODE_SERVICE}': {e}")
        return []
    except Exception as e:
        print(f"[NAMENODE_CLIENT] Error inesperado en descubrimiento DNS: {e}")
        return []


def get_namenode_leader_url() -> Optional[str]:
    """
    Obtiene la URL del MetaNameNode líder usando DNS de Docker.
    
    Returns:
        URL del MetaNameNode líder, o None si no se puede obtener
    """
    namenodes = discover_namenodes_dns()
    
    if not namenodes:
        print(f"[NAMENODE_CLIENT] No se encontraron namenodes via DNS")
        return None
    
    datanode_id = generate_datanode_id()
    
    for namenode_url in namenodes:
        try:
            print(f"[NAMENODE_CLIENT] Consultando namenode: {namenode_url}")
            response = requests.get(f"{namenode_url}/", timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Si este namenode es el líder
            if data.get("is_leader"):
                print(f"[NAMENODE_CLIENT] ✅ Líder encontrado: {namenode_url}")
                return namenode_url
            
            # Si conoce al líder
            leader_url = data.get("leader_url")
            if leader_url:
                try:
                    leader_response = requests.get(f"{leader_url}/", timeout=5)
                    leader_response.raise_for_status()
                    leader_data = leader_response.json()
                    if leader_data.get("is_leader"):
                        print(f"[NAMENODE_CLIENT] ✅ Líder verificado: {leader_url}")
                        return leader_url
                except requests.RequestException:
                    continue
            
            # Si hay leader_id, construir URL
            leader_id = data.get("leader_id")
            if leader_id:
                if leader_id.startswith("namenode-"):
                    constructed_url = f"http://tbfs-{leader_id}:{NAMENODE_PORT}"
                elif leader_id.startswith("tbfs-"):
                    constructed_url = f"http://{leader_id}:{NAMENODE_PORT}"
                else:
                    constructed_url = f"http://{leader_id}:{NAMENODE_PORT}"
                
                try:
                    verify_response = requests.get(f"{constructed_url}/", timeout=5)
                    verify_response.raise_for_status()
                    verify_data = verify_response.json()
                    if verify_data.get("is_leader"):
                        print(f"[NAMENODE_CLIENT] ✅ Líder construido y verificado: {constructed_url}")
                        return constructed_url
                except requests.RequestException:
                    continue
                    
        except requests.RequestException as e:
            print(f"[NAMENODE_CLIENT] Error consultando {namenode_url}: {e}")
            continue
    
    return None


class DataNodeNameNodeClient:
    """Cliente para registrar el DataNode con el MetaNameNode usando DNS"""
    
    def __init__(self):
        self.datanode_id = generate_datanode_id()
        print(f"[NAMENODE_CLIENT] DataNode ID generado: {self.datanode_id}")
        self.datanode_port = DATANODE_PORT
        self.datanode_url = os.getenv("NODE_ID", get_hostname())
        self.datanode_ip = get_server_ip()
        self.heartbeat_thread = None
        self.running = False
        self.registered = False
        self.namenode_url = None
    
    def _get_service_token(self) -> str:
        """Obtiene un token de servicio para autenticación"""
        try:
            return generate_service_token(self.datanode_id, "service")
        except Exception as e:
            print(f"[NAMENODE_CLIENT] Error generando token de servicio: {e}")
            return os.getenv("DATANODE_SERVICE_TOKEN", "datanode-service-token")
    
    def register(self) -> bool:
        """
        Registra el DataNode con el MetaNameNode líder.
        
        Returns:
            True si se registró correctamente, False en caso de error
        """
        try:
            # Obtener URL del MetaNameNode líder via DNS
            self.namenode_url = get_namenode_leader_url()
            if not self.namenode_url:
                print(f"[NAMENODE_CLIENT] No se pudo obtener URL del MetaNameNode líder")
                return False
            
            print(f"[NAMENODE_CLIENT] MetaNameNode líder encontrado: {self.namenode_url}")
            
            # Obtener información de almacenamiento
            from datanode.storage import get_storage_info
            storage_info = get_storage_info()
            
            # Registrar con el MetaNameNode
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
                timeout=20
            )
            response.raise_for_status()
            
            self.registered = True
            ip_info = f" (IP: {self.datanode_ip})" if self.datanode_ip else ""
            print(f"[NAMENODE_CLIENT] DataNode registrado: {self.datanode_id} -> {self.datanode_url}:{self.datanode_port}{ip_info}")
            return True
            
        except requests.RequestException as e:
            print(f"[NAMENODE_CLIENT] Error al registrar DataNode: {e}")
            return False
    
    def send_heartbeat(self) -> bool:
        """
        Envía un heartbeat al MetaNameNode con información actualizada.
        
        Returns:
            True si se envió correctamente, False en caso de error
        """
        if not self.namenode_url:
            self.namenode_url = get_namenode_leader_url()
            if not self.namenode_url:
                return False
        
        try:
            from datanode.storage import get_storage_info
            storage_info = get_storage_info()
            
            token = self._get_service_token()
            url = f"{self.namenode_url}/datanodes/{self.datanode_id}/heartbeat"
            print(f"[NAMENODE_CLIENT] Enviando heartbeat: datanode_id={self.datanode_id}")
            
            response = requests.post(
                url,
                json={
                    "free_space": storage_info["free_space"],
                    "total_space": storage_info["total_space"]
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
            
            response.raise_for_status()
            print(f"[NAMENODE_CLIENT] ✓ Heartbeat enviado exitosamente")
            return True
            
        except requests.RequestException as e:
            print(f"[NAMENODE_CLIENT] ❌ Error enviando heartbeat: {e}")
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
                    self.register()
                time.sleep(HEARTBEAT_INTERVAL)
        
        self.heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        print(f"[NAMENODE_CLIENT] Hilo de heartbeats iniciado (intervalo: {HEARTBEAT_INTERVAL}s)")
    
    def stop_heartbeat(self):
        """Detiene el hilo de heartbeats"""
        self.running = False
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        print(f"[NAMENODE_CLIENT] Hilo de heartbeats detenido")


# Instancia global del cliente (con mismo nombre para compatibilidad)
registry_client = DataNodeNameNodeClient()
