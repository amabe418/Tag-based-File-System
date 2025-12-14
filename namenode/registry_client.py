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
import sys
from pathlib import Path

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import generate_service_token

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:9000")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))  # segundos
REGISTRY_DISCOVERY_INTERVAL = int(os.getenv("REGISTRY_DISCOVERY_INTERVAL", "30"))  # Intervalo para descubrir nuevos registries (segundos)
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
        # Lista de registries conocidos (se actualiza dinámicamente)
        self._known_registry_urls = set(self.registry_urls)
        self.server_port = NAMENODE_PORT
        # Usar el NODE_ID para construir el nombre del servicio Docker
        # Si NODE_ID es "namenode-1", el nombre del servicio será "tbfs-namenode-1"
        if NAMENODE_ID:
            self.server_url = f"tbfs-{NAMENODE_ID}"  # tbfs-namenode-1, tbfs-namenode-2, etc.
        else:
            self.server_url = get_hostname()  # Fallback al hostname
        self.server_ip = get_server_ip()
        self.heartbeat_thread = None
        self.discovery_thread = None
        self.running = False
        self.registered = False
        self._last_error_time = {}
        self._error_cooldown = 60
        self._registry_urls_lock = threading.Lock()
    
    def _get_service_token(self) -> str:
        """Obtiene un token de servicio para autenticación"""
        try:
            return generate_service_token(self.server_id, "service")
        except Exception as e:
            print(f"[REGISTRY_CLIENT] Error generando token de servicio: {e}")
            # Fallback: usar token pre-compartido si está disponible
            return os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    def _update_registry_urls(self):
        """Actualiza la lista de registry URLs con los registries conocidos"""
        with self._registry_urls_lock:
            # Usar la lista actualizada de registries conocidos
            self.registry_urls = list(self._known_registry_urls)
    
    def discover_registries(self):
        """
        Descubre todos los registries conocidos consultando a los registries disponibles.
        Actualiza la lista de registry URLs para incluir todos los registries descubiertos.
        """
        print(f"[REGISTRY_CLIENT] 🔍 Iniciando descubrimiento de registries...")
        try:
            # Intentar con cualquiera de los registries conocidos
            urls_to_try = list(self._known_registry_urls)
            random.shuffle(urls_to_try)
            
            discovered_registries = set()
            successful_registry = None
            
            for registry_url in urls_to_try:
                try:
                    print(f"[REGISTRY_CLIENT] Consultando registry: {registry_url}/registries")
                    response = requests.get(
                        f"{registry_url}/registries",
                        headers={"Authorization": f"Bearer {self._get_service_token()}"},
                        timeout=5
                    )
                    response.raise_for_status()
                    data = response.json()
                    successful_registry = registry_url
                    
                    # Agregar todos los registries descubiertos
                    registry_count = 0
                    for registry_info in data.get("registries", []):
                        registry_url_from_info = registry_info.get("url")
                        if registry_url_from_info:
                            discovered_registries.add(registry_url_from_info)
                            registry_count += 1
                    
                    print(f"[REGISTRY_CLIENT] ✓ Respuesta recibida de {registry_url}: {registry_count} registries encontrados")
                    
                    # Si obtuvimos información, actualizar y salir
                    if discovered_registries:
                        with self._registry_urls_lock:
                            old_count = len(self._known_registry_urls)
                            old_urls = sorted(self._known_registry_urls.copy())
                            self._known_registry_urls.update(discovered_registries)
                            new_count = len(self._known_registry_urls)
                            new_urls = sorted(self._known_registry_urls)
                            
                            if new_count > old_count:
                                self.registry_urls = list(self._known_registry_urls)
                                print(f"[REGISTRY_CLIENT] ✨ Descubiertos {new_count - old_count} nuevos registries. Total: {new_count}")
                                print(f"[REGISTRY_CLIENT] Registries conocidos: {new_urls}")
                            elif new_urls != old_urls:
                                # Aunque el conteo sea igual, los URLs pueden haber cambiado
                                self.registry_urls = list(self._known_registry_urls)
                                print(f"[REGISTRY_CLIENT] 📋 Lista de registries actualizada: {new_urls}")
                            else:
                                print(f"[REGISTRY_CLIENT] ✓ Lista de registries se mantiene actualizada ({new_count} registries)")
                        break
                except requests.RequestException as e:
                    print(f"[REGISTRY_CLIENT] ⚠️  Error consultando {registry_url}: {e}")
                    # Intentar con el siguiente registry
                    continue
            
            # Si no descubrimos nada, mantener los registries conocidos actuales
            if not discovered_registries:
                print(f"[REGISTRY_CLIENT] ⚠️  No se pudieron descubrir nuevos registries desde ningún registry conocido")
            elif successful_registry:
                print(f"[REGISTRY_CLIENT] ✅ Descubrimiento completado exitosamente desde {successful_registry}")
                
        except Exception as e:
            print(f"[REGISTRY_CLIENT] ❌ Error en descubrimiento de registries: {e}")
    
    def _try_registry_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Intenta hacer una petición a cualquiera de los nodos del registry disponibles"""
        # Asegurarse de que tenemos la lista actualizada
        with self._registry_urls_lock:
            urls = self.registry_urls.copy()
        random.shuffle(urls)
        
        # Agregar token de servicio a los headers
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        kwargs["headers"]["Authorization"] = f"Bearer {self._get_service_token()}"
        
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
    
    def _discovery_loop(self, is_leader_func=None):
        """
        Loop que descubre nuevos registries periódicamente.
        Solo se ejecuta si este nodo es el líder.
        
        Args:
            is_leader_func: Función que retorna True si este nodo es el líder
        """
        # Esperar un poco al inicio para que el registro inicial funcione
        print(f"[REGISTRY_CLIENT] 🔄 Discovery loop iniciado (solo para líder, intervalo: {REGISTRY_DISCOVERY_INTERVAL}s)")
        time.sleep(10)
        
        cycle = 0
        while self.running:
            cycle += 1
            # Solo descubrir si somos el líder
            if is_leader_func and is_leader_func():
                try:
                    print(f"[REGISTRY_CLIENT] 🔄 Ciclo de descubrimiento #{cycle} (cada {REGISTRY_DISCOVERY_INTERVAL}s) - LÍDER")
                    self.discover_registries()
                except Exception as e:
                    print(f"[REGISTRY_CLIENT] ❌ Error en discovery loop (ciclo #{cycle}): {e}")
            else:
                # Si no somos líder, esperar sin descubrir
                if cycle % 10 == 0:  # Log cada 10 ciclos para no saturar
                    print(f"[REGISTRY_CLIENT] ⏸️  Discovery loop pausado (no soy líder) - ciclo #{cycle}")
            
            if self.running:
                if is_leader_func and is_leader_func():
                    print(f"[REGISTRY_CLIENT] ⏳ Esperando {REGISTRY_DISCOVERY_INTERVAL}s hasta el próximo descubrimiento...")
                time.sleep(REGISTRY_DISCOVERY_INTERVAL)
    
    def get_known_registries(self):
        """
        Obtiene la lista de registries conocidos.
        Útil para que el líder comparta esta información con los seguidores.
        """
        with self._registry_urls_lock:
            return sorted(list(self._known_registry_urls))
    
    def update_registries_from_leader(self, registry_urls: list):
        """
        Actualiza la lista de registries conocidos con la información del líder.
        
        Args:
            registry_urls: Lista de URLs de registries proporcionada por el líder
        """
        if not registry_urls:
            return
        
        with self._registry_urls_lock:
            old_count = len(self._known_registry_urls)
            old_urls = sorted(list(self._known_registry_urls))
            
            # Actualizar con los registries del líder
            self._known_registry_urls.update(registry_urls)
            self.registry_urls = list(self._known_registry_urls)
            
            new_count = len(self._known_registry_urls)
            new_urls = sorted(list(self._known_registry_urls))
            
            if new_urls != old_urls:
                print(f"[REGISTRY_CLIENT] 📥 Registries actualizados desde líder: {old_count} -> {new_count}")
                print(f"[REGISTRY_CLIENT] Registries conocidos: {new_urls}")
    
    def start(self, is_leader_func=None):
        """
        Inicia el cliente del registry (registro + heartbeats + discovery)
        
        Args:
            is_leader_func: Función que retorna True si este nodo es el líder.
                          Si se proporciona, solo el líder ejecutará el discovery loop.
        """
        if self.running:
            return
        
        print(f"[REGISTRY_CLIENT] Iniciando cliente del registry para MetaNameNode...")
        print(f"[REGISTRY_CLIENT] Registry URLs iniciales: {self.registry_urls}")
        print(f"[REGISTRY_CLIENT] MetaNameNode ID: {self.server_id}")
        
        # Descubrir registries disponibles al inicio (solo si es líder o no hay función de verificación)
        if not is_leader_func or is_leader_func():
            self.discover_registries()
        
        self.register()
        
        self.running = True
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        
        # Pasar la función de verificación de líder al discovery loop
        self.discovery_thread = threading.Thread(
            target=lambda: self._discovery_loop(is_leader_func=is_leader_func), 
            daemon=True
        )
        self.discovery_thread.start()
        
        discovery_mode = "solo para líder" if is_leader_func else "siempre activo"
        print(f"[REGISTRY_CLIENT] Cliente iniciado. Heartbeat cada {HEARTBEAT_INTERVAL}s, Discovery {discovery_mode} cada {REGISTRY_DISCOVERY_INTERVAL}s")
    
    def stop(self):
        """Detiene el cliente del registry"""
        self.running = False
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        if self.discovery_thread:
            self.discovery_thread.join(timeout=2)
        print(f"[REGISTRY_CLIENT] Cliente detenido")


# Instancia global del cliente
registry_client = RegistryClient()

