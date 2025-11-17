# core/utils.py

def parse_tag_query(tag_query: str) -> list[str]:
    """
    Convierte un string de consulta en una lista de etiquetas normalizadas.
    Ej: " tag1   Tag2 " -> ["tag1", "tag2"]
    """
    if not tag_query.strip():
        return []
    return list(set(tag.strip().lower() for tag in tag_query.split() if tag.strip()))


def build_query_condition(tags: list[str]) -> str:
    """
    Construye la condición SQL para buscar ficheros que contengan TODAS las etiquetas.
    Retorna una subconsulta SQL reutilizable.
    """
    if not tags:
        return "1=1"  # si no hay etiquetas, devuelve todo
    
    # Subconsulta: file_ids que contienen todas las etiquetas
    conditions = []
    for tag in tags:
        conditions.append(f"""
            file_id IN (
                SELECT file_id
                FROM file_tags ft
                JOIN tags t ON ft.tag_id = t.id
                WHERE t.name = '{tag}'
            )
        """)
    
    return " AND ".join(conditions)


def get_matching_files(conn, tags: list[str]) -> list[tuple]:
    """
    Devuelve los ficheros (id, name) que cumplen una lista de etiquetas.
    """
    sql = f"SELECT DISTINCT id, name FROM files WHERE {build_query_condition(tags)}"
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()
