"""
Base de datos del MetaNameNode - Solo metadatos (nombres y etiquetas)
No almacena rutas físicas de archivos (eso lo manejan los DataNodes)
"""
import sqlite3
import os
import threading

# Lock para operaciones concurrentes en la base de datos
db_lock = threading.Lock()

def get_db_path(node_id: str = None) -> str:
    """Obtiene la ruta de la base de datos para un nodo específico"""
    if node_id:
        data_dir = os.path.join(os.path.dirname(__file__), "data", node_id)
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "namenode.db")
    else:
        # Fallback para compatibilidad
        base_dir = os.path.dirname(__file__)
        db_dir = os.path.join(base_dir, "..", "database")
        os.makedirs(db_dir, exist_ok=True)
        return os.path.join(db_dir, "namenode.db")


def get_connection(db_path: str = None, node_id: str = None):
    """
    Abre una conexión a la base de datos y devuelve (conn, cursor).
    """
    if db_path is None:
        db_path = get_db_path(node_id)
    
    # Asegurar que existe el directorio
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Para acceder por nombre de columna
    cursor = conn.cursor()
    return conn, cursor


def init_db(db_path: str = None, node_id: str = None):
    """
    Inicializa la base de datos creando las tablas necesarias si no existen.
    Solo almacena metadatos: nombre de archivo y etiquetas.
    """
    if db_path is None:
        db_path = get_db_path(node_id)
    
    conn, cursor = get_connection(db_path=db_path)
    
    # Tabla de archivos - solo metadatos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        size INTEGER,
        hash TEXT,
        user_id TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name)
    )
    """)
    
    # Migración: agregar user_id a files si no existe (para bases de datos existentes)
    try:
        cursor.execute("ALTER TABLE files ADD COLUMN user_id TEXT")
        cursor.execute("UPDATE files SET user_id = 'system' WHERE user_id IS NULL")
        # Recrear índice único con user_id
        cursor.execute("DROP INDEX IF EXISTS idx_files_user_name")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_user_name ON files(user_id, name)")
    except sqlite3.OperationalError:
        # La columna ya existe o el índice ya existe, continuar
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
    
    # Tabla de etiquetas (pueden ser globales o por usuario)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL,
            user_id TEXT,  -- NULL para tags globales, username para tags privadas
            UNIQUE(user_id, tag)
        )
    """)
    
    # Migración: agregar user_id a tags si no existe
    try:
        cursor.execute("ALTER TABLE tags ADD COLUMN user_id TEXT")
        # Actualizar índice único
        cursor.execute("DROP INDEX IF EXISTS idx_tags_user_tag")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_user_tag ON tags(user_id, tag)")
    except sqlite3.OperationalError:
        # La columna ya existe o el índice ya existe, continuar
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
    
    # Índices para mejorar rendimiento
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_files_user_id ON files(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_user_id ON tags(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)")
    
    conn.commit()
    conn.close()
    print(f"[DATABASE] Base de datos inicializada: {db_path}")


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

