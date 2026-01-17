"""
Cliente para comunicación directa con NameNodes usando DNS de Docker.
Reemplaza el registry_client, usando resolución DNS para descubrir namenodes.
"""
import requests
import os
import random
import socket
from typing import List, Optional, Dict, Tuple

# Configuración por defecto
NAMENODE_SERVICE = os.getenv("NAMENODE_SERVICE", "namenode")
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))


class NameNodeClient:
    """
    Cliente para comunicarse con NameNodes usando DNS de Docker.
    Descubre namenodes mediante resolución DNS y maneja failover automático.
    """
    
    def __init__(self, namenode_service: str = NAMENODE_SERVICE, namenode_port: int = NAMENODE_PORT):
        self.namenode_service = namenode_service
        self.namenode_port = namenode_port
        self._cached_namenodes: List[str] = []
        self._cache_time = 0
        self._cache_ttl = 10  # segundos - TTL corto para detectar cambios rápido
        self._last_error_time: Dict[str, float] = {}
        self._error_cooldown = 30  # Solo mostrar error cada 30 segundos por URL
    
    def _discover_namenodes_dns(self) -> List[str]:
        """
        Descubre namenodes usando resolución DNS de Docker.
        Docker DNS devuelve todas las IPs de los contenedores con el alias 'namenode'.
        
        Returns:
            Lista de URLs de namenodes descubiertos
        """
        try:
            # Resolver DNS para obtener todas las IPs de los namenodes
            addr_info = socket.getaddrinfo(
                self.namenode_service, 
                self.namenode_port,
                proto=socket.IPPROTO_TCP
            )
            
            # Extraer IPs únicas
            ips = set()
            for info in addr_info:
                ip = info[4][0]  # info[4] es (ip, port)
                ips.add(ip)
            
            # Construir URLs
            urls = [f"http://{ip}:{self.namenode_port}" for ip in ips]
            
            if urls:
                print(f"[NAMENODE_CLIENT] DNS descubrió {len(urls)} namenodes: {urls}")
            
            return urls
            
        except socket.gaierror as e:
            # Error de resolución DNS - probablemente no hay namenodes disponibles
            import time
            current_time = time.time()
            if current_time - self._last_error_time.get("dns", 0) > self._error_cooldown:
                self._last_error_time["dns"] = current_time
                print(f"[NAMENODE_CLIENT] Error DNS resolviendo '{self.namenode_service}': {e}")
            return []
        except Exception as e:
            print(f"[NAMENODE_CLIENT] Error inesperado en descubrimiento DNS: {e}")
            return []
    
    def get_namenodes(self, use_cache: bool = True) -> List[str]:
        """
        Obtiene lista de URLs de namenodes disponibles.
        
        Args:
            use_cache: Si True, usa caché si está disponible y no ha expirado
        
        Returns:
            Lista de URLs de namenodes
        """
        import time
        current_time = time.time()
        
        # Usar caché si está disponible y no ha expirado
        if use_cache and self._cached_namenodes and (current_time - self._cache_time) < self._cache_ttl:
            return self._cached_namenodes
        
        # Descubrir namenodes via DNS
        namenodes = self._discover_namenodes_dns()
        
        if namenodes:
            self._cached_namenodes = namenodes
            self._cache_time = current_time
            return namenodes
        
        # Si hay caché, devolverlo aunque esté expirado
        if self._cached_namenodes:
            print(f"[NAMENODE_CLIENT] Usando namenodes en caché (expirado)")
            return self._cached_namenodes
        
        return []
    
    def get_leader_url(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Obtiene la URL del líder del cluster de namenodes.
        Consulta cada namenode hasta encontrar el líder con reintentos y timeouts más robustos.
        
        Returns:
            Tupla (leader_url, error_message)
        """
        namenodes = self.get_namenodes(use_cache=False)
        
        if not namenodes:
            return None, "No hay namenodes disponibles. Verifica que el servicio esté corriendo."
        
        # Mezclar para distribuir carga
        random.shuffle(namenodes)
        
        last_errors = []
        leader_candidates = []  # URLs candidatas de líder para verificar al final
        
        # Primera pasada: consultar todos los namenodes (incluso si algunos fallan con timeout)
        for namenode_url in namenodes:
            try:
                print(f"[NAMENODE_CLIENT] Consultando namenode: {namenode_url}")
                response = requests.get(f"{namenode_url}/", timeout=10)  # Aumentado a 10s para cambios de líder
                response.raise_for_status()
                data = response.json()
                
                # Si este namenode es el líder
                if data.get("is_leader"):
                    print(f"[NAMENODE_CLIENT] ✅ Líder encontrado: {namenode_url}")
                    return namenode_url, None
                
                # Si conoce al líder, guardarlo como candidato para verificar después
                leader_url = data.get("leader_url")
                leader_id = data.get("leader_id")
                
                if leader_url:
                    leader_candidates.append(leader_url)
                elif leader_id:
                    # Construir URL del líder
                    if leader_id.startswith("namenode-"):
                        constructed_url = f"http://tbfs-{leader_id}:{self.namenode_port}"
                    elif leader_id.startswith("tbfs-"):
                        constructed_url = f"http://{leader_id}:{self.namenode_port}"
                    else:
                        constructed_url = f"http://{leader_id}:{self.namenode_port}"
                    leader_candidates.append(constructed_url)
                        
            except requests.Timeout as e:
                # Timeout no es crítico, continuar con otros namenodes
                error_msg = f"{namenode_url}: Read timed out"
                last_errors.append(error_msg)
                print(f"[NAMENODE_CLIENT] ⚠️  Timeout consultando {namenode_url}, continuando...")
                continue
            except requests.RequestException as e:
                # Otros errores de conexión, continuar con otros namenodes
                error_msg = f"{namenode_url}: {str(e)}"
                last_errors.append(error_msg)
                print(f"[NAMENODE_CLIENT] ⚠️  Error consultando {namenode_url}: {e}")
                continue
            except Exception as e:
                error_msg = f"{namenode_url}: {str(e)}"
                last_errors.append(error_msg)
                print(f"[NAMENODE_CLIENT] ⚠️  Excepción consultando {namenode_url}: {e}")
                continue
        
        # Segunda pasada: verificar candidatos de líder (pueden ser más confiables)
        # Remover duplicados manteniendo orden
        seen = set()
        unique_candidates = []
        for candidate in leader_candidates:
            if candidate not in seen:
                seen.add(candidate)
                unique_candidates.append(candidate)
        
        print(f"[NAMENODE_CLIENT] Verificando {len(unique_candidates)} candidatos de líder: {unique_candidates}")
        
        for leader_url in unique_candidates:
            try:
                print(f"[NAMENODE_CLIENT] Verificando candidato de líder: {leader_url}")
                leader_response = requests.get(f"{leader_url}/", timeout=10)  # 10s timeout
                leader_response.raise_for_status()
                leader_data = leader_response.json()
                if leader_data.get("is_leader"):
                    print(f"[NAMENODE_CLIENT] ✅ Líder verificado: {leader_url}")
                    return leader_url, None
            except requests.Timeout:
                print(f"[NAMENODE_CLIENT] ⚠️  Timeout verificando candidato {leader_url}")
                continue
            except requests.RequestException as e:
                print(f"[NAMENODE_CLIENT] ⚠️  Error verificando candidato {leader_url}: {e}")
                continue
        
        # Si no se encontró líder, construir mensaje de error con más detalle
        error_msg = f"No se encontró líder después de consultar {len(namenodes)} namenodes"
        if last_errors:
            # Mostrar solo algunos errores (no todos) para no saturar
            errors_to_show = last_errors[:3]  # Mostrar solo los primeros 3
            error_msg += f". Últimos errores: {'; '.join(errors_to_show)}"
            if len(last_errors) > 3:
                error_msg += f" (y {len(last_errors) - 3} más...)"
        
        return None, error_msg
    
    def get_any_namenode_url(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Obtiene la URL de cualquier namenode disponible.
        Útil para operaciones que no requieren el líder.
        
        Returns:
            Tupla (namenode_url, error_message)
        """
        namenodes = self.get_namenodes()
        
        if not namenodes:
            return None, "No hay namenodes disponibles."
        
        # Seleccionar uno aleatorio
        namenode_url = random.choice(namenodes)
        return namenode_url, None
    
    def make_request(self, method: str, endpoint: str, require_leader: bool = True, **kwargs) -> requests.Response:
        """
        Realiza una petición HTTP a un namenode con failover automático.
        
        Args:
            method: Método HTTP (get, post, delete, etc.)
            endpoint: Ruta del endpoint (ej: "/list", "/add")
            require_leader: Si True, intenta conectar con el líder primero
            **kwargs: Argumentos adicionales para requests
        
        Returns:
            Response de la petición
        
        Raises:
            requests.RequestException si todos los namenodes fallan
        """
        if require_leader:
            leader_url, error = self.get_leader_url()
            if leader_url:
                try:
                    url = f"{leader_url}{endpoint}"
                    response = requests.request(method, url, timeout=10, **kwargs)
                    response.raise_for_status()
                    return response
                except requests.RequestException:
                    pass  # Continuar con failover
        
        # Failover: intentar con cualquier namenode
        namenodes = self.get_namenodes(use_cache=False)
        
        if not namenodes:
            raise requests.RequestException("No hay namenodes disponibles")
        
        random.shuffle(namenodes)
        last_error = None
        
        for namenode_url in namenodes:
            try:
                url = f"{namenode_url}{endpoint}"
                response = requests.request(method, url, timeout=10, **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as e:
                last_error = e
                continue
        
        raise last_error or requests.RequestException("Todos los namenodes fallaron")
    
    def print_namenodes(self):
        """Imprime información de debug sobre los namenodes descubiertos"""
        try:
            namenodes_info = socket.getaddrinfo(
                self.namenode_service, 
                self.namenode_port,
                proto=socket.IPPROTO_TCP
            )
            print(f"[NAMENODE_CLIENT] DNS info para '{self.namenode_service}':")
            namenodes = set(info[4] for info in namenodes_info)
            for addr in namenodes:
                print(f"  - {addr[0]}:{addr[1]}")
        except socket.gaierror as e:
            print(f"[NAMENODE_CLIENT] No se puede resolver '{self.namenode_service}': {e}")


# Instancia global del cliente
namenode_client = NameNodeClient()
