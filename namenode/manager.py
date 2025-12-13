"""
Manager del MetaNameNode - Maneja solo metadatos (sin almacenamiento físico)
Los archivos físicos se almacenan en DataNodes, no aquí.
"""
import os
import sqlite3
import time
from typing import List, Optional, Tuple, Dict
from namenode.database import get_connection, close_connection, get_db_path, db_lock


def get_or_create_tag(tag: str, user_id: str, cursor, conn) -> Optional[int]:
    """
    Obtiene o crea una etiqueta para el usuario especificado.
    Asegura que cada etiqueta pertenezca a un usuario (aislamiento estricto).
    
    Returns: tag_id o None si hay error
    """
    tag = tag.strip()
    if not tag:
        return None
    
    try:
        # Buscar etiqueta existente para este usuario
        cursor.execute(
            "SELECT id FROM tags WHERE tag = ? AND user_id = ?",
            (tag, user_id)
        )
        row = cursor.fetchone()
        
        if row:
            return row[0]
        else:
            # Crear nueva etiqueta para este usuario
            cursor.execute(
                "INSERT INTO tags (tag, user_id) VALUES (?, ?)",
                (tag, user_id)
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        # Puede haber un conflicto de UNIQUE, intentar obtener de nuevo
        cursor.execute(
            "SELECT id FROM tags WHERE tag = ? AND user_id = ?",
            (tag, user_id)
        )
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"[ERROR] Error obteniendo/creando etiqueta '{tag}' para usuario '{user_id}': {e}")
        return None


def validate_file_user(file_id: int, user_id: str, cursor) -> bool:
    """
    Valida que un archivo pertenezca al usuario especificado.
    
    Returns: True si el archivo pertenece al usuario, False en caso contrario
    """
    try:
        cursor.execute("SELECT user_id FROM files WHERE id = ?", (file_id,))
        row = cursor.fetchone()
        return row and row[0] == user_id
    except Exception:
        return False


def update_file_version(file_id: int, term: int, cursor, conn, node_id: str = None) -> bool:
    """
    Actualiza los campos de versionado de un archivo.
    Incrementa la versión y actualiza term y timestamp.
    
    Args:
        file_id: ID del archivo
        term: Término del namenode que hace la modificación
        cursor: Cursor de la base de datos
        conn: Conexión de la base de datos
        node_id: ID del nodo (opcional, para logging)
    
    Returns: True si se actualizó correctamente, False en caso contrario
    """
    try:
        # Obtener versión actual
        cursor.execute("SELECT version FROM files WHERE id = ?", (file_id,))
        row = cursor.fetchone()
        if not row:
            return False
        
        current_version = row[0] or 1
        new_version = current_version + 1
        timestamp = time.time()
        
        # Actualizar campos de versionado
        cursor.execute("""
            UPDATE files 
            SET version = ?, 
                last_modified_term = ?, 
                last_modified_timestamp = ?
            WHERE id = ?
        """, (new_version, term, timestamp, file_id))
        
        return True
    except Exception as e:
        print(f"[ERROR] Error actualizando versión del archivo {file_id}: {e}")
        return False


def add_file_metadata(name: str, tags: List[str], size: Optional[int] = None, 
                     hash_value: Optional[str] = None, node_id: str = None,
                     user_id: str = None, term: int = 0) -> Optional[int]:
    """
    Agrega metadatos de un archivo al sistema.
    NO almacena el archivo físico, solo los metadatos.
    
    Args:
        user_id: ID del usuario propietario del archivo (requerido)
        term: Término del namenode que hace la modificación (para versionado)
    
    Returns: file_id si se agregó correctamente, None en caso contrario
    """
    if not tags:
        print("[ERROR] No se pueden agregar archivos sin etiquetas.")
        return None
    
    if not user_id:
        print("[ERROR] Se requiere user_id para agregar archivos.")
        return None
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        # Verificar que la columna last_modified_timestamp existe, si no, agregarla
        try:
            cursor.execute("PRAGMA table_info(files)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'last_modified_timestamp' not in columns:
                print("[MANAGER] Columna last_modified_timestamp no existe, agregándola...")
                cursor.execute("ALTER TABLE files ADD COLUMN last_modified_timestamp REAL")
                cursor.execute("UPDATE files SET last_modified_timestamp = ? WHERE last_modified_timestamp IS NULL", (time.time(),))
                conn.commit()
                print("[MANAGER] Columna last_modified_timestamp agregada exitosamente")
        except Exception as e:
            print(f"[MANAGER] Error verificando/agregando columna last_modified_timestamp: {e}")
            # Continuar de todas formas, el error se manejará más adelante
        
        try:
            # Verificar si ya existe para este usuario
            cursor.execute("SELECT id FROM files WHERE user_id = ? AND name = ?", (user_id, name))
            row = cursor.fetchone()
            if row:
                # Archivo existe para este usuario, verificar si tiene las mismas etiquetas
                existing_file_id = row[0]
                # Solo obtener etiquetas del mismo usuario (aislamiento estricto)
                cursor.execute("""
                    SELECT t.tag 
                    FROM tags t
                    JOIN file_tags ft ON t.id = ft.tag_id
                    WHERE ft.file_id = ? AND t.user_id = ?
                    ORDER BY t.tag
                """, (existing_file_id, user_id))
                existing_tags = {row[0] for row in cursor.fetchall()}
                new_tags_set = {tag.strip() for tag in tags if tag.strip()}
                
                if existing_tags == new_tags_set:
                    # Mismo nombre, mismo usuario, mismas etiquetas -> actualizar metadatos
                    print(f"[WARNING] El archivo '{name}' ya existe para el usuario '{user_id}' con las mismas etiquetas. Actualizando metadatos...")
                    cursor.execute(
                        "UPDATE files SET size = ?, hash = ? WHERE id = ?",
                        (size, hash_value, existing_file_id)
                    )
                    file_id = existing_file_id
                    # Actualizar versionado
                    update_file_version(existing_file_id, term, cursor, conn, node_id)
                else:
                    # Mismo nombre, mismo usuario, diferentes etiquetas -> permitir (actualizar etiquetas)
                    print(f"[INFO] Archivo '{name}' existe para el usuario '{user_id}' pero con diferentes etiquetas. Actualizando etiquetas...")
                    # Eliminar etiquetas antiguas
                    cursor.execute("DELETE FROM file_tags WHERE file_id = ?", (existing_file_id,))
                    # Actualizar metadatos
                    cursor.execute(
                        "UPDATE files SET size = ?, hash = ? WHERE id = ?",
                        (size, hash_value, existing_file_id)
                    )
                    file_id = existing_file_id
                    # Actualizar versionado
                    update_file_version(existing_file_id, term, cursor, conn, node_id)
            else:
                # Insertar archivo nuevo (solo metadatos)
                try:
                    timestamp = time.time()
                    cursor.execute(
                        "INSERT INTO files (name, size, hash, user_id, version, last_modified_term, last_modified_timestamp) VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (name, size, hash_value, user_id, term, timestamp)
                    )
                    file_id = cursor.lastrowid
                except sqlite3.IntegrityError as e:
                    # Puede haber una restricción UNIQUE antigua solo en 'name'
                    if "UNIQUE constraint" in str(e) and "name" in str(e):
                        print(f"[WARNING] Conflicto de UNIQUE constraint en 'name'. Verificando archivo existente...")
                        # Buscar archivo existente (puede ser de otro usuario o sin user_id)
                        cursor.execute("SELECT id, user_id FROM files WHERE name = ?", (name,))
                        existing = cursor.fetchone()
                        if existing:
                            existing_id, existing_user = existing[0], existing[1]
                            if existing_user == user_id or existing_user is None or existing_user == 'system':
                                # Es del mismo usuario o sin user_id -> actualizar
                                print(f"[INFO] Actualizando archivo existente (id={existing_id}) con user_id='{user_id}'")
                                cursor.execute(
                                    "UPDATE files SET size = ?, hash = ?, user_id = ? WHERE id = ?",
                                    (size, hash_value, user_id, existing_id)
                                )
                                file_id = existing_id
                                # Actualizar versionado
                                update_file_version(existing_id, term, cursor, conn, node_id)
                            else:
                                # Es de otro usuario -> permitir (diferente usuario puede tener mismo nombre)
                                # Pero hay una restricción UNIQUE antigua solo en 'name' que lo impide
                                # En este caso, retornar None para que el endpoint maneje el error
                                print(f"[ERROR] Conflicto: archivo '{name}' existe para usuario '{existing_user}', pero restricción UNIQUE antigua impide agregarlo para usuario '{user_id}'")
                                return None
                        else:
                            raise
                    else:
                        raise
            
            # Insertar etiquetas y relaciones (cada etiqueta pertenece al usuario del archivo)
            for tag in tags:
                tag_id = get_or_create_tag(tag, user_id, cursor, conn)
                if tag_id:
                    cursor.execute(
                        "INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                        (file_id, tag_id)
                    )
                else:
                    print(f"[WARNING] No se pudo crear/obtener etiqueta '{tag}' para usuario '{user_id}'")
            
            conn.commit()
            print(f"[INFO] Metadatos de '{name}' agregados con etiquetas: {', '.join(tags)}")
            return file_id
            
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al agregar metadatos: {e}")
            return None
        finally:
            close_connection(conn)


def query_files(query_tags: Optional[List[str]] = None, node_id: str = None,
                user_id: str = None) -> List[Tuple[int, str, str]]:
    """
    Devuelve lista de tuplas (id, name, tags_concat) que cumplen la consulta.
    - query_tags: lista de etiquetas (AND). Si None o vacía -> devuelve todo.
    - user_id: ID del usuario. Si None, devuelve archivos de todos los usuarios (solo admin).
    Returns: Lista de (id, name, tags)
    """
    if query_tags is None:
        query_tags = []
    
    if not user_id:
        print(f"[WARNING] query_files llamado sin user_id. Solo admin debería hacer esto. query_tags={query_tags}")
    else:
        print(f"[DEBUG] query_files llamado con user_id='{user_id}', query_tags={query_tags}")
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            if not query_tags:
                if user_id:
                    # Incluir archivos del usuario Y archivos legacy (system o NULL) si el usuario es admin
                    # Para usuarios normales, solo mostrar sus propios archivos
                    if user_id == "admin":
                        # Admin puede ver archivos de todos los usuarios, pero etiquetas deben ser del usuario del archivo
                        cursor.execute("""
                            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
                            FROM files f
                            LEFT JOIN file_tags ft ON f.id = ft.file_id
                            LEFT JOIN tags t ON ft.tag_id = t.id AND t.user_id = f.user_id
                            WHERE f.user_id = 'admin' OR f.user_id = 'system' OR f.user_id IS NULL
                            GROUP BY f.id
                            ORDER BY f.id
                        """)
                    else:
                        # Usuario normal: solo sus archivos y sus etiquetas
                        cursor.execute("""
                            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
                            FROM files f
                            LEFT JOIN file_tags ft ON f.id = ft.file_id
                            LEFT JOIN tags t ON ft.tag_id = t.id AND t.user_id = ?
                            WHERE f.user_id = ?
                            GROUP BY f.id
                            ORDER BY f.id
                        """, (user_id, user_id))
                else:
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
                # Validar que user_id no sea una cadena vacía
                if user_id and user_id.strip():
                    # Incluir archivos del usuario Y archivos legacy (system o NULL) si el usuario es admin
                    if user_id == "admin":
                        # Admin puede buscar en archivos de todos los usuarios, pero etiquetas deben coincidir con el usuario del archivo
                        sql = f"""
                            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
                            FROM files f
                            JOIN file_tags ft ON f.id = ft.file_id
                            JOIN tags t ON ft.tag_id = t.id AND t.user_id = f.user_id
                            WHERE (f.user_id = 'admin' OR f.user_id = 'system' OR f.user_id IS NULL) AND t.tag IN ({placeholders})
                            GROUP BY f.id
                            HAVING COUNT(DISTINCT t.tag) = ?
                            ORDER BY f.id
                        """
                        params = (*query_tags, len(query_tags))
                        print(f"[DEBUG] Admin query: {len(query_tags)} tags, {len(params)} params, SQL placeholders: {sql.count('?')}")
                        cursor.execute(sql, params)
                    else:
                        # Usuario normal: solo sus archivos y sus etiquetas
                        sql = f"""
                            SELECT f.id, f.name, GROUP_CONCAT(DISTINCT t.tag) as tags
                            FROM files f
                            JOIN file_tags ft ON f.id = ft.file_id
                            JOIN tags t ON ft.tag_id = t.id AND t.user_id = ?
                            WHERE f.user_id = ? AND t.tag IN ({placeholders})
                            GROUP BY f.id
                            HAVING COUNT(DISTINCT t.tag) = ?
                            ORDER BY f.id
                        """
                        params = (user_id, user_id, *query_tags, len(query_tags))
                        print(f"[DEBUG] User query: user_id={user_id}, {len(query_tags)} tags, {len(params)} params, SQL placeholders: {sql.count('?')}")
                        cursor.execute(sql, params)
                else:
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
                    params = (*query_tags, len(query_tags))
                    print(f"[DEBUG] No user_id query: {len(query_tags)} tags, {len(params)} params, SQL placeholders: {sql.count('?')}")
                    cursor.execute(sql, params)
                results = cursor.fetchall()
            
            return [(row[0], row[1], row[2] or "") for row in results]
            
        finally:
            close_connection(conn)


def get_file_by_id(file_id: int, node_id: str = None, user_id: str = None) -> Optional[Dict]:
    """
    Obtiene los metadatos de un archivo por su ID.
    Si se proporciona user_id, verifica que el archivo pertenezca al usuario.
    Returns: Dict con {id, name, tags, size, hash, user_id} o None
    """
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            if user_id:
                cursor.execute("SELECT id, name, size, hash, user_id FROM files WHERE id = ? AND user_id = ?", 
                             (file_id, user_id))
            else:
                cursor.execute("SELECT id, name, size, hash, user_id FROM files WHERE id = ?", (file_id,))
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
                "user_id": row[4] if len(row) > 4 else None,
                "tags": tags
            }
        finally:
            close_connection(conn)


def delete_file_metadata(file_id: int, node_id: str = None, user_id: str = None, term: int = 0) -> bool:
    """
    Elimina los metadatos de un archivo y el archivo físico de los DataNodes.
    Args:
        term: Término del namenode que hace la modificación (para versionado)
    Returns: True si se eliminó, False si no existía
    """
    db_path = get_db_path(node_id)
    
    # Primero obtener la información del archivo (con bloqueo mínimo)
    file_name = None
    file_hash = None
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            if user_id:
                cursor.execute("SELECT name, hash FROM files WHERE id = ? AND user_id = ?", (file_id, user_id))
            else:
                cursor.execute("SELECT name, hash FROM files WHERE id = ?", (file_id,))
            row = cursor.fetchone()
            if not row:
                return False
            
            file_name = row[0]
            hash_value = row[1]
            
            # Extraer hash sin prefijo "sha256:"
            file_hash = hash_value[7:] if hash_value.startswith("sha256:") else hash_value
        finally:
            close_connection(conn)
    
    # Eliminar archivo de DataNodes ANTES de eliminar metadatos (fuera del bloqueo)
    # Esto evita mantener el bloqueo de la BD durante las peticiones HTTP
    from namenode.datanode_manager import delete_file_from_datanodes
    print(f"[MANAGER] Eliminando archivo físico {file_hash} de DataNodes...")
    delete_file_from_datanodes(file_hash, file_id, node_id_db=node_id)
    
    # Ahora eliminar metadatos (con bloqueo)
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            # Obtener user_id del archivo antes de eliminarlo
            cursor.execute("SELECT user_id FROM files WHERE id = ?", (file_id,))
            file_user_row = cursor.fetchone()
            if not file_user_row:
                print(f"[MANAGER] Archivo {file_id} ya no existe en metadatos")
                return False
            
            file_user_id = file_user_row[0]
            
            # Las relaciones se eliminan por CASCADE
            cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
            
            # Limpiar etiquetas huérfanas del usuario (etiquetas que no tienen ningún archivo asociado)
            if file_user_id:
                cursor.execute("""
                    DELETE FROM tags 
                    WHERE user_id = ? 
                    AND id NOT IN (SELECT DISTINCT tag_id FROM file_tags WHERE tag_id IS NOT NULL)
                """, (file_user_id,))
                orphan_tags_deleted = cursor.rowcount
                if orphan_tags_deleted > 0:
                    print(f"[INFO] Eliminadas {orphan_tags_deleted} etiquetas huérfanas del usuario '{file_user_id}'")
            
            conn.commit()
            print(f"[INFO] Metadatos eliminados: {file_name}")
            return True
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al eliminar metadatos: {e}")
            return False
        finally:
            close_connection(conn)


def delete_files_by_tags(query_tags: List[str], node_id: str = None, user_id: str = None, term: int = 0) -> bool:
    """
    Elimina metadatos de archivos que cumplen la query (por etiquetas).
    Args:
        term: Término del namenode que hace la modificación (para versionado)
    Returns: True si se eliminó al menos un archivo, False si no hubo coincidencias.
    """
    if not query_tags:
        print("[ERROR] delete_files_by_tags requiere una query de etiquetas.")
        return False
    
    files = query_files(query_tags, node_id=node_id, user_id=user_id)
    if not files:
        return False
    
    db_path = get_db_path(node_id)
    deleted_count = 0
    
    # Eliminar archivos
    for file_id, name, _ in files:
        if delete_file_metadata(file_id, node_id=node_id, user_id=user_id, term=term):
            deleted_count += 1
    
    # Limpiar etiquetas huérfanas después de eliminar archivos
    # (delete_file_metadata ya limpia, pero hacemos una limpieza final por si acaso)
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            cursor.execute("""
                DELETE FROM tags 
                WHERE id NOT IN (SELECT DISTINCT tag_id FROM file_tags WHERE tag_id IS NOT NULL)
            """)
            orphan_tags_deleted = cursor.rowcount
            if orphan_tags_deleted > 0:
                print(f"[INFO] Limpieza final: eliminadas {orphan_tags_deleted} etiquetas huérfanas")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error en limpieza de etiquetas huérfanas: {e}")
        finally:
            close_connection(conn)
    
    return deleted_count > 0


def add_tags_to_files(query_tags: List[str], new_tags: List[str], node_id: str = None, user_id: str = None, term: int = 0) -> bool:
    """
    Añade etiquetas new_tags a todos los archivos del usuario que cumplen query_tags.
    Solo afecta a archivos del usuario especificado (aislamiento estricto).
    
    Args:
        query_tags: Etiquetas para buscar archivos
        new_tags: Nuevas etiquetas a agregar
        node_id: ID del nodo
        user_id: ID del usuario (requerido)
        term: Término del namenode que hace la modificación (para versionado)
    
    Returns: True si se agregó al menos a un archivo, False si no hubo coincidencias.
    """
    if not user_id:
        print("[ERROR] Se requiere user_id para agregar etiquetas")
        return False
    
    # Buscar archivos del usuario que cumplen la query
    files = query_files(query_tags, node_id=node_id, user_id=user_id)
    if not files:
        return False
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            affected = 0
            for file_id, name, _ in files:
                # Validar que el archivo pertenece al usuario
                if not validate_file_user(file_id, user_id, cursor):
                    print(f"[WARNING] Archivo '{name}' (id={file_id}) no pertenece al usuario '{user_id}', saltando...")
                    continue
                
                for tag in new_tags:
                    tag = tag.strip()
                    if not tag:
                        continue
                    
                    # Crear o obtener etiqueta del usuario
                    tag_id = get_or_create_tag(tag, user_id, cursor, conn)
                    if tag_id:
                        cursor.execute(
                            "INSERT OR IGNORE INTO file_tags (file_id, tag_id) VALUES (?, ?)",
                            (file_id, tag_id)
                        )
                        if cursor.rowcount > 0:
                            affected += 1
                    else:
                        print(f"[WARNING] No se pudo crear/obtener etiqueta '{tag}' para usuario '{user_id}'")
                
                # Actualizar versionado del archivo si se agregaron etiquetas
                if affected > 0:
                    update_file_version(file_id, term, cursor, conn, node_id)
                
                print(f"[INFO] Etiquetas agregadas a {name} (usuario: {user_id})")
            
            conn.commit()
            return affected > 0
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al agregar etiquetas: {e}")
            return False
        finally:
            close_connection(conn)


def delete_tags_from_files(query_tags: List[str], del_tags: List[str], node_id: str = None, user_id: str = None, term: int = 0) -> bool:
    """
    Elimina las etiquetas del_tags de los archivos del usuario que cumplen query_tags.
    Solo afecta a archivos del usuario especificado (aislamiento estricto).
    No elimina etiquetas si el archivo quedaría sin ninguna.
    
    Args:
        query_tags: Etiquetas para buscar archivos
        del_tags: Etiquetas a eliminar
        node_id: ID del nodo
        user_id: ID del usuario (requerido)
        term: Término del namenode que hace la modificación (para versionado)
    
    Returns: True si al menos una relación fue eliminada, False si no hubo coincidencias.
    """
    if not user_id:
        print("[ERROR] Se requiere user_id para eliminar etiquetas")
        return False
    
    # Buscar archivos del usuario que cumplen la query
    files = query_files(query_tags, node_id=node_id, user_id=user_id)
    if not files:
        return False
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        
        try:
            total_deleted = 0
            
            for file_id, name, _ in files:
                # Validar que el archivo pertenece al usuario
                if not validate_file_user(file_id, user_id, cursor):
                    print(f"[WARNING] Archivo '{name}' (id={file_id}) no pertenece al usuario '{user_id}', saltando...")
                    continue
                
                # Contar cuántas etiquetas del usuario tiene actualmente
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM file_tags ft
                    JOIN tags t ON ft.tag_id = t.id
                    WHERE ft.file_id = ? AND t.user_id = ?
                """, (file_id, user_id))
                tag_count = cursor.fetchone()[0]
                
                for tag in del_tags:
                    tag = tag.strip()
                    if not tag:
                        continue
                    
                    # Si solo queda una etiqueta, no eliminar más
                    if tag_count <= 1:
                        print(f"[WARN] No se puede eliminar la última etiqueta de '{name}'.")
                        break
                    
                    # Buscar etiqueta del usuario
                    cursor.execute("SELECT id FROM tags WHERE tag = ? AND user_id = ?", (tag, user_id))
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
                
                # Actualizar versionado del archivo si se eliminaron etiquetas
                if total_deleted > 0:
                    update_file_version(file_id, term, cursor, conn, node_id)
                
                print(f"[INFO] Etiquetas eliminadas de {name} (usuario: {user_id}, quedan {tag_count})")
            
            # Limpiar etiquetas huérfanas del usuario (etiquetas que no tienen ningún archivo asociado)
            cursor.execute("""
                DELETE FROM tags 
                WHERE user_id = ? 
                AND id NOT IN (SELECT DISTINCT tag_id FROM file_tags WHERE tag_id IS NOT NULL)
            """, (user_id,))
            orphan_tags_deleted = cursor.rowcount
            if orphan_tags_deleted > 0:
                print(f"[INFO] Limpieza final: eliminadas {orphan_tags_deleted} etiquetas huérfanas del usuario '{user_id}'")
            
            conn.commit()
            return total_deleted > 0
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] Error al eliminar etiquetas: {e}")
            return False
        finally:
            close_connection(conn)

