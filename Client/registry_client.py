"""
Cliente del Registry Service para descubrimiento de servidores
Soporta múltiples nodos del registry con failover automático
"""
import requests
import os
import random
from typing import List, Optional, Dict

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:9000")


class RegistryClient:
    """Cliente para consultar el Registry Service y obtener servidores disponibles"""
    
    def __init__(self, registry_url: str = REGISTRY_URL):
        # Parsear múltiples URLs del registry (separadas por comas)
        if isinstance(registry_url, str):
            self.registry_urls = [url.strip() for url in registry_url.split(",") if url.strip()]
        else:
            self.registry_urls = [registry_url]
        
        self._cached_servers = []
        self._cache_time = 0
        self._cache_ttl = 30  # segundos
    
    def _try_registry_request(self, endpoint: str, **kwargs) -> Optional[requests.Response]:
        """Intenta hacer una petición a cualquiera de los nodos del registry disponibles"""
        # Mezclar URLs para distribuir carga
        urls = self.registry_urls.copy()
        random.shuffle(urls)
        
        for registry_url in urls:
            try:
                response = requests.get(
                    f"{registry_url}{endpoint}",
                    timeout=5,
                    **kwargs
                )
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                print(f"[REGISTRY_CLIENT] Error con nodo {registry_url}: {e}")
                continue
        return None
    
    def get_active_servers(self, use_cache: bool = True) -> List[Dict]:
        """
        Obtiene la lista de servidores activos desde el registry
        - use_cache: si True, usa caché si está disponible
        """
        import time
        current_time = time.time()
        
        # Usar caché si está disponible y no ha expirado
        if use_cache and self._cached_servers and (current_time - self._cache_time) < self._cache_ttl:
            return self._cached_servers
        
        response = self._try_registry_request("/servers/active")
        if response:
            servers = response.json()
            # Actualizar caché
            self._cached_servers = servers
            self._cache_time = current_time
            return servers
        
        # Si hay caché, devolverlo aunque esté expirado
        if self._cached_servers:
            print(f"[REGISTRY_CLIENT] Usando servidores en caché")
            return self._cached_servers
        return []
    
    def get_server_url(self, strategy: str = "random") -> Optional[str]:
        """
        Obtiene la URL de un servidor usando una estrategia de selección
        - strategy: "random" (aleatorio), "first" (primero), "round_robin" (round-robin)
        """
        servers = self.get_active_servers()
        
        if not servers:
            return None
        
        if strategy == "random":
            server = random.choice(servers)
        elif strategy == "first":
            server = servers[0]
        elif strategy == "round_robin":
            # Round-robin simple (podría mejorarse con estado persistente)
            server = servers[0]  # Por ahora usa el primero
        else:
            server = servers[0]
        
        return server.get("url")
    
    def get_all_servers(self) -> List[Dict]:
        """Obtiene todos los servidores (activos e inactivos)"""
        response = self._try_registry_request("/servers")
        if response:
            return response.json()
        return []
    
    def make_request_with_failover(self, method: str, endpoint: str, **kwargs):
        """
        Realiza una petición HTTP con failover automático entre servidores
        - method: "get", "post", "delete", etc.
        - endpoint: ruta del endpoint (ej: "/list", "/add")
        - **kwargs: argumentos adicionales para requests
        """
        servers = self.get_active_servers()
        
        if not servers:
            raise requests.RequestException("No hay servidores disponibles en el registry")
        
        # Intentar con cada servidor hasta que uno funcione
        last_error = None
        for server in servers:
            server_url = server.get("url")
            try:
                url = f"{server_url}{endpoint}"
                response = requests.request(method, url, timeout=10, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                last_error = e
                print(f"[REGISTRY_CLIENT] Error con servidor {server_url}: {e}")
                continue
        
        # Si todos fallaron, lanzar el último error
        raise last_error or requests.RequestException("Todos los servidores fallaron")


# Instancia global del cliente
registry_client = RegistryClient()