import sqlite3
import os

# Ruta por defecto de la base de datos
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "db.db")

def get_connection():
    """
    Abre una conexión a la base de datos y devuelve (conn, cursor).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    return conn, cursor

def init_db():
    """
    Inicializa la base de datos creando las tablas necesarias si no existen.
    """
    conn, cursor = get_connection()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            tag TEXT,
            UNIQUE(file_id, tag),
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
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
    conn.commit()
    conn.close()
    init_db()
