import os
from core.database import get_connection, close_connection

def add_files(file_list, tag_list, db_path="database/db.db"):
    """
    Agrega ficheros y sus etiquetas al sistema.
    """
    if not tag_list:
        print("[ERROR] No se pueden agregar ficheros sin etiquetas.")
        return

    conn, cursor = get_connection(db_path)

    for file_name in file_list:
        file_name = file_name.strip()

        # Revisar si ya existe
        cursor.execute("SELECT id FROM files WHERE name = ?", (file_name,))
        exists = cursor.fetchone()
        if exists:
            print(f"[ERROR] El fichero '{file_name}' ya existe en la base de datos. No se puede volver a agregar.")
            continue  # saltamos este fichero, pero seguimos con los demás

        # Insertar fichero
        cursor.execute("INSERT INTO files (name) VALUES (?)", (file_name,))
        file_id = cursor.lastrowid

        # Insertar etiquetas y la relación
        for tag in tag_list:
            tag = tag.strip()
            if not tag:
                continue

            # Insertar etiqueta única
            cursor.execute("INSERT OR IGNORE INTO tags (tag) VALUES (?)", (tag,))
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            tag_id = cursor.fetchone()[0]

            # Insertar relación archivo-etiqueta
            cursor.execute("""
                INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)
            """, (file_id, tag_id))

    conn.commit()
    close_connection(conn)
    print("[INFO] Archivos agregados correctamente.")

def query_files(query_tags, db_path="database/db.db"):
    conn, cursor = get_connection(db_path)

    if not query_tags:
        cursor.execute("""
            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag)
            FROM files f
            LEFT JOIN file_tags ft ON f.id = ft.file_id
            LEFT JOIN tags t ON ft.tag_id = t.id
            GROUP BY f.id
        """)
    else:
        placeholders = ",".join("?" for _ in query_tags)
        sql = f"""
        SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag)
        FROM files f
        JOIN file_tags ft ON f.id = ft.file_id
        JOIN tags t ON ft.tag_id = t.id
        WHERE t.tag IN ({placeholders})
        GROUP BY f.id
        HAVING COUNT(DISTINCT t.tag) = ?
        """
        cursor.execute(sql, (*query_tags, len(query_tags)))

    results = cursor.fetchall()
    close_connection(conn)
    return results


def list_files(query_tags):
    """
    Lista en consola los ficheros que cumplen con la consulta.
    """
    files = query_files(query_tags)
    print("ya busque los ficheros")
    print (files)
    for _, name, tags in files:
        print(f"{name} | Etiquetas: {tags}")
    return files


def delete_files(query_tags, db_path="database/db.db"):
    conn, cursor = get_connection(db_path)
    files = query_files(query_tags, db_path)

    # Evitar IDs repetidos
    file_ids = set(file_id for file_id, _, _ in files)

    for file_id in file_ids:
        # Primero eliminar relaciones file_tags
        cursor.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
        # Luego eliminar el fichero
        cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))

    conn.commit()
    close_connection(conn)
    print(f"[INFO] {len(file_ids)} archivos eliminados.")


def add_tags(query_tags, new_tags, db_path="database/db.db"):
    """
    Añade etiquetas a los ficheros que cumplen con la consulta.
    """
    conn, cursor = get_connection(db_path)
    files = query_files(query_tags, db_path)

    for file_id, name, _ in files:
        for tag in new_tags:
            # Normalizamos el tag (sin espacios, en minúsculas por consistencia)
            tag = tag.strip()

            # Insertar etiqueta en tags si no existe
            cursor.execute("INSERT OR IGNORE INTO tags(tag) VALUES (?)", (tag,))
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            tag_id = cursor.fetchone()[0]

            # Relacionar el archivo con la etiqueta
            cursor.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES (?, ?)",
                (file_id, tag_id)
            )

        print(f"[INFO] Etiquetas agregadas a {name}")

    conn.commit()
    close_connection(conn)

def delete_tags(query_tags, del_tags):
    """
    Elimina etiquetas de los ficheros que cumplen con la consulta.
    """
    conn, cursor = get_connection()
    files = query_files(query_tags)

    for file_id, name, _ in files:
        for tag in del_tags:
            # Buscar el id de la etiqueta
            cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
            row = cursor.fetchone()
            if row:
                tag_id = row[0]
                # Eliminar la relación en file_tags
                cursor.execute(
                    "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
                    (file_id, tag_id)
                )
        print(f"[INFO] Etiquetas eliminadas de {name}")

    conn.commit()
    close_connection(conn)
