"""
Cliente para el Registry Service - MetaNameNode
Maneja el registro automático y envío de heartbeats del MetaNameNode
Soporta múltiples nodos del registry con failover automático
"""
import requests
import threading
import time
import os
import socket
import uuid
import random

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:9000")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))  # segundos
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
NAMENODE_ID = os.getenv("NODE_ID", None)  # Usar el mismo NODE_ID del namenode


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


def generate_server_id():
    """Genera un ID único para el namenode"""
    if NAMENODE_ID:
        return NAMENODE_ID
    hostname = get_hostname()
    unique_id = str(uuid.uuid4())[:8]
    return f"{hostname}-{unique_id}"


class RegistryClient:
    def __init__(self, registry_url: str = REGISTRY_URL):
        self.server_id = generate_server_id()
        # Parsear múltiples URLs del registry (separadas por comas)
        if isinstance(registry_url, str):
            self.registry_urls = [url.strip() for url in registry_url.split(",") if url.strip()]
        else:
            self.registry_urls = [registry_url]
        self.server_port = NAMENODE_PORT
        # Usar el NODE_ID para construir el nombre del servicio Docker
        # Si NODE_ID es "namenode-1", el nombre del servicio será "tbfs-namenode-1"
        if NAMENODE_ID:
            self.server_url = f"tbfs-{NAMENODE_ID}"  # tbfs-namenode-1, tbfs-namenode-2, etc.
        else:
            self.server_url = get_hostname()  # Fallback al hostname
        self.server_ip = get_server_ip()
        self.heartbeat_thread = None
        self.running = False
        self.registered = False
        self._last_error_time = {}
        self._error_cooldown = 60
    
    def _try_registry_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Intenta hacer una petición a cualquiera de los nodos del registry disponibles"""
        urls = self.registry_urls.copy()
        random.shuffle(urls)
        
        last_error = None
        successful_url = None
        failed_urls = []
        
        for registry_url in urls:
            try:
                url = f"{registry_url}{endpoint}"
                response = requests.request(method, url, timeout=5, **kwargs)
                response.raise_for_status()
                successful_url = registry_url
                if registry_url in self._last_error_time:
                    del self._last_error_time[registry_url]
                return response
            except requests.RequestException as e:
                last_error = e
                failed_urls.append(registry_url)
                current_time = time.time()
                last_error_time = self._last_error_time.get(registry_url, 0)
                if current_time - last_error_time > self._error_cooldown:
                    self._last_error_time[registry_url] = current_time
                    if len(failed_urls) == len(urls):
                        print(f"[REGISTRY_CLIENT] Error con nodo {registry_url}: {e}")
                continue
        
        if successful_url is None:
            current_time = time.time()
            last_general_error = self._last_error_time.get("_general", 0)
            if current_time - last_general_error > self._error_cooldown:
                self._last_error_time["_general"] = current_time
                print(f"[REGISTRY_CLIENT] Todos los nodos del registry fallaron ({len(failed_urls)}/{len(urls)} nodos)")
        
        raise last_error or requests.RequestException("Todos los nodos del registry fallaron")
    
    def register(self):
        """Registra el namenode en el registry"""
        try:
            response = self._try_registry_request(
                "post",
                "/register",
                json={
                    "server_id": self.server_id,
                    "url": self.server_url,
                    "port": self.server_port,
                    "ip": self.server_ip
                }
            )
            self.registered = True
            ip_info = f" (IP: {self.server_ip})" if self.server_ip else ""
            print(f"[REGISTRY_CLIENT] MetaNameNode registrado: {self.server_id} -> {self.server_url}:{self.server_port}{ip_info}")
            return True
        except requests.RequestException as e:
            print(f"[REGISTRY_CLIENT] Error al registrar MetaNameNode: {e}")
            return False
    
    def send_heartbeat(self):
        """Envía un heartbeat al registry"""
        try:
            self._try_registry_request(
                "post",
                "/heartbeat",
                json={"server_id": self.server_id}
            )
            return True
        except requests.RequestException as e:
            current_time = time.time()
            last_heartbeat_error = self._last_error_time.get("_heartbeat", 0)
            if current_time - last_heartbeat_error > self._error_cooldown:
                self._last_error_time["_heartbeat"] = current_time
                print(f"[REGISTRY_CLIENT] Error al enviar heartbeat: {e}")
            if self.registered:
                self.registered = False
                if current_time - last_heartbeat_error > self._error_cooldown:
                    print(f"[REGISTRY_CLIENT] Intentando re-registrar MetaNameNode...")
                self.register()
            return False
    
    def _heartbeat_loop(self):
        """Loop que envía heartbeats periódicamente"""
        time.sleep(2)
        
        while self.running:
            if self.registered:
                self.send_heartbeat()
            else:
                self.register()
            
            time.sleep(HEARTBEAT_INTERVAL)
    
    def start(self):
        """Inicia el cliente del registry (registro + heartbeats)"""
        if self.running:
            return
        
        print(f"[REGISTRY_CLIENT] Iniciando cliente del registry para MetaNameNode...")
        print(f"[REGISTRY_CLIENT] Registry URLs: {self.registry_urls}")
        print(f"[REGISTRY_CLIENT] MetaNameNode ID: {self.server_id}")
        
        self.register()
        
        self.running = True
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        print(f"[REGISTRY_CLIENT] Cliente iniciado. Heartbeat cada {HEARTBEAT_INTERVAL}s")
    
    def stop(self):
        """Detiene el cliente del registry"""
        self.running = False
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        print(f"[REGISTRY_CLIENT] Cliente detenido")


# Instancia global del cliente
registry_client = RegistryClient()

