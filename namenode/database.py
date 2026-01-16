"""
Base de datos del MetaNameNode - Solo metadatos (nombres y etiquetas)
No almacena rutas físicas de archivos (eso lo manejan los DataNodes)
"""
import sqlite3
import os
import threading
import time
import json
from queue import Queue

# Locks separados para cada base de datos (evita bloqueos cruzados)
# Usar ReadWriteLock para permitir múltiples lecturas simultáneas
from namenode.rw_lock import ReadWriteLock, WriteLock

_metadata_rw_lock = ReadWriteLock()  # Para metadatos (files, tags, users, etc.)
_operations_rw_lock = ReadWriteLock()  # Para logs de operaciones (operation_log, operation_states, etc.)

# Wrappers para compatibilidad: usar como context manager para escritura (comportamiento legacy)
class _WriteLockWrapper:
    """Wrapper que hace que 'with db_lock:' funcione como write lock"""
    def __init__(self, rw_lock):
        self.rw_lock = rw_lock
    
    def __enter__(self):
        self.rw_lock.acquire_write()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.rw_lock.release_write()

metadata_db_lock = _WriteLockWrapper(_metadata_rw_lock)
operations_db_lock = _WriteLockWrapper(_operations_rw_lock)

# Lock legacy para compatibilidad (usar metadata_db_lock como write lock)
db_lock = metadata_db_lock

# Exponer los locks RW reales para uso avanzado
metadata_rw_lock = _metadata_rw_lock
operations_rw_lock = _operations_rw_lock

# Cola global para replicación asíncrona de escrituras SQL
_sql_replication_queue = Queue()
_replication_thread_started = False
_replication_thread_lock = threading.Lock()

# NODE_ID del contenedor actual - solo este nodo debe tener su carpeta de datos
_CURRENT_NODE_ID = os.getenv("NODE_ID", "namenode-1")

def _get_current_node_id() -> str:
    """Obtiene el NODE_ID del contenedor actual"""
    return _CURRENT_NODE_ID

def _start_replication_thread_if_needed():
    """Inicia el hilo de replicación asíncrona si no está iniciado"""
    global _replication_thread_started
    with _replication_thread_lock:
        if not _replication_thread_started:
            _replication_thread_started = True
            thread = threading.Thread(target=_replication_worker, daemon=True)
            thread.start()
            print("[DATABASE] Hilo de replicación asíncrona iniciado")


def _replication_worker():
    """Worker thread que procesa la cola de replicación SQL de forma asíncrona"""
    print("[DATABASE] Worker de replicación SQL iniciado")
    while True:
        try:
            # Esperar hasta 1 segundo por un item (timeout para permitir shutdown graceful)
            try:
                item = _sql_replication_queue.get(timeout=1)
            except Exception:
                continue
            
            sql_writes = item.get('sql_writes', [])
            node_id = item.get('node_id')
            
            if not sql_writes:
                continue
            
            try:
                from namenode.namenode import replicate_sql_writes, is_leader
                # Solo replicar si aún somos líder (puede haber cambiado)
                if is_leader():
                    replicate_sql_writes(sql_writes, node_id)
                else:
                    print(f"[DATABASE] Saltando replicación - ya no somos líder")
            except Exception as e:
                print(f"[DATABASE] Error en worker de replicación: {e}")
                import traceback
                traceback.print_exc()
            
            _sql_replication_queue.task_done()
        except Exception as e:
            print(f"[DATABASE] Error crítico en worker de replicación: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(0.1)  # Pequeña pausa antes de continuar


def get_db_path(node_id: str = None) -> str:
    """Obtiene la ruta de la base de datos de METADATOS para este nodo"""
    return get_metadata_db_path(node_id)

def get_metadata_db_path(node_id: str = None) -> str:
    """
    Obtiene la ruta de la base de datos de METADATOS.
    IMPORTANTE: Siempre usa el NODE_ID del contenedor actual, ignorando el parámetro node_id.
    Esto evita crear carpetas para otros nodos dentro de este contenedor.
    """
    # Siempre usar el NODE_ID del contenedor actual
    current_node_id = _get_current_node_id()
    data_dir = os.path.join(os.path.dirname(__file__), "data", current_node_id)
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "namenode_metadata.db")

def get_operations_db_path(node_id: str = None) -> str:
    """
    Obtiene la ruta de la base de datos de OPERACIONES.
    IMPORTANTE: Siempre usa el NODE_ID del contenedor actual, ignorando el parámetro node_id.
    Esto evita crear carpetas para otros nodos dentro de este contenedor.
    """
    # Siempre usar el NODE_ID del contenedor actual
    current_node_id = _get_current_node_id()
    data_dir = os.path.join(os.path.dirname(__file__), "data", current_node_id)
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "namenode_operations.db")


class ReplicatingConnection:
    """
    Wrapper de conexión que intercepta commits y replica escrituras automáticamente.
    """
    def __init__(self, conn, cursor, db_type: str, node_id: str):
        self._conn = conn
        self._cursor = cursor
        self._db_type = db_type
        self._node_id = node_id
        self._pending_writes = []  # Batch de escrituras para replicar
        self._is_replicating = False  # Flag para evitar loops infinitos
    
    def commit(self):
        """Hace commit local y programa replicación asíncrona de escrituras pendientes"""
        # Commit local (no bloquea)
        self._conn.commit()
        
        # Programar replicación asíncrona si hay escrituras pendientes
        if self._pending_writes and not self._is_replicating:
            try:
                from namenode.namenode import is_leader
                if is_leader():
                    # Enviar a cola para replicación asíncrona (no bloquea)
                    _start_replication_thread_if_needed()
                    _sql_replication_queue.put({
                        'sql_writes': self._pending_writes.copy(),
                        'node_id': self._node_id
                    })
            except Exception as e:
                print(f"[DATABASE] Error programando replicación: {e}")
            finally:
                self._pending_writes = []
    
    # Delegar todos los demás métodos a la conexión real
    def __getattr__(self, name):
        return getattr(self._conn, name)


class ReplicatingCursor:
    """
    Wrapper de cursor que intercepta escrituras y las replica automáticamente.
    Solo replica cuando el nodo es líder y la operación es una escritura (INSERT/UPDATE/DELETE).
    """
    def __init__(self, cursor, conn_wrapper, db_type: str, node_id: str):
        self._cursor = cursor
        self._conn_wrapper = conn_wrapper  # Referencia al wrapper de conexión
        self._db_type = db_type
        self._node_id = node_id
    
    def execute(self, sql, params=None):
        """Ejecuta SQL y captura escrituras para replicación"""
        # Normalizar SQL para detectar escrituras
        sql_upper = sql.strip().upper()
        
        # Detectar si es escritura (INSERT, UPDATE, DELETE, REPLACE)
        is_write = any(sql_upper.startswith(cmd) for cmd in ['INSERT', 'UPDATE', 'DELETE', 'REPLACE'])
        
        # Ejecutar en BD local
        if params:
            result = self._cursor.execute(sql, params)
        else:
            result = self._cursor.execute(sql)
        
        # Si es escritura, somos líder, y no estamos replicando, agregar a batch
        if is_write and not self._conn_wrapper._is_replicating:
            try:
                # Verificar si somos líder (importar aquí para evitar circular imports)
                from namenode.namenode import is_leader
                if is_leader():
                    # Serializar parámetros para replicación
                    params_serialized = None
                    if params:
                        # Convertir parámetros a formato serializable
                        if isinstance(params, (list, tuple)):
                            params_serialized = [self._serialize_param(p) for p in params]
                        else:
                            params_serialized = self._serialize_param(params)
                    
                    self._conn_wrapper._pending_writes.append({
                        'sql': sql,
                        'params': params_serialized,
                        'db_type': self._db_type
                    })
            except Exception as e:
                # Si falla la verificación de líder, continuar sin replicar
                # (puede pasar durante inicialización)
                pass
        
        return result
    
    def executemany(self, sql, params_seq):
        """Ejecuta SQL múltiples veces con diferentes parámetros"""
        # Para executemany, replicar cada ejecución individual
        results = []
        for params in params_seq:
            result = self.execute(sql, params)
            results.append(result)
        return results
    
    def _serialize_param(self, param):
        """Serializa un parámetro para replicación"""
        if param is None:
            return None
        elif isinstance(param, (int, float, str, bool)):
            return param
        elif isinstance(param, bytes):
            # Convertir bytes a base64 para serialización JSON
            import base64
            return {'__type__': 'bytes', '__value__': base64.b64encode(param).decode('utf-8')}
        else:
            # Intentar convertir a string
            return str(param)
    
    # Delegar todos los demás métodos al cursor real
    def __getattr__(self, name):
        return getattr(self._cursor, name)
    
    def fetchone(self):
        return self._cursor.fetchone()
    
    def fetchall(self):
        return self._cursor.fetchall()
    
    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size)
    
    @property
    def rowcount(self):
        return self._cursor.rowcount
    
    @property
    def lastrowid(self):
        return self._cursor.lastrowid


def get_connection(db_path: str = None, node_id: str = None, db_type: str = "metadata"):
    """
    Abre una conexión a la base de datos y devuelve (conn, cursor).
    El cursor está envuelto para replicación automática de escrituras.
    
    Args:
        db_path: Ruta específica de la BD (opcional)
        node_id: ID del nodo (opcional)
        db_type: Tipo de BD - "metadata" o "operations" (default: "metadata")
    """
    if db_path is None:
        if db_type == "operations":
            db_path = get_operations_db_path(node_id)
        else:
            db_path = get_metadata_db_path(node_id)
    
    # Asegurar que existe el directorio
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    # Habilitar WAL mode para mejor concurrencia (menos bloqueos)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")  # 5 segundos máximo de espera
    conn.row_factory = sqlite3.Row  # Para acceder por nombre de columna
    cursor = conn.cursor()
    
    # Envolver conexión y cursor para replicación automática
    # Obtener node_id actual si no se proporciona
    if node_id is None:
        node_id = _get_current_node_id()
    
    replicating_conn = ReplicatingConnection(conn, cursor, db_type, node_id)
    replicating_cursor = ReplicatingCursor(cursor, replicating_conn, db_type, node_id)
    return replicating_conn, replicating_cursor


def init_db(db_path: str = None, node_id: str = None):
    """
    Inicializa AMBAS bases de datos: metadatos y operaciones.
    Crea las tablas necesarias si no existen.
    """
    # Inicializar base de datos de metadatos
    init_metadata_db(db_path, node_id)
    
    # Inicializar base de datos de operaciones
    init_operations_db(node_id)

def init_metadata_db(db_path: str = None, node_id: str = None):
    """
    Inicializa la base de datos de METADATOS.
    Solo almacena metadatos: nombre de archivo, etiquetas, usuarios, datanodes.
    """
    if db_path is None:
        db_path = get_metadata_db_path(node_id)
    
    conn, cursor = get_connection(db_path=db_path, db_type="metadata")
    
    # Tabla de archivos - solo metadatos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        size INTEGER,
        hash TEXT,
        user_id TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        version INTEGER DEFAULT 1,
        last_modified_term INTEGER DEFAULT 0,
        last_modified_timestamp REAL,
        UNIQUE(user_id, name)
    )
    """)
    
    # Migración: agregar user_id a files si no existe (para bases de datos existentes)
    try:
        # Verificar si la columna user_id existe
        cursor.execute("PRAGMA table_info(files)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'user_id' not in columns:
            cursor.execute("ALTER TABLE files ADD COLUMN user_id TEXT")
            cursor.execute("UPDATE files SET user_id = 'system' WHERE user_id IS NULL")
            print("[DATABASE] Columna user_id agregada a tabla files")
    except sqlite3.OperationalError as e:
        # La columna ya existe, continuar
        print(f"[DATABASE] user_id ya existe o error: {e}")
        pass
    
    # Migración: agregar campos de versionado si no existen
    try:
        cursor.execute("PRAGMA table_info(files)")
        columns = [col[1] for col in cursor.fetchall()]
        
        migration_needed = False
        
        if 'version' not in columns:
            cursor.execute("ALTER TABLE files ADD COLUMN version INTEGER DEFAULT 1")
            cursor.execute("UPDATE files SET version = 1 WHERE version IS NULL")
            print("[DATABASE] Columna version agregada a tabla files")
            migration_needed = True
        
        if 'last_modified_term' not in columns:
            cursor.execute("ALTER TABLE files ADD COLUMN last_modified_term INTEGER DEFAULT 0")
            cursor.execute("UPDATE files SET last_modified_term = 0 WHERE last_modified_term IS NULL")
            print("[DATABASE] Columna last_modified_term agregada a tabla files")
            migration_needed = True
        
        if 'last_modified_timestamp' not in columns:
            cursor.execute("ALTER TABLE files ADD COLUMN last_modified_timestamp REAL")
            # Usar time.time() para establecer valores por defecto en lugar de julianday
            cursor.execute("UPDATE files SET last_modified_timestamp = ? WHERE last_modified_timestamp IS NULL", (time.time(),))
            print("[DATABASE] Columna last_modified_timestamp agregada a tabla files")
            migration_needed = True
        
        # Hacer commit de las migraciones si se realizaron cambios
        if migration_needed:
            conn.commit()
            print("[DATABASE] Migraciones de campos de versionado completadas y guardadas")
    except sqlite3.OperationalError as e:
        print(f"[DATABASE] Error en migración de campos de versionado: {e}")
        # Intentar hacer rollback si hay un error
        try:
            conn.rollback()
        except:
            pass
        # Re-verificar que las columnas existen después del error
        try:
            cursor.execute("PRAGMA table_info(files)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'last_modified_timestamp' not in columns:
                print("[DATABASE] ERROR CRÍTICO: Columna last_modified_timestamp no existe y no se pudo agregar")
                # Intentar agregar la columna de nuevo con un enfoque más simple
                try:
                    cursor.execute("ALTER TABLE files ADD COLUMN last_modified_timestamp REAL")
                    cursor.execute("UPDATE files SET last_modified_timestamp = ? WHERE last_modified_timestamp IS NULL", (time.time(),))
                    conn.commit()
                    print("[DATABASE] Columna last_modified_timestamp agregada con método alternativo")
                except Exception as e2:
                    print(f"[DATABASE] Error crítico agregando last_modified_timestamp: {e2}")
        except Exception as e3:
            print(f"[DATABASE] Error verificando columnas después de fallo de migración: {e3}")
    
    # Migración: agregar campo replica_count si no existe
    try:
        cursor.execute("PRAGMA table_info(files)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'replica_count' not in columns:
            cursor.execute("ALTER TABLE files ADD COLUMN replica_count INTEGER DEFAULT 0")
            # Inicializar contador basado en réplicas existentes
            cursor.execute("""
                UPDATE files 
                SET replica_count = (
                    SELECT COUNT(*) 
                    FROM file_replicas 
                    WHERE file_replicas.file_id = files.id
                )
            """)
            conn.commit()
            print("[DATABASE] Columna replica_count agregada a tabla files e inicializada")
    except sqlite3.OperationalError as e:
        print(f"[DATABASE] Error en migración de replica_count: {e}")
        pass
    
    # Asegurar que user_id no sea NULL
    try:
        cursor.execute("UPDATE files SET user_id = 'system' WHERE user_id IS NULL")
    except sqlite3.OperationalError:
        pass
    
    # Eliminar cualquier restricción UNIQUE antigua solo en 'name'
    # SQLite no permite eliminar restricciones UNIQUE directamente, pero podemos
    # verificar si hay duplicados y manejarlos, o recrear la tabla si es necesario
    try:
        # Verificar si hay archivos sin user_id o con user_id NULL
        cursor.execute("SELECT COUNT(*) FROM files WHERE user_id IS NULL")
        null_count = cursor.fetchone()[0]
        if null_count > 0:
            cursor.execute("UPDATE files SET user_id = 'system' WHERE user_id IS NULL")
            print(f"[DATABASE] Actualizados {null_count} archivos con user_id='system'")
    except Exception as e:
        print(f"[DATABASE] Error verificando user_id: {e}")
    
    # Crear índice único correcto con user_id y name
    # Esto permite que diferentes usuarios tengan archivos con el mismo nombre
    try:
        cursor.execute("DROP INDEX IF EXISTS idx_files_user_name")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_user_name ON files(user_id, name)")
        print("[DATABASE] Índice único (user_id, name) creado/verificado")
    except sqlite3.OperationalError as e:
        print(f"[DATABASE] Error creando índice único: {e}")
        pass
    
    # Migración: inicializar usuario admin si no existe
    try:
        from passlib.context import CryptContext
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        admin_password_hash = pwd_context.hash("admin")
        
        cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO users (username, password_hash, role, is_active)
                VALUES (?, ?, ?, ?)
            """, ("admin", admin_password_hash, "ADMIN", 1))
            print("[DATABASE] Usuario admin creado con contraseña 'admin'")
    except Exception as e:
        # Si falla (por ejemplo, passlib no disponible), continuar
        print(f"[DATABASE] No se pudo crear usuario admin: {e}")
        pass
    
    # Tabla de etiquetas - cada etiqueta pertenece a un usuario (aislamiento estricto)
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                user_id TEXT NOT NULL,  -- OBLIGATORIO: cada etiqueta pertenece a un usuario
                UNIQUE(user_id, tag)
            )
        """)
        conn.commit()  # Asegurar que la tabla se crea antes de continuar
        print("[DATABASE] Tabla 'tags' creada/verificada")
    except sqlite3.OperationalError as e:
        print(f"[DATABASE] Error creando tabla tags: {e}")
        # Intentar verificar si la tabla existe de otra manera
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tags'")
        if not cursor.fetchone():
            raise  # Re-lanzar el error si la tabla realmente no existe
    
    # Migración: agregar user_id a tags si no existe y migrar datos existentes
    try:
        # Verificar si la columna user_id existe
        cursor.execute("PRAGMA table_info(tags)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'user_id' not in columns:
            # Agregar columna user_id
            cursor.execute("ALTER TABLE tags ADD COLUMN user_id TEXT")
            print("[DATABASE] Columna user_id agregada a tabla tags")
        
        # Migrar datos existentes: asignar user_id a etiquetas basándose en archivos asociados
        try:
            cursor.execute("""
                UPDATE tags 
                SET user_id = (
                    SELECT DISTINCT f.user_id 
                    FROM files f
                    JOIN file_tags ft ON f.id = ft.file_id
                    WHERE ft.tag_id = tags.id AND f.user_id IS NOT NULL
                    LIMIT 1
                )
                WHERE user_id IS NULL AND id IN (
                    SELECT DISTINCT ft.tag_id 
                    FROM file_tags ft
                    JOIN files f ON ft.file_id = f.id
                    WHERE f.user_id IS NOT NULL
                )
            """)
            migrated_count = cursor.rowcount
            if migrated_count > 0:
                print(f"[DATABASE] Migradas {migrated_count} etiquetas con user_id basado en archivos asociados")
            
            # Asignar 'system' a etiquetas que aún no tienen user_id pero tienen archivos
            cursor.execute("""
                UPDATE tags 
                SET user_id = 'system'
                WHERE user_id IS NULL AND id IN (
                    SELECT DISTINCT tag_id FROM file_tags
                )
            """)
            system_count = cursor.rowcount
            if system_count > 0:
                print(f"[DATABASE] Asignadas {system_count} etiquetas legacy a user_id='system'")
            
            # Eliminar etiquetas sin archivos asociados y sin user_id
            cursor.execute("""
                DELETE FROM tags 
                WHERE user_id IS NULL 
                AND id NOT IN (SELECT DISTINCT tag_id FROM file_tags)
            """)
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                print(f"[DATABASE] Eliminadas {deleted_count} etiquetas huérfanas sin user_id")
            
            conn.commit()
        except Exception as e:
            print(f"[DATABASE] Error en migración de etiquetas: {e}")
            conn.rollback()
        
        # Asegurar que todas las etiquetas tengan user_id
        try:
            cursor.execute("UPDATE tags SET user_id = 'system' WHERE user_id IS NULL")
            if cursor.rowcount > 0:
                print(f"[DATABASE] Asignadas {cursor.rowcount} etiquetas restantes a user_id='system'")
                conn.commit()
        except Exception as e:
            print(f"[DATABASE] Error asignando user_id a etiquetas: {e}")
        
        # Crear índice único correcto
        cursor.execute("DROP INDEX IF EXISTS idx_tags_user_tag")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_user_tag ON tags(user_id, tag)")
        print("[DATABASE] Índice único (user_id, tag) creado/verificado")
    except sqlite3.OperationalError as e:
        # La columna ya existe o el índice ya existe, continuar
        print(f"[DATABASE] user_id ya existe o error en migración: {e}")
        pass
    
    # Tabla intermedia archivo-etiqueta
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_tags (
            file_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY(file_id, tag_id),
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)
    
    # Tabla para tracking de réplicas en DataNodes (se usará más adelante)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS file_replicas (
            file_id INTEGER,
            datanode_id TEXT,
            replica_type TEXT,  -- 'primary', 'secondary', 'tertiary'
            PRIMARY KEY(file_id, datanode_id),
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    """)
    
    # Tabla de DataNodes registrados
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS datanodes (
            node_id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            port INTEGER NOT NULL,
            ip TEXT,
            total_space INTEGER NOT NULL,
            free_space INTEGER NOT NULL,
            last_heartbeat TIMESTAMP,
            status TEXT DEFAULT 'active',
            draining BOOLEAN DEFAULT 0,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Índices optimizados para DataNodes (consultas frecuentes)
    try:
        # Índice compuesto para get_active_datanodes (status + draining + free_space)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_datanodes_status_draining_space 
            ON datanodes(status, draining, free_space DESC)
        """)
        # Índice para consultas por status
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_datanodes_status 
            ON datanodes(status)
        """)
        # Índice para heartbeats (last_heartbeat)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_datanodes_heartbeat 
            ON datanodes(last_heartbeat)
        """)
        print("[DATABASE] Índices optimizados para DataNodes creados/verificados")
    except sqlite3.OperationalError as e:
        print(f"[DATABASE] Error creando índices de DataNodes: {e}")
    
    # Tabla de usuarios (autenticación)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            email TEXT
        )
    """)
    
    # NOTA: Las tablas de operaciones (operation_log, operation_states, operation_state_log)
    # ahora están en una base de datos separada (init_operations_db)
    
    # Índices para mejorar rendimiento
    # Verificar que las tablas existen antes de crear índices
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'")
    if cursor.fetchone():
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_files_user_id ON files(user_id)")
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tags'")
    if cursor.fetchone():
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_user_id ON tags(user_id)")
    else:
        print("[DATABASE] Advertencia: tabla 'tags' no existe, no se puede crear índice idx_tags_user_id")
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if cursor.fetchone():
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
    
    conn.commit()
    conn.close()
    print(f"[DATABASE] Base de datos de METADATOS inicializada: {db_path}")


def init_operations_db(node_id: str = None):
    """
    Inicializa la base de datos de OPERACIONES.
    Almacena logs de operaciones, estados de operaciones y auditoría.
    """
    db_path = get_operations_db_path(node_id)
    conn, cursor = get_connection(db_path=db_path, db_type="operations")
    
    # Tabla para log persistente de operaciones (Fase 2)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL,
            data TEXT NOT NULL,  -- JSON string
            term INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            node_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_term ON operation_log(term)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_timestamp ON operation_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_log_node_id ON operation_log(node_id)")
    
    # Tabla para estado actual de operaciones activas (nueva)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_states (
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,  -- 'upload', 'replicate', 'delete', etc.
            user_id TEXT,
            state TEXT NOT NULL,  -- 'init', 'in_progress', 'completed', 'failed'
            progress_data TEXT,  -- JSON con detalles del progreso
            metadata TEXT,  -- JSON con metadata de la operación
            created_at REAL NOT NULL,
            last_updated REAL NOT NULL,
            retry_count INTEGER DEFAULT 0
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_states_user ON operation_states(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_states_state ON operation_states(state)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_states_type ON operation_states(operation_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_states_updated ON operation_states(last_updated)")
    
    # Tabla para historial de cambios de estado (auditoría y recovery)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operation_state_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            state_change TEXT,  -- 'init' -> 'receiving_chunks'
            progress_snapshot TEXT  -- JSON snapshot del progreso
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_state_log_op ON operation_state_log(operation_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_operation_state_log_time ON operation_state_log(timestamp)")
    
    conn.commit()
    conn.close()
    print(f"[DATABASE] Base de datos de OPERACIONES inicializada: {db_path}")


def close_connection(conn):
    """
    Cierra la conexión con la base de datos.
    """
    if conn:
        conn.close()


def reset_db(db_path: str = None, node_id: str = None) -> None:
    """
    Elimina y recrea las tablas.
    ⚠️ ADVERTENCIA: Esta función destruye toda la base de datos.
    Solo debe usarse para pruebas.
    """
    if db_path is None:
        db_path = get_db_path(node_id)
    
    conn, cursor = get_connection(db_path=db_path)
    
    # Eliminar tablas existentes
    cursor.execute("DROP TABLE IF EXISTS file_replicas")
    cursor.execute("DROP TABLE IF EXISTS file_tags")
    cursor.execute("DROP TABLE IF EXISTS tags")
    cursor.execute("DROP TABLE IF EXISTS files")
    cursor.execute("DROP TABLE IF EXISTS datanodes")
    cursor.execute("DROP TABLE IF EXISTS users")
    conn.commit()
    conn.close()
    
    # Recrear las tablas vacías
    init_db(db_path=db_path, node_id=node_id)
    
    print(f"[DATABASE] Base de datos reiniciada: {db_path}")

