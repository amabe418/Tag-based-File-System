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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(name)
    )
    """)
    
    # Tabla de etiquetas (únicas)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT UNIQUE NOT NULL
        )
    """)
    
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
    conn.commit()
    conn.close()
    
    # Recrear las tablas vacías
    init_db(db_path=db_path)
    
    print(f"[DATABASE] Base de datos reiniciada: {db_path}")

