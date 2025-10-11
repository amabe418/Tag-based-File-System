import os
import shutil
from core.database import get_connection, close_connection

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "storage")

# Crear carpeta storage si no existe
os.makedirs(STORAGE_DIR, exist_ok=True)

def add_files(file_list, tag_list, db_path="database/db.db"):
    """
    Agrega ficheros y sus etiquetas al sistema.
    - file_list: lista de rutas a ficheros (locales). Cada fichero se copia a storage/.
    - tag_list: lista de etiquetas (strings, sin espacios).
    Devuelve True si al menos un archivo fue agregado, False si no se agregó ninguno.
    """
    if not tag_list:
        print("[ERROR] No se pueden agregar ficheros sin etiquetas.")
        return False

    # Asegurar storage
    storage_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage"))
    os.makedirs(storage_dir, exist_ok=True)

    conn, cursor = get_connection(db_path)
    added_any = False

    for file_path in file_list:
        file_path = file_path.strip()
        if not file_path:
            continue

        # Comprobar existencia del fichero en el FS local
        if not os.path.exists(file_path):
            print(f"[WARNING] El fichero '{file_path}' no existe en el sistema de archivos. Se omite.")
            continue

        file_name = os.path.basename(file_path)

        # Comprobar si ya existe en la BD
        cursor.execute("SELECT id FROM files WHERE name = ?", (file_name,))
        row = cursor.fetchone()
        if row:
            print(f"[ERROR] El fichero '{file_name}' ya existe en la base de datos. Se omite.")
            continue

        # Insertar fichero (con path temporal vacío) y recuperar id con SELECT
        cursor.execute("INSERT INTO files (name, path) VALUES (?, ?)", (file_name, ""))
        # Recuperar id (no usar lastrowid tras INSERT OR IGNORE)
        cursor.execute("SELECT id FROM files WHERE name = ?", (file_name,))
        file_id = cursor.fetchone()[0]

        # Construir nombre/ubicación en storage
        new_file_name = f"{file_id}_{file_name}"
        storage_path = os.path.join(storage_dir, new_file_name)

        # Copiar fichero físico a storage (preservando metadatos)
        try:
            shutil.copy2(file_path, storage_path)
        except Exception as e:
            # Si falla copia, eliminar el registro insertado para mantener consistencia
            cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
            print(f"[ERROR] No se pudo copiar '{file_path}' a storage: {e}. Registro eliminado.")
            continue

        # Actualizar la ruta en la BD
        cursor.execute("UPDATE files SET path = ? WHERE id = ?", (storage_path, file_id))

        # Insertar etiquetas y relaciones
        for tag in tag_list:
            tag = tag.strip()
            if not tag:
                continue
            cursor.execute("INSERT OR IGNORE INTO tags (tag) VALUES (?)", (tag,))
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            tag_id = cursor.fetchone()[0]
            cursor.execute("INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)", (file_id, tag_id))

        print(f"[INFO] Fichero '{file_name}' agregado con etiquetas: {', '.join(tag_list)}")
        added_any = True

    conn.commit()
    close_connection(conn)
    return added_any


def query_files(query_tags=None, db_path="database/db.db"):
    """
    Devuelve lista de tuplas (id, name, tags_concat, path) que cumplen la consulta.
    - query_tags: lista de etiquetas (AND). Si None o vacía -> devuelve todo.
    """
    if query_tags is None:
        query_tags = []

    conn, cursor = get_connection(db_path)

    if not query_tags:
        cursor.execute("""
            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags, f.path
            FROM files f
            LEFT JOIN file_tags ft ON f.id = ft.file_id
            LEFT JOIN tags t ON ft.tag_id = t.id
            GROUP BY f.id
            ORDER BY f.id
        """)
        results = cursor.fetchall()
    else:
        placeholders = ",".join("?" for _ in query_tags)
        sql = f"""
            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags, f.path
            FROM files f
            JOIN file_tags ft ON f.id = ft.file_id
            JOIN tags t ON ft.tag_id = t.id
            WHERE t.tag IN ({placeholders})
            GROUP BY f.id
            HAVING COUNT(DISTINCT t.tag) = ?
            ORDER BY f.id
        """
        cursor.execute(sql, (*query_tags, len(query_tags)))
        results = cursor.fetchall()

    close_connection(conn)
    return results  # lista de (id, name, tags_concat, path)


def list_files(query_tags=None, db_path="database/db.db"):
    files = query_files(query_tags, db_path)
    if not files:
        print("[INFO] No se encontraron archivos.")
        return files

    for _, name, tags, _ in files:
        print(f"{name} | Etiquetas: {tags}")
    return files


def delete_files(query_tags, db_path="database/db.db"):
    """
    Elimina ficheros que cumplen la query (por etiquetas).
    Borra registros en DB y archivos en storage.
    Devuelve True si se eliminó al menos un archivo, False si no hubo coincidencias.
    """
    if not query_tags:
        # si quieres permitir borrar TODO cuando query vacía, cambia la lógica
        print("[ERROR] delete_files requiere una query de etiquetas.")
        return False

    conn, cursor = get_connection(db_path)

    # obtener archivos a eliminar (usando query_files para consistencia)
    files = query_files(query_tags, db_path)
    if not files:
        close_connection(conn)
        return False

    file_ids = set()
    for file_id, name, tags, path in files:
        file_ids.add((file_id, path, name))

    for fid, path, name in file_ids:
        # borrar archivo físico si existe
        if path and os.path.exists(path):
            try:
                os.remove(path)
                print(f"[INFO] Archivo físico eliminado: {path}")
            except Exception as e:
                print(f"[WARNING] No pude eliminar '{path}': {e}")

        # borrar relaciones y metadatos
        cursor.execute("DELETE FROM file_tags WHERE file_id = ?", (fid,))
        cursor.execute("DELETE FROM files WHERE id = ?", (fid,))
        print(f"[INFO] Eliminado (DB): {name}")

    conn.commit()
    close_connection(conn)
    return True

def add_tags(query_tags, new_tags, db_path="database/db.db"):
    """
    Añade etiquetas new_tags a todos los ficheros que cumplen query_tags.
    Devuelve True si se agregó al menos a un archivo, False si no hubo coincidencias.
    """
    conn, cursor = get_connection(db_path)
    files = query_files(query_tags, db_path)
    if not files:
        close_connection(conn)
        return False

    affected = 0
    for file_id, name, _, _ in files:
        for tag in new_tags:
            tag = tag.strip()
            if not tag:
                continue
            cursor.execute("INSERT OR IGNORE INTO tags (tag) VALUES (?)", (tag,))
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            tag_id = cursor.fetchone()[0]
            cursor.execute("INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)", (file_id, tag_id))
        affected += 1
        print(f"[INFO] Etiquetas agregadas a {name}")

    conn.commit()
    close_connection(conn)
    return affected > 0

def delete_tags(query_tags, del_tags, db_path="database/db.db"):
    """
    Elimina las etiquetas del_tags de los ficheros que cumplen query_tags.
    Devuelve True si al menos una relación fue eliminada, False si no hubo coincidencias.
    """
    conn, cursor = get_connection(db_path)
    files = query_files(query_tags, db_path)
    if not files:
        close_connection(conn)
        return False

    total_deleted = 0
    for file_id, name, _, _ in files:
        for tag in del_tags:
            tag = tag.strip()
            if not tag:
                continue
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            row = cursor.fetchone()
            if row:
                tag_id = row[0]
                cursor.execute("DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?", (file_id, tag_id))
                total_deleted += cursor.rowcount
        print(f"[INFO] Etiquetas eliminadas de {name}")

    conn.commit()
    close_connection(conn)
    return total_deleted > 0


def download_file(file_name, destination_folder, db_path="database/db.db"):
    """
    Copia un archivo del sistema (desde storage/) hacia una carpeta destino existente.
    Si el usuario pasa 'Downloads', automáticamente apunta al directorio de descargas del usuario.
    Devuelve True si se descargó correctamente, False en caso contrario.
    """
    conn, cursor = get_connection(db_path)
    cursor.execute("SELECT path FROM files WHERE name = ?", (file_name,))
    row = cursor.fetchone()
    close_connection(conn)

    if not row:
        print(f"[ERROR] El archivo '{file_name}' no existe en la base de datos.")
        return False

    storage_path = row[0]
    if not storage_path or not os.path.exists(storage_path):
        print(f"[ERROR] El archivo '{file_name}' no se encuentra en el almacenamiento interno.")
        return False

    # Interpretar correctamente carpeta "Downloads"
    if destination_folder.lower() in ("downloads", "~/downloads"):
        destination_folder = os.path.expanduser("~/Downloads")

    # Verificar que exista la carpeta de destino
    if not os.path.isdir(destination_folder):
        print(f"[ERROR] La carpeta destino '{destination_folder}' no existe en el sistema.")
        return False

    destination_path = os.path.join(destination_folder, file_name)

    try:
        shutil.copy2(storage_path, destination_path)
        print(f"[INFO] Archivo '{file_name}' descargado correctamente en '{destination_folder}'.")
        return True
    except Exception as e:
        print(f"[ERROR] No se pudo copiar el archivo: {e}")
        return False
