import os
from core.database import get_connection, close_connection

def add_files(file_list, tag_list):
    """
    Agrega ficheros y sus etiquetas al sistema.
    """
    conn, cursor = get_connection()

    for file_name in file_list:
        print(file_name)
        # if not os.path.exists(file_name):
        #     print(f"[WARNING] El fichero {file_name} no existe, se ignora.")
        #     continue

        # Insertar fichero si no existe
        cursor.execute("INSERT OR IGNORE INTO files (name) VALUES (?)", (file_name,))
        cursor.execute("SELECT id FROM files WHERE name = ?", (file_name,))
        file_id = cursor.fetchone()[0]
        print("ya meti el archivo")

        # Insertar etiquetas
        for tag in tag_list:
            if " " in tag:
                print(f"[WARNING] Etiqueta inválida '{tag}', se reemplazó por '{tag.replace(' ', '_')}'")
                tag = tag.replace(" ", "_")
            cursor.execute("INSERT OR IGNORE INTO tags (file_id, tag) VALUES (?, ?)", (file_id, tag))
            print("ya meti las etiquetas")
    conn.commit()
    close_connection(conn)
    print("[INFO] Archivos agregados correctamente.")


def query_files(query_tags):
    """
    Devuelve todos los ficheros que cumplen con las etiquetas dadas.
    Si query_tags está vacío, devuelve todos los ficheros.
    """
    conn, cursor = get_connection()

    if not query_tags:
        cursor.execute("""
            SELECT f.id, f.name, GROUP_CONCAT(t.tag)
            FROM files f
            LEFT JOIN tags t ON f.id = t.file_id
            GROUP BY f.id
        """)
    else:
        placeholders = ",".join("?" for _ in query_tags)
        sql = f"""
        SELECT f.id, f.name, GROUP_CONCAT(t.tag)
        FROM files f
        JOIN tags t ON f.id = t.file_id
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


# def delete_files(query_tags):
#     """
#     Elimina los ficheros que cumplen con la consulta.
#     """
#     conn, cursor = get_connection()
#     files = query_files(query_tags)

#     for file_id, name, _ in files:
#         cursor.execute("DELETE FROM tags WHERE file_id = ?", (file_id,))
#         cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
#         print(f"[INFO] Eliminado: {name}")

#     conn.commit()
#     close_connection(conn)
#     print(f"[INFO] {len(files)} archivos eliminados.")


# def add_tags(query_tags, new_tags):
#     """
#     Añade etiquetas a los ficheros que cumplen con la consulta.
#     """
#     conn, cursor = get_connection()
#     files = query_files(query_tags)

#     for file_id, name, _ in files:
#         for tag in new_tags:
#             cursor.execute("INSERT OR IGNORE INTO tags (file_id, tag) VALUES (?, ?)", (file_id, tag))
#         print(f"[INFO] Etiquetas agregadas a {name}")

#     conn.commit()
#     close_connection(conn)


# def delete_tags(query_tags, del_tags):
#     """
#     Elimina etiquetas de los ficheros que cumplen con la consulta.
#     """
#     conn, cursor = get_connection()
#     files = query_files(query_tags)

#     for file_id, name, _ in files:
#         for tag in del_tags:
#             cursor.execute("DELETE FROM tags WHERE file_id = ? AND tag = ?", (file_id, tag))
#         print(f"[INFO] Etiquetas eliminadas de {name}")

#     conn.commit()
#     close_connection(conn)
