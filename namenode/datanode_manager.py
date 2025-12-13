"""
Gestión de DataNodes en el MetaNameNode
Maneja registro, heartbeats, asignación de réplicas y detección de inactivos
"""
import time
import requests
from typing import List, Optional, Dict, Tuple
from namenode.database import get_db_path, get_connection, close_connection, db_lock


def register_datanode(node_id: str, url: str, port: int, ip: Optional[str], 
                      total_space: int, free_space: int, node_id_db: str = None) -> bool:
    """
    Registra un nuevo DataNode o actualiza uno existente.
    
    Returns:
        True si se registró correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            current_time = time.time()
            
            # Verificar si ya existe
            cursor.execute("SELECT node_id FROM datanodes WHERE node_id = ?", (node_id,))
            exists = cursor.fetchone()
            
            if exists:
                # Actualizar DataNode existente (preservar estado de draining si existe)
                cursor.execute("""
                    UPDATE datanodes 
                    SET url = ?, port = ?, ip = ?, total_space = ?, free_space = ?, 
                        last_heartbeat = ?, status = 'active'
                    WHERE node_id = ?
                """, (url, port, ip, total_space, free_space, current_time, node_id))
            else:
                # Insertar nuevo DataNode
                cursor.execute("""
                    INSERT INTO datanodes (node_id, url, port, ip, total_space, free_space, last_heartbeat, status, draining)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0)
                """, (node_id, url, port, ip, total_space, free_space, current_time))
            
            conn.commit()
            print(f"[DATANODE_MANAGER] DataNode registrado/actualizado: {node_id} -> {url}:{port}")
            return True
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al registrar DataNode {node_id}: {e}")
            return False
        finally:
            close_connection(conn)


def update_datanode_heartbeat(node_id: str, free_space: int, total_space: int, 
                              node_id_db: str = None) -> bool:
    """
    Actualiza el heartbeat de un DataNode.
    
    Returns:
        True si se actualizó correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            current_time = time.time()
            
            cursor.execute("""
                UPDATE datanodes 
                SET free_space = ?, total_space = ?, last_heartbeat = ?, status = 'active'
                WHERE node_id = ?
            """, (free_space, total_space, current_time, node_id))
            
            if cursor.rowcount == 0:
                print(f"[DATANODE_MANAGER] DataNode {node_id} no encontrado para heartbeat")
                close_connection(conn)
                return False
            
            conn.commit()
            return True
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al actualizar heartbeat de {node_id}: {e}")
            return False
        finally:
            close_connection(conn)


def get_datanode(node_id: str, node_id_db: str = None) -> Optional[Dict]:
    """
    Obtiene información de un DataNode específico.
    
    Returns:
        Diccionario con información del DataNode, o None si no existe
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                SELECT node_id, url, port, ip, total_space, free_space, 
                       last_heartbeat, status, registered_at, draining
                FROM datanodes 
                WHERE node_id = ?
            """, (node_id,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            return {
                "node_id": row[0],
                "url": row[1],
                "port": row[2],
                "ip": row[3],
                "total_space": row[4],
                "free_space": row[5],
                "last_heartbeat": row[6],
                "status": row[7],
                "registered_at": row[8],
                "draining": bool(row[9]) if row[9] is not None else False
            }
        finally:
            close_connection(conn)


def list_datanodes(status: Optional[str] = None, node_id_db: str = None) -> List[Dict]:
    """
    Lista todos los DataNodes registrados.
    
    Args:
        status: Filtrar por status ('active', 'inactive'), None para todos
    
    Returns:
        Lista de diccionarios con información de DataNodes
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            if status:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes 
                    WHERE status = ?
                    ORDER BY node_id
                """, (status,))
            else:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes 
                    ORDER BY node_id
                """)
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "node_id": row[0],
                    "url": row[1],
                    "port": row[2],
                    "ip": row[3],
                    "total_space": row[4],
                    "free_space": row[5],
                    "last_heartbeat": row[6],
                    "status": row[7],
                    "registered_at": row[8],
                    "draining": bool(row[9]) if row[9] is not None else False
                })
            
            return results
        finally:
            close_connection(conn)


def get_active_datanodes(node_id_db: str = None, exclude_draining: bool = True) -> List[Dict]:
    """
    Obtiene lista de DataNodes activos ordenados por espacio libre (descendente).
    
    Args:
        exclude_draining: Si True, excluye DataNodes en proceso de drenaje
    
    Returns:
        Lista de DataNodes activos
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            if exclude_draining:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes 
                    WHERE status = 'active' AND (draining = 0 OR draining IS NULL)
                    ORDER BY free_space DESC
                """)
            else:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes 
                    WHERE status = 'active'
                    ORDER BY free_space DESC
                """)
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "node_id": row[0],
                    "url": row[1],
                    "port": row[2],
                    "ip": row[3],
                    "total_space": row[4],
                    "free_space": row[5],
                    "last_heartbeat": row[6],
                    "status": row[7],
                    "registered_at": row[8],
                    "draining": bool(row[9]) if row[9] is not None else False
                })
            
            return results
        finally:
            close_connection(conn)


def assign_replicas(file_hash: str, file_size: int, node_id_db: str = None, 
                    exclude_datanodes: List[str] = None) -> Optional[List[str]]:
    """
    Asigna DataNodes para almacenar réplicas de un archivo (idealmente 3, mínimo 1).
    Optimiza la selección considerando espacio disponible y excluyendo DataNodes en drenaje.
    
    Args:
        file_hash: Hash del archivo (sin prefijo "sha256:")
        file_size: Tamaño del archivo en bytes
        exclude_datanodes: Lista de DataNode IDs a excluir de la asignación
    
    Returns:
        Lista de DataNode IDs [primary, secondary, tertiary] (hasta 3, mínimo 1), 
        o None si no hay DataNodes disponibles
    """
    if exclude_datanodes is None:
        exclude_datanodes = []
    
    # Obtener DataNodes activos (excluyendo los que están en drenaje)
    active_datanodes = get_active_datanodes(node_id_db=node_id_db, exclude_draining=True)
    
    # Excluir DataNodes específicos
    active_datanodes = [dn for dn in active_datanodes if dn["node_id"] not in exclude_datanodes]
    
    if len(active_datanodes) < 1:
        print(f"[DATANODE_MANAGER] No hay DataNodes activos disponibles")
        return None
    
    # Filtrar DataNodes con espacio suficiente (con margen del 10% para seguridad)
    required_space = int(file_size * 1.1)  # 10% de margen
    available_datanodes = [
        dn for dn in active_datanodes 
        if dn["free_space"] >= required_space
    ]
    
    if len(available_datanodes) < 1:
        print(f"[DATANODE_MANAGER] No hay DataNodes con espacio disponible (requerido: {required_space} bytes)")
        return None
    
    # Ordenar por espacio libre (descendente) para balancear carga
    # Priorizar DataNodes con más espacio libre
    available_datanodes.sort(key=lambda x: x["free_space"], reverse=True)
    
    # Calcular slot basado en hash del archivo
    # Usar hash de Python para obtener un entero del string del hash
    slot = hash(file_hash) % len(available_datanodes)
    # Asegurar que el slot sea positivo
    if slot < 0:
        slot = -slot
    
    # Seleccionar DataNode primario (del slot calculado)
    primary = available_datanodes[slot]
    
    # Seleccionar réplicas adicionales (diferentes al primario)
    # Intentar obtener hasta 2 réplicas más (para un total de 3), pero aceptar las disponibles
    replicas = [dn for dn in available_datanodes if dn["node_id"] != primary["node_id"]][:2]
    
    # Construir lista de asignados: primario + réplicas (hasta 3 en total)
    assigned = [primary["node_id"]]
    for replica in replicas:
        assigned.append(replica["node_id"])
    
    replica_count = len(assigned)
    ideal_count = 3
    if replica_count < ideal_count:
        print(f"[DATANODE_MANAGER] Réplicas asignadas para {file_hash[:16]}...: {assigned} ({replica_count}/{ideal_count} - modo degradado)")
    else:
        print(f"[DATANODE_MANAGER] Réplicas asignadas para {file_hash[:16]}...: {assigned}")
    
    return assigned


def save_file_replicas(file_id: int, datanode_ids: List[str], node_id_db: str = None) -> bool:
    """
    Guarda la asignación de réplicas para un archivo.
    
    Args:
        file_id: ID del archivo en la base de datos
        datanode_ids: Lista de 3 DataNode IDs [primary, secondary, tertiary]
    
    Returns:
        True si se guardó correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            replica_types = ["primary", "secondary", "tertiary"]
            
            for i, datanode_id in enumerate(datanode_ids):
                if i < len(replica_types):
                    cursor.execute("""
                        INSERT OR REPLACE INTO file_replicas (file_id, datanode_id, replica_type)
                        VALUES (?, ?, ?)
                    """, (file_id, datanode_id, replica_types[i]))
            
            conn.commit()
            print(f"[DATANODE_MANAGER] Réplicas guardadas para file_id={file_id}: {datanode_ids}")
            return True
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al guardar réplicas: {e}")
            return False
        finally:
            close_connection(conn)


def get_file_replicas(file_id: int, node_id_db: str = None) -> List[Dict]:
    """
    Obtiene la lista de DataNodes donde está replicado un archivo.
    
    Returns:
        Lista de diccionarios con información de réplicas
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                SELECT fr.datanode_id, fr.replica_type, dn.url, dn.port, dn.status
                FROM file_replicas fr
                JOIN datanodes dn ON fr.datanode_id = dn.node_id
                WHERE fr.file_id = ?
                ORDER BY 
                    CASE fr.replica_type
                        WHEN 'primary' THEN 1
                        WHEN 'secondary' THEN 2
                        WHEN 'tertiary' THEN 3
                    END
            """, (file_id,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "datanode_id": row[0],
                    "replica_type": row[1],
                    "url": row[2],
                    "port": row[3],
                    "status": row[4]
                })
            
            return results
        finally:
            close_connection(conn)


def get_best_datanode_for_read(file_id: int, node_id_db: str = None) -> Optional[Dict]:
    """
    Obtiene el mejor DataNode para leer un archivo (balanceo de carga).
    Prioriza DataNodes activos y con menor carga.
    
    Args:
        file_id: ID del archivo en la base de datos
    
    Returns:
        Diccionario con información del DataNode y URL completa, o None si no hay réplicas disponibles
    """
    replicas = get_file_replicas(file_id, node_id_db=node_id_db)
    
    if not replicas:
        return None
    
    # Filtrar solo réplicas activas
    active_replicas = [r for r in replicas if r.get("status") == "active"]
    
    if not active_replicas:
        return None
    
    # Priorizar réplica primaria si está activa
    primary = next((r for r in active_replicas if r.get("replica_type") == "primary"), None)
    if primary:
        url = primary["url"]
        if not url.startswith("http"):
            url = f"http://{url}:{primary['port']}"
        return {
            "datanode_id": primary["datanode_id"],
            "url": url,
            "replica_type": primary["replica_type"]
        }
    
    # Si no hay primaria activa, usar la primera réplica activa disponible
    replica = active_replicas[0]
    url = replica["url"]
    if not url.startswith("http"):
        url = f"http://{url}:{replica['port']}"
    return {
        "datanode_id": replica["datanode_id"],
        "url": url,
        "replica_type": replica["replica_type"]
    }


def discover_file_replicas(file_hash: str, file_id: int, node_id_db: str = None) -> List[Dict]:
    """
    Descubre en qué DataNodes está almacenado un archivo consultando todos los DataNodes activos.
    Útil cuando la información de réplicas no está disponible (ej: después de cambio de líder).
    
    Args:
        file_hash: Hash del archivo (sin prefijo "sha256:")
        file_id: ID del archivo en la base de datos
    
    Returns:
        Lista de diccionarios con información de réplicas encontradas
    """
    print(f"[DATANODE_MANAGER] Descubriendo réplicas para file_id={file_id} consultando DataNodes...")
    
    active_datanodes = get_active_datanodes(node_id_db=node_id_db, exclude_draining=True)
    discovered_replicas = []
    
    for datanode in active_datanodes:
        datanode_id = datanode["node_id"]
        url = datanode["url"]
        if not url.startswith("http"):
            url = f"http://{url}:{datanode['port']}"
        
        try:
            # Obtener token de servicio para autenticación con DataNode
            import os
            try:
                from security.service_auth import generate_service_token
                service_token = generate_service_token(os.getenv("NODE_ID", "namenode-1"), "service")
            except Exception:
                service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
            
            # Intentar leer el archivo desde este DataNode
            response = requests.get(
                f"{url}/retrieve/{file_hash}",
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=5
            )
            if response.status_code == 200:
                print(f"[DATANODE_MANAGER] Archivo encontrado en {datanode_id}")
                discovered_replicas.append({
                    "datanode_id": datanode_id,
                    "url": url,
                    "port": datanode["port"],
                    "replica_type": "discovered",  # Tipo temporal hasta que se asigne
                    "status": "active"
                })
        except Exception as e:
            # El archivo no está en este DataNode o hay un error
            pass
    
    # Si se encontraron réplicas, guardarlas en la base de datos
    if discovered_replicas:
        print(f"[DATANODE_MANAGER] {len(discovered_replicas)} réplicas descubiertas, guardando en base de datos...")
        # Asignar tipos de réplica (primary, secondary, tertiary)
        replica_types = ["primary", "secondary", "tertiary"]
        datanode_ids = [r["datanode_id"] for r in discovered_replicas[:3]]  # Máximo 3
        
        if save_file_replicas(file_id, datanode_ids, node_id_db=node_id_db):
            # Actualizar la lista con los tipos correctos
            for i, replica in enumerate(discovered_replicas[:3]):
                if i < len(replica_types):
                    replica["replica_type"] = replica_types[i]
            print(f"[DATANODE_MANAGER] Réplicas guardadas: {datanode_ids}")
    
    return discovered_replicas


def get_all_replicas_for_read(file_id: int, node_id_db: str = None) -> List[Dict]:
    """
    Obtiene todas las réplicas activas de un archivo para lectura con fallback.
    
    Returns:
        Lista de diccionarios con información de réplicas activas, ordenadas por prioridad
    """
    replicas = get_file_replicas(file_id, node_id_db=node_id_db)
    
    if not replicas:
        return []
    
    # Filtrar solo réplicas activas y construir URLs
    active_replicas = []
    for r in replicas:
        if r.get("status") == "active":
            url = r["url"]
            if not url.startswith("http"):
                url = f"http://{url}:{r['port']}"
            active_replicas.append({
                "datanode_id": r["datanode_id"],
                "url": url,
                "replica_type": r["replica_type"]
            })
    
    # Ordenar por prioridad: primary, secondary, tertiary
    priority_order = {"primary": 1, "secondary": 2, "tertiary": 3}
    active_replicas.sort(key=lambda x: priority_order.get(x.get("replica_type", "tertiary"), 3))
    
    return active_replicas


def delete_file_from_datanodes(file_hash: str, file_id: int, node_id_db: str = None) -> bool:
    """
    Elimina un archivo de todos los DataNodes donde está replicado.
    
    Args:
        file_hash: Hash del archivo (sin prefijo "sha256:")
        file_id: ID del archivo en la base de datos
    
    Returns:
        True si se eliminó de al menos un DataNode, False si falló en todos
    """
    print(f"[DATANODE_MANAGER] Iniciando eliminación de archivo {file_hash[:16]}... (file_id={file_id})")
    replicas = get_file_replicas(file_id, node_id_db=node_id_db)
    
    if not replicas:
        print(f"[DATANODE_MANAGER] No hay réplicas registradas para file_id={file_id}")
        return False
    
    print(f"[DATANODE_MANAGER] Eliminando de {len(replicas)} DataNodes: {[r['datanode_id'] for r in replicas]}")
    success_count = 0
    failed_count = 0
    
    for replica in replicas:
        datanode_id = replica["datanode_id"]
        url = replica["url"]
        if not url.startswith("http"):
            url = f"http://{url}:{replica['port']}"
        
        print(f"[DATANODE_MANAGER] Eliminando de {datanode_id} ({url})...")
        try:
            # Obtener token de servicio para autenticación con DataNode
            import os
            try:
                from security.service_auth import generate_service_token
                service_token = generate_service_token(os.getenv("NODE_ID", "namenode-1"), "service")
            except Exception:
                service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
            
            response = requests.delete(
                f"{url}/delete/{file_hash}",
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=5  # Reducido a 5 segundos para evitar cuelgues
            )
            response.raise_for_status()
            print(f"[DATANODE_MANAGER] ✓ Archivo eliminado de {datanode_id}")
            success_count += 1
        except requests.Timeout:
            print(f"[DATANODE_MANAGER] ✗ Timeout eliminando de {datanode_id} (más de 5 segundos)")
            failed_count += 1
        except requests.RequestException as e:
            print(f"[DATANODE_MANAGER] ✗ Error eliminando de {datanode_id}: {e}")
            failed_count += 1
        except Exception as e:
            print(f"[DATANODE_MANAGER] ✗ Error inesperado eliminando de {datanode_id}: {e}")
            failed_count += 1
    
    print(f"[DATANODE_MANAGER] Eliminación completada: {success_count} exitosas, {failed_count} fallidas")
    return success_count > 0


def get_files_affected_by_datanode(datanode_id: str, node_id_db: str = None) -> List[int]:
    """
    Obtiene la lista de file_ids que tienen réplicas en un DataNode específico.
    
    Args:
        datanode_id: ID del DataNode que falló
    
    Returns:
        Lista de file_ids afectados
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                SELECT DISTINCT file_id 
                FROM file_replicas 
                WHERE datanode_id = ?
            """, (datanode_id,))
            
            return [row[0] for row in cursor.fetchall()]
        finally:
            close_connection(conn)


def rereplicate_file(file_id: int, file_hash: str, failed_datanode_id: str, 
                     node_id_db: str = None) -> bool:
    """
    Re-replica un archivo desde una réplica existente a un nuevo DataNode.
    
    Args:
        file_id: ID del archivo en la base de datos
        file_hash: Hash del archivo (sin prefijo "sha256:")
        failed_datanode_id: ID del DataNode que falló
    
    Returns:
        True si se re-replicó exitosamente, False en caso de error
    """
    # Obtener réplicas existentes (excluyendo la que falló)
    replicas = get_file_replicas(file_id, node_id_db=node_id_db)
    active_replicas = [r for r in replicas if r.get("status") == "active" and r["datanode_id"] != failed_datanode_id]
    
    if not active_replicas:
        print(f"[DATANODE_MANAGER] No hay réplicas activas disponibles para re-replicar file_id={file_id}")
        return False
    
    # Leer archivo desde una réplica existente
    source_replica = active_replicas[0]
    source_url = source_replica["url"]
    if not source_url.startswith("http"):
        source_url = f"http://{source_url}:{source_replica['port']}"
    
    try:
        # Obtener token de servicio para autenticación con DataNode
        import os
        try:
            from security.service_auth import generate_service_token
            service_token = generate_service_token(os.getenv("NODE_ID", "namenode-1"), "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Leer archivo desde la réplica existente
        response = requests.get(
            f"{source_url}/retrieve/{file_hash}",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=30
        )
        response.raise_for_status()
        file_content = response.content
        file_size = len(file_content)
        
        print(f"[DATANODE_MANAGER] Archivo leído desde {source_replica['datanode_id']} ({file_size} bytes)")
        
        # Obtener lista de DataNodes a excluir (los que ya tienen réplicas y el que falló)
        existing_datanode_ids = [r["datanode_id"] for r in replicas]
        exclude_datanodes = existing_datanode_ids + [failed_datanode_id]
        
        # Asignar nuevo DataNode para la réplica (excluyendo los existentes)
        new_datanode_ids = assign_replicas(file_hash, file_size, node_id_db=node_id_db, exclude_datanodes=exclude_datanodes)
        if not new_datanode_ids:
            print(f"[DATANODE_MANAGER] No se pudo asignar nuevo DataNode para re-replicación")
            return False
        
        # Seleccionar un DataNode que no sea el que falló ni los que ya tienen réplicas
        available_new_datanodes = [dn_id for dn_id in new_datanode_ids 
                                   if dn_id not in existing_datanode_ids and dn_id != failed_datanode_id]
        
        if not available_new_datanodes:
            print(f"[DATANODE_MANAGER] No hay DataNodes disponibles para re-replicación")
            return False
        
        new_datanode_id = available_new_datanodes[0]
        new_datanode_info = get_datanode(new_datanode_id, node_id_db=node_id_db)
        
        if not new_datanode_info:
            print(f"[DATANODE_MANAGER] DataNode {new_datanode_id} no encontrado")
            return False
        
        new_datanode_url = new_datanode_info["url"]
        if not new_datanode_url.startswith("http"):
            new_datanode_url = f"http://{new_datanode_url}:{new_datanode_info['port']}"
        
        # Obtener token de servicio para autenticación con DataNode
        import os
        try:
            from security.service_auth import generate_service_token
            service_token = generate_service_token(os.getenv("NODE_ID", "namenode-1"), "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Enviar archivo al nuevo DataNode
        files = {"file": ("replica", file_content)}
        data = {"file_id": file_hash}
        
        response = requests.post(
            f"{new_datanode_url}/store",
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=30
        )
        response.raise_for_status()
        
        print(f"[DATANODE_MANAGER] Archivo re-replicado a {new_datanode_id}")
        
        # Actualizar asignación de réplicas (reemplazar el DataNode fallido)
        # Obtener el tipo de réplica que tenía el DataNode fallido
        failed_replica_type = None
        for r in replicas:
            if r["datanode_id"] == failed_datanode_id:
                failed_replica_type = r["replica_type"]
                break
        
        # Actualizar la base de datos
        db_path = get_db_path(node_id_db)
        with db_lock:
            conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
            try:
                # Eliminar réplica del DataNode fallido
                cursor.execute("""
                    DELETE FROM file_replicas 
                    WHERE file_id = ? AND datanode_id = ?
                """, (file_id, failed_datanode_id))
                
                # Agregar nueva réplica
                if failed_replica_type:
                    cursor.execute("""
                        INSERT INTO file_replicas (file_id, datanode_id, replica_type)
                        VALUES (?, ?, ?)
                    """, (file_id, new_datanode_id, failed_replica_type))
                else:
                    # Si no sabemos el tipo, usar 'tertiary' como default
                    cursor.execute("""
                        INSERT INTO file_replicas (file_id, datanode_id, replica_type)
                        VALUES (?, ?, ?)
                    """, (file_id, new_datanode_id, "tertiary"))
                
                conn.commit()
                print(f"[DATANODE_MANAGER] Réplicas actualizadas: {failed_datanode_id} -> {new_datanode_id}")
                return True
            except Exception as e:
                conn.rollback()
                print(f"[DATANODE_MANAGER] Error actualizando réplicas: {e}")
                return False
            finally:
                close_connection(conn)
        
    except Exception as e:
        print(f"[DATANODE_MANAGER] Error en re-replicación de file_id={file_id}: {e}")
        return False


def mark_datanode_inactive(node_id: str, node_id_db: str = None) -> bool:
    """
    Marca un DataNode como inactivo.
    
    Returns:
        True si se actualizó correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                UPDATE datanodes 
                SET status = 'inactive'
                WHERE node_id = ?
            """, (node_id,))
            
            conn.commit()
            if cursor.rowcount > 0:
                print(f"[DATANODE_MANAGER] DataNode {node_id} marcado como inactivo")
                return True
            return False
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al marcar DataNode {node_id} como inactivo: {e}")
            return False
        finally:
            close_connection(conn)


def mark_datanode_draining(node_id: str, node_id_db: str = None) -> bool:
    """
    Marca un DataNode para drenaje. Esto evita que se le asignen nuevos archivos.
    
    Returns:
        True si se marcó correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                UPDATE datanodes 
                SET draining = 1
                WHERE node_id = ?
            """, (node_id,))
            
            conn.commit()
            if cursor.rowcount > 0:
                print(f"[DATANODE_MANAGER] DataNode {node_id} marcado para drenaje")
                return True
            return False
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al marcar DataNode {node_id} para drenaje: {e}")
            return False
        finally:
            close_connection(conn)


def unmark_datanode_draining(node_id: str, node_id_db: str = None) -> bool:
    """
    Desmarca un DataNode del proceso de drenaje.
    
    Returns:
        True si se desmarcó correctamente, False en caso de error
    """
    db_path = get_db_path(node_id_db)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            cursor.execute("""
                UPDATE datanodes 
                SET draining = 0
                WHERE node_id = ?
            """, (node_id,))
            
            conn.commit()
            if cursor.rowcount > 0:
                print(f"[DATANODE_MANAGER] DataNode {node_id} desmarcado del drenaje")
                return True
            return False
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al desmarcar DataNode {node_id} del drenaje: {e}")
            return False
        finally:
            close_connection(conn)


def drain_datanode(node_id: str, node_id_db: str = None) -> Dict:
    """
    Drena un DataNode: re-replica todos sus archivos a otros DataNodes.
    
    Returns:
        Diccionario con estadísticas del drenaje
    """
    print(f"[DATANODE_MANAGER] Iniciando drenaje de DataNode {node_id}...")
    
    # Marcar para drenaje (evitar nuevas asignaciones)
    mark_datanode_draining(node_id, node_id_db=node_id_db)
    
    # Obtener archivos afectados
    affected_files = get_files_affected_by_datanode(node_id, node_id_db=node_id_db)
    print(f"[DATANODE_MANAGER] {len(affected_files)} archivos a re-replicar desde {node_id}")
    
    # Re-replicar cada archivo
    from namenode.manager import get_file_by_id
    rereplicated_count = 0
    failed_count = 0
    
    for file_id in affected_files:
        file_data = get_file_by_id(file_id, node_id=node_id_db)
        if not file_data:
            failed_count += 1
            continue
        
        hash_value = file_data.get("hash", "")
        file_hash = hash_value[7:] if hash_value.startswith("sha256:") else hash_value
        
        if rereplicate_file(file_id, file_hash, node_id, node_id_db=node_id_db):
            rereplicated_count += 1
        else:
            failed_count += 1
    
    result = {
        "datanode_id": node_id,
        "total_files": len(affected_files),
        "rereplicated": rereplicated_count,
        "failed": failed_count,
        "drained": rereplicated_count == len(affected_files) and len(affected_files) > 0
    }
    
    print(f"[DATANODE_MANAGER] Drenaje completado para {node_id}: {rereplicated_count}/{len(affected_files)} archivos re-replicados")
    
    return result


def detect_inactive_datanodes(timeout_seconds: int = 30, node_id_db: str = None) -> List[str]:
    """
    Detecta DataNodes que no han enviado heartbeat en el tiempo especificado.
    
    Args:
        timeout_seconds: Tiempo en segundos sin heartbeat para considerar inactivo
    
    Returns:
        Lista de DataNode IDs marcados como inactivos
    """
    db_path = get_db_path(node_id_db)
    current_time = time.time()
    inactive_ids = []
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id_db)
        
        try:
            # Buscar DataNodes activos sin heartbeat reciente
            cursor.execute("""
                SELECT node_id, last_heartbeat 
                FROM datanodes 
                WHERE status = 'active' AND last_heartbeat IS NOT NULL
            """)
            
            for row in cursor.fetchall():
                node_id = row[0]
                last_heartbeat = row[1]
                
                if last_heartbeat and (current_time - last_heartbeat) > timeout_seconds:
                    # Marcar como inactivo
                    cursor.execute("""
                        UPDATE datanodes 
                        SET status = 'inactive'
                        WHERE node_id = ?
                    """, (node_id,))
                    inactive_ids.append(node_id)
                    print(f"[DATANODE_MANAGER] DataNode {node_id} detectado como inactivo (último heartbeat: {current_time - last_heartbeat:.1f}s)")
            
            if inactive_ids:
                conn.commit()
            
            return inactive_ids
            
        except Exception as e:
            conn.rollback()
            print(f"[DATANODE_MANAGER] Error al detectar DataNodes inactivos: {e}")
            return []
        finally:
            close_connection(conn)
