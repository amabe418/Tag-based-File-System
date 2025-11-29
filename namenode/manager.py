"""
Manager del MetaNameNode - Maneja solo metadatos (sin almacenamiento físico)
Los archivos físicos se almacenan en DataNodes, no aquí.
"""
import os
from typing import List, Optional, Tuple, Dict
from namenode.database import get_connection, close_connection, get_db_path, db_lock

def add_file_metadata(name: str, tags: List[str], size: Optional[int] = None, 
                     hash_value: Optional[str] = None, node_id: str = None) -> Optional[int]:
    """
    Agrega metadatos de un archivo al sistema.
    NO almacena el archivo físico, solo los metadatos.
    
    Returns: file_id si se agregó correctamente, None en caso contrario
    """
    if not tags:
        print("[ERROR] No se pueden agregar archivos sin etiquetas.")
        return None
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            # Verificar si ya existe
            cursor.execute("SELECT id FROM files WHERE name = ?", (name,))
            row = cursor.fetchone()
            if row:
                print(f"[WARNING] El archivo '{name}' ya existe en la base de datos.")
                file_id = row[0]
            else:
                # Insertar archivo (solo metadatos)
                cursor.execute(
                    "INSERT INTO files (name, size, hash) VALUES (?, ?, ?)",
                    (name, size, hash_value)
                )
                file_id = cursor.lastrowid
            
            # Insertar etiquetas y relaciones
            for tag in tags:
                tag = tag.strip()
                if not tag:
                    continue
                cursor.execute("INSERT OR IGNORE INTO tags (tag) VALUES (?)", (tag,))
                cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
                tag_row = cursor.fetchone()
                if tag_row:
                    tag_id = tag_row[0]
                    cursor.execute(
                        "INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                        (file_id, tag_id)
                    )
            
            conn.commit()
            print(f"[INFO] Metadatos de '{name}' agregados con etiquetas: {', '.join(tags)}")
            return file_id
            
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al agregar metadatos: {e}")
            return None
        finally:
            close_connection(conn)


def query_files(query_tags: Optional[List[str]] = None, node_id: str = None) -> List[Tuple[int, str, str]]:
    """
    Devuelve lista de tuplas (id, name, tags_concat) que cumplen la consulta.
    - query_tags: lista de etiquetas (AND). Si None o vacía -> devuelve todo.
    Returns: Lista de (id, name, tags)
    """
    if query_tags is None:
        query_tags = []
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            if not query_tags:
                cursor.execute("""
                    SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
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
                    SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
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
            
            return [(row[0], row[1], row[2] or "") for row in results]
            
        finally:
            close_connection(conn)


def get_file_by_id(file_id: int, node_id: str = None) -> Optional[Dict]:
    """
    Obtiene los metadatos de un archivo por su ID.
    Returns: Dict con {id, name, tags, size, hash} o None
    """
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            cursor.execute("SELECT id, name, size, hash FROM files WHERE id = ?", (file_id,))
            row = cursor.fetchone()
            if not row:
                return None
            
            # Obtener tags
            cursor.execute("""
                SELECT t.tag
                FROM tags t
                JOIN file_tags ft ON t.id = ft.tag_id
                WHERE ft.file_id = ?
            """, (file_id,))
            tags = [tag_row[0] for tag_row in cursor.fetchall()]
            
            return {
                "id": row[0],
                "name": row[1],
                "size": row[2],
                "hash": row[3],
                "tags": tags
            }
        finally:
            close_connection(conn)


def delete_file_metadata(file_id: int, node_id: str = None) -> bool:
    """
    Elimina los metadatos de un archivo.
    Returns: True si se eliminó, False si no existía
    """
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            cursor.execute("SELECT name FROM files WHERE id = ?", (file_id,))
            row = cursor.fetchone()
            if not row:
                return False
            
            # Las relaciones se eliminan por CASCADE
            cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
            print(f"[INFO] Metadatos eliminados: {row[0]}")
            return True
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al eliminar metadatos: {e}")
            return False
        finally:
            close_connection(conn)


def delete_files_by_tags(query_tags: List[str], node_id: str = None) -> bool:
    """
    Elimina metadatos de archivos que cumplen la query (por etiquetas).
    Returns: True si se eliminó al menos un archivo, False si no hubo coincidencias.
    """
    if not query_tags:
        print("[ERROR] delete_files_by_tags requiere una query de etiquetas.")
        return False
    
    files = query_files(query_tags, node_id=node_id)
    if not files:
        return False
    
    deleted_count = 0
    for file_id, name, _ in files:
        if delete_file_metadata(file_id, node_id=node_id):
            deleted_count += 1
    
    return deleted_count > 0


def add_tags_to_files(query_tags: List[str], new_tags: List[str], node_id: str = None) -> bool:
    """
    Añade etiquetas new_tags a todos los archivos que cumplen query_tags.
    Returns: True si se agregó al menos a un archivo, False si no hubo coincidencias.
    """
    files = query_files(query_tags, node_id=node_id)
    if not files:
        return False
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            affected = 0
            for file_id, name, _ in files:
                for tag in new_tags:
                    tag = tag.strip()
                    if not tag:
                        continue
                    cursor.execute("INSERT OR IGNORE INTO tags (tag) VALUES (?)", (tag,))
                    cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
                    tag_row = cursor.fetchone()
                    if tag_row:
                        tag_id = tag_row[0]
                        cursor.execute(
                            "INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                            (file_id, tag_id)
                        )
                affected += 1
                print(f"[INFO] Etiquetas agregadas a {name}")
            
            conn.commit()
            return affected > 0
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al agregar etiquetas: {e}")
            return False
        finally:
            close_connection(conn)


def delete_tags_from_files(query_tags: List[str], del_tags: List[str], node_id: str = None) -> bool:
    """
    Elimina las etiquetas del_tags de los archivos que cumplen query_tags.
    No elimina etiquetas si el archivo quedaría sin ninguna.
    Returns: True si al menos una relación fue eliminada, False si no hubo coincidencias.
    """
    files = query_files(query_tags, node_id=node_id)
    if not files:
        return False
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            total_deleted = 0
            
            for file_id, name, _ in files:
                # Contar cuántas etiquetas tiene actualmente
                cursor.execute("SELECT COUNT(*) FROM file_tags WHERE file_id = ?", (file_id,))
                tag_count = cursor.fetchone()[0]
                
                for tag in del_tags:
                    tag = tag.strip()
                    if not tag:
                        continue
                    
                    # Si solo queda una etiqueta, no eliminar más
                    if tag_count <= 1:
                        print(f"[WARN] No se puede eliminar la última etiqueta de '{name}'.")
                        break
                    
                    cursor.execute("SELECT id FROM tags WHERE tag = ?", (tag,))
                    tag_row = cursor.fetchone()
                    if not tag_row:
                        continue
                    
                    tag_id = tag_row[0]
                    cursor.execute(
                        "DELETE FROM file_tags WHERE file_id = ? AND tag_id = ?",
                        (file_id, tag_id)
                    )
                    if cursor.rowcount > 0:
                        total_deleted += cursor.rowcount
                        tag_count -= 1
                
                print(f"[INFO] Etiquetas eliminadas de {name} (quedan {tag_count})")
            
            conn.commit()
            return total_deleted > 0
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al eliminar etiquetas: {e}")
            return False
        finally:
            close_connection(conn)

