import sqlite3
import os

# Ruta por defecto de la base de datos
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "db.db")

def get_connection(db_path="database/db.db"):
    """
    Abre una conexión a la base de datos y devuelve (conn, cursor).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    return conn, cursor

def init_db(db_path="database/db.db"):
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

def reset_db():
    """
    Elimina y recrea las tablas. 
    Esto es solo para probar, no podemos usar esta vaina en el proyecto entero porque
    así como por arte de magia se borra la basedato y yo mato a una gente.
    """
    conn, cursor = get_connection()
    cursor.execute("DROP TABLE IF EXISTS tags")
    cursor.execute("DROP TABLE IF EXISTS files")
    cursor.execute("DROP TABLE IF EXISTS file_tags")
    conn.commit()
    conn.close()
    init_db()
