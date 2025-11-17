import sqlite3
import os
import shutil

DB_PATH = os.path.join(os.path.dirname(__file__).removesuffix("/core"),'database','db.db')
print(f"Ruta: {DB_PATH}")

def get_connection(db_path=DB_PATH):
    """
    Abre una conexión a la base de datos y devuelve (conn, cursor).
    """
    # asegurarse de que existe la carpeta de la base de datos.
    os.makedirs(DB_PATH.removesuffix('/db.db'), exist_ok=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    return conn, cursor

def init_db(db_path=DB_PATH):
    """
    Inicializa la base de datos creando las tablas necesarias si no existen.
    """
    conn, cursor = get_connection(db_path)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        path TEXT NOT NULL
    )
    """)
    
    # Tabla de etiquetas (únicas)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT UNIQUE
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
    conn.commit()
    conn.close()

def close_connection(conn):
    """
    Cierra la conexión con la base de datos.
    """
    if conn:
        conn.close()

def reset_db() -> None:
    """
    Elimina y recrea las tablas, y borra todos los archivos del almacenamiento físico (storage/).
    ⚠️ ADVERTENCIA: Esta función destruye toda la base de datos y los archivos almacenados.
    Solo debe usarse para pruebas.
    """
    conn, cursor = get_connection()

    # Eliminar tablas existentes
    cursor.execute("DROP TABLE IF EXISTS file_tags")
    cursor.execute("DROP TABLE IF EXISTS tags")
    cursor.execute("DROP TABLE IF EXISTS files")
    conn.commit()
    conn.close()

    # Eliminar todos los archivos del almacenamiento
    storage_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage"))
    if os.path.exists(storage_dir):
        for file_name in os.listdir(storage_dir):
            file_path = os.path.join(storage_dir, file_name)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"[WARNING] No se pudo eliminar '{file_path}': {e}")

    # Recrear las tablas vacías
    init_db()

    print("[INFO] Base de datos y almacenamiento reiniciados correctamente.")
