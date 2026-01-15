"""
Cache en memoria de DataNodes para evitar consultas frecuentes a la BD
Actualiza automáticamente cuando hay cambios (heartbeats, registros)
"""
import threading
import time
from typing import Dict, List, Optional
from namenode.database import get_db_path, get_connection, close_connection, db_lock


class DataNodeCache:
    """
    Cache thread-safe de DataNodes en memoria
    Se actualiza automáticamente cuando hay cambios
    """
    
    def __init__(self, node_id_db: str = None):
        self.node_id_db = node_id_db
        self.cache: Dict[str, Dict] = {}  # node_id -> datanode_info
        self.cache_lock = threading.RLock()  # Read-write lock (RLock permite re-entrancy)
        self.last_refresh = 0
        self.refresh_interval = 10.0  # Refrescar desde BD cada 10 segundos como máximo
        self._initialized = False
    
    def _load_from_db(self) -> Dict[str, Dict]:
        """Carga todos los DataNodes desde la BD"""
        db_path = get_db_path(self.node_id_db)
        
        with db_lock:
            conn, cursor = get_connection(db_path=db_path, node_id=self.node_id_db)
            
            try:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes
                """)
                
                datanodes = {}
                for row in cursor.fetchall():
                    datanodes[row[0]] = {
                        "node_id": row[0],
                        "url": row[1],
                        "port": row[2],
                        "ip": row[3],
                        "total_space": row[4],
                        "free_space": row[5],
                        "last_heartbeat": row[6],
                        "status": row[7],
                        "registered_at": row[8],
                        "draining": bool(row[9]) if row[9] is not None else False
                    }
                
                return datanodes
            finally:
                close_connection(conn)
    
    def _ensure_initialized(self):
        """Asegura que el cache está inicializado"""
        if not self._initialized:
            with self.cache_lock:
                if not self._initialized:  # Double-check
                    self.cache = self._load_from_db()
                    self.last_refresh = time.time()
                    self._initialized = True
                    print(f"[DATANODE_CACHE] Cache inicializado con {len(self.cache)} DataNodes")
    
    def _maybe_refresh(self):
        """Refresca el cache si ha pasado suficiente tiempo"""
        current_time = time.time()
        if current_time - self.last_refresh > self.refresh_interval:
            with self.cache_lock:
                # Double-check después de adquirir lock
                if current_time - self.last_refresh > self.refresh_interval:
                    self.cache = self._load_from_db()
                    self.last_refresh = current_time
                    print(f"[DATANODE_CACHE] Cache refrescado: {len(self.cache)} DataNodes")
    
    def get_all(self, status: Optional[str] = None, exclude_draining: bool = True) -> List[Dict]:
        """
        Obtiene todos los DataNodes del cache (sin lock de BD)
        
        Args:
            status: Filtrar por status ('active', 'inactive'), None para todos
            exclude_draining: Si True, excluye DataNodes en drenaje
        
        Returns:
            Lista de DataNodes
        """
        self._ensure_initialized()
        self._maybe_refresh()
        
        with self.cache_lock:
            results = list(self.cache.values())
        
        # Filtrar fuera del lock (más rápido)
        if status:
            results = [dn for dn in results if dn["status"] == status]
        
        if exclude_draining:
            results = [dn for dn in results if not dn.get("draining", False)]
        
        return results
    
    def get_active(self, exclude_draining: bool = True) -> List[Dict]:
        """
        Obtiene DataNodes activos ordenados por espacio libre (sin lock de BD)
        
        Args:
            exclude_draining: Si True, excluye DataNodes en drenaje
        
        Returns:
            Lista de DataNodes activos ordenados por free_space DESC
        """
        datanodes = self.get_all(status='active', exclude_draining=exclude_draining)
        # Ordenar por espacio libre (descendente)
        datanodes.sort(key=lambda x: x.get("free_space", 0), reverse=True)
        return datanodes
    
    def get(self, node_id: str) -> Optional[Dict]:
        """
        Obtiene un DataNode específico del cache (sin lock de BD)
        
        Args:
            node_id: ID del DataNode
        
        Returns:
            Información del DataNode o None si no existe
        """
        self._ensure_initialized()
        self._maybe_refresh()
        
        with self.cache_lock:
            return self.cache.get(node_id)
    
    def update(self, node_id: str, updates: Dict):
        """
        Actualiza un DataNode en el cache (sin lock de BD para lectura)
        Se llama cuando hay un heartbeat o registro
        
        Args:
            node_id: ID del DataNode
            updates: Diccionario con campos a actualizar
        """
        self._ensure_initialized()
        
        with self.cache_lock:
            if node_id in self.cache:
                self.cache[node_id].update(updates)
            else:
                # Si no existe, cargar desde BD
                self.cache = self._load_from_db()
                self.last_refresh = time.time()
    
    def invalidate(self):
        """Invalida el cache, forzando recarga en la próxima consulta"""
        with self.cache_lock:
            self.last_refresh = 0
            self._initialized = False
    
    def refresh_now(self):
        """Fuerza un refresh inmediato del cache"""
        with self.cache_lock:
            self.cache = self._load_from_db()
            self.last_refresh = time.time()
            self._initialized = True
            print(f"[DATANODE_CACHE] Cache refrescado manualmente: {len(self.cache)} DataNodes")


# Cache global por node_id_db
_caches: Dict[str, DataNodeCache] = {}
_cache_lock = threading.Lock()


def get_datanode_cache(node_id_db: str = None) -> DataNodeCache:
    """Obtiene o crea el cache para un node_id_db específico"""
    cache_key = node_id_db or "default"
    
    with _cache_lock:
        if cache_key not in _caches:
            _caches[cache_key] = DataNodeCache(node_id_db=node_id_db)
        return _caches[cache_key]
