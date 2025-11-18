"""
Cliente para el Registry Service
Maneja el registro automático y envío de heartbeats
"""
import requests
import threading
import time
import os
import socket
import uuid

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


def generate_server_id():
    """Genera un ID único para el servidor"""
    if SERVER_ID:
        return SERVER_ID
    hostname = get_hostname()
    unique_id = str(uuid.uuid4())[:8]
    return f"{hostname}-{unique_id}"


class RegistryClient:
    def __init__(self):
        self.server_id = generate_server_id()
        self.registry_url = REGISTRY_URL
        self.server_port = SERVER_PORT
        self.server_url = get_hostname()
        self.heartbeat_thread = None
        self.running = False
        self.registered = False
    
    def register(self):
        """Registra el servidor en el registry"""
        try:
            response = requests.post(
                f"{self.registry_url}/register",
                json={
                    "server_id": self.server_id,
                    "url": self.server_url,
                    "port": self.server_port
                },
                timeout=5
            )
            response.raise_for_status()
            self.registered = True
            print(f"[REGISTRY_CLIENT] Servidor registrado: {self.server_id} -> {self.server_url}:{self.server_port}")
            return True
        except requests.RequestException as e:
            print(f"[REGISTRY_CLIENT] Error al registrar servidor: {e}")
            return False
    
    def send_heartbeat(self):
        """Envía un heartbeat al registry"""
        try:
            response = requests.post(
                f"{self.registry_url}/heartbeat",
                json={"server_id": self.server_id},
                timeout=5
            )
            response.raise_for_status()
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
        print(f"[REGISTRY_CLIENT] Registry URL: {self.registry_url}")
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

