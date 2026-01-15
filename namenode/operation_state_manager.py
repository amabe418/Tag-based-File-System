"""
Módulo para gestión persistente del estado de operaciones
Permite recuperación de operaciones después de reinicios del NameNode
"""
import json
import time
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from namenode.database import get_connection, get_operations_db_path, operations_db_lock, close_connection


@dataclass
class OperationState:
    """Estado de una operación persistente"""
    operation_id: str
    operation_type: str  # 'upload', 'replicate', 'delete'
    user_id: Optional[str]
    state: str  # 'init', 'in_progress', 'completed', 'failed'
    progress_data: Dict[str, Any]
    metadata: Dict[str, Any]
    created_at: float
    last_updated: float
    retry_count: int = 0


class OperationStateManager:
    """
    Gestiona el estado persistente de operaciones
    Thread-safe para operaciones concurrentes
    Con batch updates para reducir escrituras a BD
    """
    
    def __init__(self, node_id: str = None, batch_interval: float = 3.0, debounce_interval: float = 2.0):
        self.node_id = node_id
        self.lock = threading.Lock()
        # Cache en memoria para acceso rápido
        self._state_cache: Dict[str, OperationState] = {}
        self._cache_dirty = set()  # IDs que necesitan persistirse
        self._batch_interval = batch_interval  # Segundos entre batch writes
        self._debounce_interval = debounce_interval  # Segundos de debounce para update_progress
        self._last_batch_write = time.time()
        self._last_progress_update: Dict[str, float] = {}  # operation_id -> timestamp
        self._batch_thread = None
        self._stop_batch_thread = threading.Event()
        self._start_batch_thread()
    
    def _get_db_path(self):
        """Obtiene la ruta de la base de datos de OPERACIONES"""
        return get_operations_db_path(self.node_id)
    
    def create_operation(
        self,
        operation_id: str,
        operation_type: str,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        progress_data: Optional[Dict[str, Any]] = None
    ) -> OperationState:
        """
        Crea una nueva operación persistente
        
        Args:
            operation_id: ID único de la operación
            operation_type: Tipo de operación ('upload', 'replicate', 'delete')
            user_id: ID del usuario que inició la operación
            metadata: Metadata de la operación
            progress_data: Datos de progreso iniciales
        
        Returns:
            OperationState creado
        """
        current_time = time.time()
        
        state = OperationState(
            operation_id=operation_id,
            operation_type=operation_type,
            user_id=user_id,
            state='init',
            progress_data=progress_data or {},
            metadata=metadata or {},
            created_at=current_time,
            last_updated=current_time,
            retry_count=0
        )
        
        # Guardar en BD
        try:
            self._persist_state(state)
        except Exception as e:
            print(f"[OPERATION_STATE] ⚠️  Error persistiendo estado inicial (continuando): {e}")
            # Continuar aunque falle la persistencia inicial
        
        # Agregar a cache
        with self.lock:
            self._state_cache[operation_id] = state
        
        # Registrar en log de cambios (no bloqueante, en caso de error continuar)
        try:
            self._log_state_change(operation_id, 'init', state.progress_data)
        except Exception as e:
            print(f"[OPERATION_STATE] ⚠️  Error registrando log inicial (continuando): {e}")
            # Continuar aunque falle el log
        
        print(f"[OPERATION_STATE] ✅ Operación creada: {operation_id} | tipo={operation_type} | usuario={user_id}")
        
        return state
    
    def update_operation_state(
        self,
        operation_id: str,
        new_state: str,
        progress_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Actualiza el estado de una operación
        
        Args:
            operation_id: ID de la operación
            new_state: Nuevo estado ('in_progress', 'completed', 'failed')
            progress_data: Datos de progreso actualizados
            metadata: Metadata actualizada
        
        Returns:
            True si se actualizó correctamente, False si no existe
        """
        with self.lock:
            state = self._state_cache.get(operation_id)
            if not state:
                # Intentar cargar desde BD
                state = self._load_state_from_db(operation_id)
                if not state:
                    return False
            
            old_state = state.state
            state.state = new_state
            state.last_updated = time.time()
            
            if progress_data is not None:
                # Merge con datos existentes
                state.progress_data.update(progress_data)
            
            if metadata is not None:
                state.metadata.update(metadata)
            
            # Marcar como dirty para persistir
            self._cache_dirty.add(operation_id)
        
        # Persistir inmediatamente solo para estados críticos (completed, failed)
        # De lo contrario, el batch thread se encargará
        is_critical = new_state in ('completed', 'failed')
        if is_critical:
            try:
                self._persist_state(state, update_log=True)
            except Exception as e:
                print(f"[OPERATION_STATE] ⚠️  Error persistiendo estado crítico (continuando): {e}")
        
        # Registrar cambio de estado en log
        if old_state != new_state:
            if is_critical:
                # Log inmediato para estados críticos
                self._log_state_change(operation_id, f"{old_state} -> {new_state}", state.progress_data)
            print(f"[OPERATION_STATE] 🔄 Estado actualizado: {operation_id} | {old_state} -> {new_state}")
        
        return True
    
    def update_progress(
        self,
        operation_id: str,
        progress_data: Dict[str, Any],
        force_log: bool = False
    ) -> bool:
        """
        Actualiza solo el progreso de una operación (más eficiente que update_operation_state)
        
        Args:
            operation_id: ID de la operación
            progress_data: Datos de progreso a actualizar
            force_log: Si True, registra en log aunque no cambie el estado
        
        Returns:
            True si se actualizó correctamente
        """
        current_time = time.time()
        
        with self.lock:
            state = self._state_cache.get(operation_id)
            if not state:
                state = self._load_state_from_db(operation_id)
                if not state:
                    return False
            
            # Merge progreso
            state.progress_data.update(progress_data)
            state.last_updated = current_time
            self._cache_dirty.add(operation_id)
            self._last_progress_update[operation_id] = current_time
        
        # Persistir inmediatamente solo si:
        # 1. Es crítico (force_log)
        # 2. Ha pasado suficiente tiempo desde la última actualización (debouncing)
        last_update = self._last_progress_update.get(operation_id, 0)
        time_since_update = current_time - last_update
        
        should_persist_now = force_log or time_since_update >= self._debounce_interval
        
        if should_persist_now:
            try:
                self._persist_state(state, update_log=force_log)
                # Resetear el timestamp después de persistir
                with self.lock:
                    self._last_progress_update[operation_id] = current_time
            except Exception as e:
                print(f"[OPERATION_STATE] ⚠️  Error persistiendo progreso (continuando): {e}")
        # Si no se persiste ahora, el batch thread se encargará en el próximo ciclo
        
        return True
    
    def get_operation(self, operation_id: str) -> Optional[OperationState]:
        """
        Obtiene el estado de una operación
        
        Args:
            operation_id: ID de la operación
        
        Returns:
            OperationState o None si no existe
        """
        with self.lock:
            # Intentar desde cache primero
            if operation_id in self._state_cache:
                return self._state_cache[operation_id]
            
            # Cargar desde BD
            state = self._load_state_from_db(operation_id)
            if state:
                self._state_cache[operation_id] = state
            
            return state
    
    def get_operations_by_state(
        self,
        state: Optional[str] = None,
        operation_type: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> List[OperationState]:
        """
        Obtiene operaciones filtradas por criterios
        
        Args:
            state: Filtrar por estado ('in_progress', 'completed', etc.)
            operation_type: Filtrar por tipo ('upload', 'replicate', etc.)
            user_id: Filtrar por usuario
        
        Returns:
            Lista de OperationState
        """
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            query = "SELECT * FROM operation_states WHERE 1=1"
            params = []
            
            if state:
                query += " AND state = ?"
                params.append(state)
            
            if operation_type:
                query += " AND operation_type = ?"
                params.append(operation_type)
            
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            
            query += " ORDER BY last_updated DESC"
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            operations = []
            for row in rows:
                try:
                    state_obj = OperationState(
                        operation_id=row['operation_id'],
                        operation_type=row['operation_type'],
                        user_id=row['user_id'],
                        state=row['state'],
                        progress_data=json.loads(row['progress_data'] or '{}'),
                        metadata=json.loads(row['metadata'] or '{}'),
                        created_at=row['created_at'],
                        last_updated=row['last_updated'],
                        retry_count=row['retry_count']
                    )
                    operations.append(state_obj)
                except Exception as e:
                    print(f"[OPERATION_STATE] Error parseando operación {row['operation_id']}: {e}")
            
            return operations
            
        finally:
            close_connection(conn)
    
    def complete_operation(self, operation_id: str, success: bool = True) -> bool:
        """
        Marca una operación como completada o fallida
        
        Args:
            operation_id: ID de la operación
            success: True si fue exitosa, False si falló
        
        Returns:
            True si se actualizó correctamente
        """
        new_state = 'completed' if success else 'failed'
        return self.update_operation_state(operation_id, new_state)
    
    def increment_retry(self, operation_id: str) -> bool:
        """
        Incrementa el contador de reintentos de una operación
        
        Args:
            operation_id: ID de la operación
        
        Returns:
            True si se actualizó correctamente
        """
        with self.lock:
            state = self._state_cache.get(operation_id)
            if not state:
                state = self._load_state_from_db(operation_id)
                if not state:
                    return False
            
            state.retry_count += 1
            state.last_updated = time.time()
            self._cache_dirty.add(operation_id)
        
        self._persist_state(state)
        return True
    
    def delete_operation(self, operation_id: str) -> bool:
        """
        Elimina una operación (generalmente después de completarse y limpiarse)
        
        Args:
            operation_id: ID de la operación
        
        Returns:
            True si se eliminó correctamente
        """
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            with operations_db_lock:
                cursor.execute("DELETE FROM operation_states WHERE operation_id = ?", (operation_id,))
                conn.commit()
            
            with self.lock:
                if operation_id in self._state_cache:
                    del self._state_cache[operation_id]
                if operation_id in self._cache_dirty:
                    self._cache_dirty.remove(operation_id)
            
            print(f"[OPERATION_STATE] 🗑️  Operación eliminada: {operation_id}")
            return True
            
        except Exception as e:
            print(f"[OPERATION_STATE] Error eliminando operación {operation_id}: {e}")
            conn.rollback()
            return False
        finally:
            close_connection(conn)
    
    def cleanup_old_operations(self, max_age_hours: int = 24, completed_only: bool = True):
        """
        Limpia operaciones antiguas de la base de datos
        
        Args:
            max_age_hours: Edad máxima en horas
            completed_only: Si True, solo limpia operaciones completadas/fallidas
        """
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            cutoff_time = time.time() - (max_age_hours * 3600)
            
            if completed_only:
                query = """
                    DELETE FROM operation_states 
                    WHERE (state = 'completed' OR state = 'failed') 
                    AND last_updated < ?
                """
            else:
                query = "DELETE FROM operation_states WHERE last_updated < ?"
            
            with operations_db_lock:
                cursor.execute(query, (cutoff_time,))
                deleted_count = cursor.rowcount
                conn.commit()
            
            if deleted_count > 0:
                print(f"[OPERATION_STATE] 🧹 Limpiadas {deleted_count} operaciones antiguas (> {max_age_hours}h)")
            
            # Limpiar cache de operaciones eliminadas
            with self.lock:
                to_remove = [
                    op_id for op_id, state in self._state_cache.items()
                    if state.last_updated < cutoff_time
                ]
                for op_id in to_remove:
                    del self._state_cache[op_id]
                    if op_id in self._cache_dirty:
                        self._cache_dirty.remove(op_id)
            
        except Exception as e:
            print(f"[OPERATION_STATE] Error limpiando operaciones antiguas: {e}")
            conn.rollback()
        finally:
            close_connection(conn)
    
    def _persist_state(self, state: OperationState, update_log: bool = False):
        """Persiste un estado en la base de datos"""
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            # Usar timeout más corto para evitar bloqueos prolongados
            # SQLite tiene un timeout por conexión, pero también podemos usar WAL mode
            # Usar operations_db_lock (separado de metadata_db_lock)
            with operations_db_lock:
                # Usar timeout en la conexión SQLite para evitar bloqueos indefinidos
                conn.execute("PRAGMA busy_timeout = 5000")  # 5 segundos máximo de espera
                cursor.execute("""
                    INSERT OR REPLACE INTO operation_states 
                    (operation_id, operation_type, user_id, state, progress_data, metadata, 
                     created_at, last_updated, retry_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    state.operation_id,
                    state.operation_type,
                    state.user_id,
                    state.state,
                    json.dumps(state.progress_data),
                    json.dumps(state.metadata),
                    state.created_at,
                    state.last_updated,
                    state.retry_count
                ))
                conn.commit()
            
            if update_log:
                self._log_state_change(state.operation_id, state.state, state.progress_data)
            
        except Exception as e:
            print(f"[OPERATION_STATE] Error persistiendo estado {state.operation_id}: {e}")
            try:
                conn.rollback()
            except:
                pass
        finally:
            close_connection(conn)
    
    def _load_state_from_db(self, operation_id: str) -> Optional[OperationState]:
        """Carga un estado desde la base de datos"""
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            cursor.execute("""
                SELECT * FROM operation_states WHERE operation_id = ?
            """, (operation_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            state = OperationState(
                operation_id=row['operation_id'],
                operation_type=row['operation_type'],
                user_id=row['user_id'],
                state=row['state'],
                progress_data=json.loads(row['progress_data'] or '{}'),
                metadata=json.loads(row['metadata'] or '{}'),
                created_at=row['created_at'],
                last_updated=row['last_updated'],
                retry_count=row['retry_count']
            )
            
            # Agregar a cache
            with self.lock:
                self._state_cache[operation_id] = state
            
            return state
            
        except Exception as e:
            print(f"[OPERATION_STATE] Error cargando estado {operation_id}: {e}")
            return None
        finally:
            close_connection(conn)
    
    def _start_batch_thread(self):
        """Inicia el thread de batch writes"""
        def batch_writer():
            while not self._stop_batch_thread.is_set():
                try:
                    # Esperar el intervalo o hasta que se detenga
                    if self._stop_batch_thread.wait(self._batch_interval):
                        break  # Se detuvo el thread
                    
                    # Persistir todos los estados dirty
                    self._flush_dirty_states()
                except Exception as e:
                    print(f"[OPERATION_STATE] Error en batch writer: {e}")
        
        self._batch_thread = threading.Thread(target=batch_writer, daemon=True)
        self._batch_thread.start()
        print(f"[OPERATION_STATE] Batch writer iniciado (intervalo: {self._batch_interval}s)")
    
    def _flush_dirty_states(self):
        """Persiste todos los estados marcados como dirty en un solo batch"""
        with self.lock:
            if not self._cache_dirty:
                return
            
            # Copiar IDs dirty y limpiar el set
            dirty_ids = list(self._cache_dirty)
            self._cache_dirty.clear()
            states_to_persist = [
                self._state_cache.get(op_id) for op_id in dirty_ids
                if op_id in self._state_cache
            ]
        
        if not states_to_persist:
            return
        
        # Persistir todos en una sola transacción
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            with operations_db_lock:
                conn.execute("PRAGMA busy_timeout = 5000")
                
                # Insertar/actualizar todos los estados en batch
                for state in states_to_persist:
                    if state is None:
                        continue
                    cursor.execute("""
                        INSERT OR REPLACE INTO operation_states 
                        (operation_id, operation_type, user_id, state, progress_data, metadata, 
                         created_at, last_updated, retry_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        state.operation_id,
                        state.operation_type,
                        state.user_id,
                        state.state,
                        json.dumps(state.progress_data),
                        json.dumps(state.metadata),
                        state.created_at,
                        state.last_updated,
                        state.retry_count
                    ))
                
                conn.commit()
                self._last_batch_write = time.time()
                print(f"[OPERATION_STATE] 📦 Batch write: {len(states_to_persist)} estados persistidos")
        except Exception as e:
            print(f"[OPERATION_STATE] Error en batch write: {e}")
            try:
                conn.rollback()
            except:
                pass
            # Re-agregar IDs a dirty para reintentar
            with self.lock:
                self._cache_dirty.update(dirty_ids)
        finally:
            close_connection(conn)
    
    def flush_now(self):
        """Fuerza un flush inmediato de todos los estados dirty"""
        self._flush_dirty_states()
    
    def _log_state_change(
        self,
        operation_id: str,
        state_change: str,
        progress_snapshot: Dict[str, Any]
    ):
        """Registra un cambio de estado en el log de auditoría"""
        db_path = self._get_db_path()
        conn, cursor = get_connection(db_path=db_path, node_id=self.node_id, db_type="operations")
        
        try:
            with operations_db_lock:
                cursor.execute("""
                    INSERT INTO operation_state_log 
                    (operation_id, timestamp, state_change, progress_snapshot)
                    VALUES (?, ?, ?, ?)
                """, (
                    operation_id,
                    time.time(),
                    state_change,
                    json.dumps(progress_snapshot)
                ))
                conn.commit()
        except Exception as e:
            print(f"[OPERATION_STATE] Error registrando cambio de estado: {e}")
            conn.rollback()
        finally:
            close_connection(conn)
    
    def recover_incomplete_operations(self) -> List[OperationState]:
        """
        Recupera todas las operaciones incompletas al iniciar el NameNode
        
        Returns:
            Lista de OperationState incompletas
        """
        incomplete = self.get_operations_by_state(
            state=None  # Obtener todas
        )
        
        # Filtrar solo las incompletas
        incomplete = [
            op for op in incomplete
            if op.state not in ('completed', 'failed')
        ]
        
        if incomplete:
            print(f"[OPERATION_STATE] 🔄 Recuperadas {len(incomplete)} operaciones incompletas")
            for op in incomplete:
                print(f"  - {op.operation_id} ({op.operation_type}): {op.state}")
        
        return incomplete


# Instancia global (se inicializará con node_id al iniciar)
operation_state_manager: Optional[OperationStateManager] = None


def init_operation_state_manager(node_id: str) -> OperationStateManager:
    """Inicializa el manager global de estado de operaciones"""
    global operation_state_manager
    operation_state_manager = OperationStateManager(node_id=node_id)
    return operation_state_manager


def get_operation_state_manager() -> OperationStateManager:
    """Obtiene la instancia global del manager"""
    global operation_state_manager
    if operation_state_manager is None:
        raise RuntimeError("OperationStateManager no inicializado. Llama a init_operation_state_manager() primero.")
    return operation_state_manager
