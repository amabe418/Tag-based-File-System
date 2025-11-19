"""
Cliente para el Registry Service
Maneja el registro automático y envío de heartbeats
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
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))
SERVER_ID = os.getenv("SERVER_ID", None)  # Si no se proporciona, se genera automáticamente


def get_hostname():
    """Obtiene el hostname del contenedor/servidor"""
    try:
        return socket.gethostname()
    except:
        return "localhost"


def get_server_ip():
    """Obtiene la IP del servidor"""
    try:
        # Intentar obtener la IP de la interfaz de red principal
        # Primero intentar conectarse a un servidor externo para obtener la IP local
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No necesita conectarse realmente, solo configura la conexión
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        except Exception:
            # Si falla, intentar obtener la IP del hostname
            ip = socket.gethostbyname(get_hostname())
        finally:
            s.close()
        return ip
    except Exception:
        # Si todo falla, retornar None
        return None


def generate_server_id():
    """Genera un ID único para el servidor"""
    if SERVER_ID:
        return SERVER_ID
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
        self.server_port = SERVER_PORT
        self.server_url = get_hostname()
        self.server_ip = get_server_ip()  # Obtener IP del servidor
        self.heartbeat_thread = None
        self.running = False
        self.registered = False
    
    def _try_registry_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Intenta hacer una petición a cualquiera de los nodos del registry disponibles"""
        # Mezclar URLs para distribuir carga
        urls = self.registry_urls.copy()
        random.shuffle(urls)
        
        last_error = None
        for registry_url in urls:
            try:
                url = f"{registry_url}{endpoint}"
                response = requests.request(method, url, timeout=5, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                last_error = e
                print(f"[REGISTRY_CLIENT] Error con nodo {registry_url}: {e}")
                continue
        
        # Si todos fallaron, lanzar el último error
        raise last_error or requests.RequestException("Todos los nodos del registry fallaron")
    
    def register(self):
        """Registra el servidor en el registry"""
        try:
            response = self._try_registry_request(
                "post",
                "/register",
                json={
                    "server_id": self.server_id,
                    "url": self.server_url,
                    "port": self.server_port,
                    "ip": self.server_ip  # Incluir IP como alternativa de acceso
                }
            )
            self.registered = True
            ip_info = f" (IP: {self.server_ip})" if self.server_ip else ""
            print(f"[REGISTRY_CLIENT] Servidor registrado: {self.server_id} -> {self.server_url}:{self.server_port}{ip_info}")
            return True
        except requests.RequestException as e:
            print(f"[REGISTRY_CLIENT] Error al registrar servidor: {e}")
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
            print(f"[REGISTRY_CLIENT] Error al enviar heartbeat: {e}")
            # Intentar re-registrarse si falla
            if self.registered:
                self.registered = False
                print(f"[REGISTRY_CLIENT] Intentando re-registrar servidor...")
                self.register()
            return False
    
    def _heartbeat_loop(self):
        """Loop que envía heartbeats periódicamente"""
        # Esperar un poco antes del primer heartbeat
        time.sleep(2)
        
        while self.running:
            if self.registered:
                self.send_heartbeat()
            else:
                # Intentar registrarse si no está registrado
                self.register()
            
            time.sleep(HEARTBEAT_INTERVAL)
    
    def start(self):
        """Inicia el cliente del registry (registro + heartbeats)"""
        if self.running:
            return
        
        print(f"[REGISTRY_CLIENT] Iniciando cliente del registry...")
        print(f"[REGISTRY_CLIENT] Registry URLs: {self.registry_urls}")
        print(f"[REGISTRY_CLIENT] Server ID: {self.server_id}")
        
        # Intentar registro inicial
        self.register()
        
        # Iniciar hilo de heartbeats
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

