"""
MetaNameNode - Servicio distribuido con 3 réplicas
Mantiene metadatos de archivos (nombres y etiquetas) con replicación Raft-like
Los archivos físicos se almacenan en DataNodes, no aquí.
"""
from fastapi import FastAPI, HTTPException, Query, UploadFile, Form
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import time
import threading
from datetime import datetime
import os
import requests
from contextlib import asynccontextmanager
import json
import sqlite3
import hashlib

from namenode.database import init_db, get_db_path, get_connection, close_connection, db_lock
from namenode.manager import (
    add_file_metadata, query_files, get_file_by_id, delete_file_metadata,
    delete_files_by_tags, add_tags_to_files, delete_tags_from_files
)
from namenode.datanode_manager import (
    register_datanode, update_datanode_heartbeat, get_datanode, list_datanodes,
    assign_replicas, save_file_replicas, get_file_replicas, detect_inactive_datanodes,
    get_best_datanode_for_read, get_all_replicas_for_read, delete_file_from_datanodes,
    get_files_affected_by_datanode, rereplicate_file, mark_datanode_draining,
    unmark_datanode_draining, drain_datanode, discover_file_replicas
)
from namenode.registry_client import registry_client

app = FastAPI(title="TBFS MetaNameNode (Distributed)")

# Configurar CORS para permitir peticiones desde el frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, especificar dominios específicos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Estado del cluster
cluster_state = {
    "node_id": os.getenv("NODE_ID", "namenode-1"),
    "is_leader": False,
    "leader_id": None,
    "term": 0,
    "last_heartbeat_time": 0,
    "last_election_time": 0,
    "peers": []
}
cluster_lock = threading.Lock()

# Configuración
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
LEADER_HEARTBEAT_INTERVAL = int(os.getenv("LEADER_HEARTBEAT_INTERVAL", "5"))
ELECTION_TIMEOUT = int(os.getenv("ELECTION_TIMEOUT", "15"))
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))

# Parsear lista de peers desde variable de entorno
PEERS_ENV = os.getenv("PEERS", "")
if PEERS_ENV:
    cluster_state["peers"] = [p.strip() for p in PEERS_ENV.split(",") if p.strip()]

# Obtener node_id para la base de datos
NODE_ID = cluster_state["node_id"]


# Modelos Pydantic
class FileMetadata(BaseModel):
    name: str
    tags: List[str]
    size: Optional[int] = None
    hash: Optional[str] = None


class TagOperation(BaseModel):
    tags: List[str]


class ReplicationData(BaseModel):
    """Datos para replicación entre nodos"""
    term: int
    leader_id: str
    # En lugar de replicar todo el estado, replicamos operaciones
    # Esto es más eficiente que replicar toda la BD


class VoteRequest(BaseModel):
    candidate_id: str
    term: int


class VoteResponse(BaseModel):
    granted: bool
    term: int


class OperationLog(BaseModel):
    """Log de operaciones para replicación"""
    operation: str  # 'add_file', 'delete_file', 'add_tags', 'delete_tags'
    data: Dict
    term: int
    timestamp: float


# Log de operaciones para Raft
operation_log = []
log_lock = threading.Lock()
commit_index = 0


def get_peer_url(peer: str) -> str:
    """Obtiene la URL completa de un peer"""
    if not peer.startswith("http"):
        # Si el peer es "namenode-1", construir "tbfs-namenode-1" (nombre del servicio Docker)
        if peer.startswith("namenode-"):
            peer = f"tbfs-{peer}"
        return f"http://{peer}:{NAMENODE_PORT}"
    return peer


def is_leader() -> bool:
    """Verifica si este nodo es el líder"""
    with cluster_lock:
        return cluster_state["is_leader"]


def get_leader_url() -> Optional[str]:
    """Obtiene la URL del líder actual"""
    with cluster_lock:
        if cluster_state["is_leader"]:
            return None  # Este nodo es el líder
        if cluster_state["leader_id"]:
            return get_peer_url(cluster_state["leader_id"])
        return None


def replicate_to_peers(operation: OperationLog):
    """Replica una operación a los peers del cluster"""
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        term = cluster_state["term"]
        leader_id = cluster_state["node_id"]
    
    # Si no hay peers, no hay nada que replicar (modo desarrollo)
    if not peers:
        return True
    
    success_count = 0
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/replicate",
                json={
                    "operation": operation.operation,
                    "data": operation.data,
                    "term": operation.term,
                    "timestamp": operation.timestamp
                },
                timeout=3
            )
            if response.status_code == 200:
                success_count += 1
        except Exception as e:
            print(f"[NAMENODE] Error replicando a {peer}: {e}")
    
    # Se necesita mayoría (quorum): al menos 2 de 3 nodos
    total_nodes = len(peers) + 1  # +1 por este nodo
    quorum = (total_nodes // 2) + 1
    replicated = success_count + 1 >= quorum  # +1 por este nodo
    
    if not replicated:
        print(f"[NAMENODE] WARNING: Solo se replicó a {success_count + 1}/{total_nodes} nodos (quorum: {quorum})")
    
    return replicated


def request_vote(candidate_id: str, term: int) -> bool:
    """Solicita votos para elección de líder"""
    with cluster_lock:
        peers = cluster_state["peers"].copy()
    
    if not peers:
        return True
    
    votes = 1  # Voto propio
    successful_contacts = 1
    
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/vote",
                json={"candidate_id": candidate_id, "term": term},
                timeout=2
            )
            if response.status_code == 200:
                successful_contacts += 1
                data = response.json()
                if data.get("granted"):
                    votes += 1
        except Exception as e:
            print(f"[NAMENODE] Error solicitando voto a {peer}: {e}")
    
    total_nodes = len(peers) + 1
    quorum = (total_nodes // 2) + 1
    
    if votes >= quorum and successful_contacts >= quorum:
        return True
    
    if successful_contacts == 1:
        print(f"[NAMENODE] No se pudo contactar a ningún peer. Este nodo es el único disponible, convirtiéndose en líder.")
        return True
    
    print(f"[NAMENODE] Votos obtenidos: {votes}/{quorum}, Nodos contactados: {successful_contacts}/{total_nodes}")
    return False


def start_election():
    """Inicia una elección de líder"""
    current_time = time.time()
    
    with cluster_lock:
        time_since_last_election = current_time - cluster_state.get("last_election_time", 0)
        if time_since_last_election < 5:
            print(f"[NAMENODE] Elección reciente hace {time_since_last_election:.1f}s, esperando cooldown...")
            return False
        
        cluster_state["term"] += 1
        cluster_state["last_election_time"] = current_time
        candidate_id = cluster_state["node_id"]
        term = cluster_state["term"]
        peers = cluster_state["peers"].copy()
        cluster_state["is_leader"] = False
        cluster_state["leader_id"] = None
    
    if not peers:
        with cluster_lock:
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[NAMENODE] Modo desarrollo: nodo único, automáticamente líder (término {term})")
        return True
    
    print(f"[NAMENODE] Iniciando elección (término {term})...")
    
    if request_vote(candidate_id, term):
        with cluster_lock:
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[NAMENODE] ¡Elegido como líder! (término {term})")
        return True
    else:
        print(f"[NAMENODE] No se obtuvo mayoría en la elección (término {term})")
        return False


def leader_heartbeat_loop():
    """Loop que envía heartbeats a los seguidores"""
    while True:
        time.sleep(LEADER_HEARTBEAT_INTERVAL)
        
        if not is_leader():
            continue
        
        # Enviar heartbeat vacío para mantener el liderazgo
        with cluster_lock:
            term = cluster_state["term"]
            leader_id = cluster_state["node_id"]
            peers = cluster_state["peers"].copy()
        
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                requests.post(
                    f"{peer_url}/internal/heartbeat",
                    json={"term": term, "leader_id": leader_id},
                    timeout=2
                )
            except Exception as e:
                pass  # Silenciar errores de heartbeat
        
        with cluster_lock:
            cluster_state["last_heartbeat_time"] = time.time()


def follower_heartbeat_check():
    """Verifica si el líder sigue activo (para seguidores)"""
    while True:
        time.sleep(ELECTION_TIMEOUT)
        
        if is_leader():
            continue
        
        with cluster_lock:
            time_since_heartbeat = time.time() - cluster_state["last_heartbeat_time"]
            leader_id = cluster_state["leader_id"]
        
        if leader_id:
            if time_since_heartbeat > ELECTION_TIMEOUT:
                try:
                    leader_url = get_peer_url(leader_id)
                    response = requests.get(f"{leader_url}/", timeout=3)
                    if response.status_code == 200:
                        with cluster_lock:
                            cluster_state["last_heartbeat_time"] = time.time()
                        continue
                    else:
                        print(f"[NAMENODE] Líder {leader_id} no responde correctamente, iniciando elección...")
                        with cluster_lock:
                            cluster_state["leader_id"] = None
                        start_election()
                except Exception as e:
                    if time_since_heartbeat > (ELECTION_TIMEOUT * 1.5):
                        print(f"[NAMENODE] Líder {leader_id} inaccesible ({time_since_heartbeat:.1f}s sin contacto): {e}")
                        print(f"[NAMENODE] Iniciando elección...")
                        with cluster_lock:
                            cluster_state["leader_id"] = None
                        start_election()
        elif time_since_heartbeat > ELECTION_TIMEOUT:
            print(f"[NAMENODE] No hay líder conocido después de {time_since_heartbeat:.1f}s, intentando elección...")
            start_election()


def election_retry_loop():
    """Loop que reintenta elecciones si no hay líder"""
    time.sleep(10)
    
    while True:
        time.sleep(ELECTION_TIMEOUT * 2)
        
        with cluster_lock:
            is_leader_flag = cluster_state["is_leader"]
            leader_id = cluster_state["leader_id"]
            last_heartbeat = cluster_state["last_heartbeat_time"]
        
        current_time = time.time()
        time_since_heartbeat = current_time - last_heartbeat
        
        if not is_leader_flag and not leader_id and time_since_heartbeat > (ELECTION_TIMEOUT * 3):
            print(f"[NAMENODE] No hay líder detectado después de {time_since_heartbeat:.1f}s, intentando elección...")
            start_election()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el ciclo de vida de la aplicación"""
    # Startup
    print(f"[NAMENODE] Nodo iniciado: {cluster_state['node_id']}")
    print(f"[NAMENODE] Peers: {cluster_state['peers']}")
    
    # Inicializar base de datos
    init_db(node_id=NODE_ID)
    
    # Iniciar registro en el registry
    registry_client.start()
    
    # Iniciar hilos
    heartbeat_thread = threading.Thread(target=leader_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    
    follower_thread = threading.Thread(target=follower_heartbeat_check, daemon=True)
    follower_thread.start()
    
    election_thread = threading.Thread(target=election_retry_loop, daemon=True)
    election_thread.start()
    
    # Hilo para monitorear DataNodes inactivos y re-replicar archivos (solo en el líder)
    def datanode_monitor_loop():
        """Monitorea DataNodes inactivos cada 30 segundos y re-replica archivos afectados"""
        while True:
            time.sleep(30)  # Revisar cada 30 segundos
            if is_leader():
                try:
                    inactive = detect_inactive_datanodes(timeout_seconds=30, node_id_db=NODE_ID)
                    if inactive:
                        print(f"[NAMENODE] DataNodes inactivos detectados: {inactive}")
                        
                        # Re-replicar archivos afectados por cada DataNode inactivo
                        for failed_datanode_id in inactive:
                            print(f"[NAMENODE] Iniciando re-replicación para archivos en {failed_datanode_id}...")
                            
                            # Obtener archivos afectados
                            affected_files = get_files_affected_by_datanode(failed_datanode_id, node_id_db=NODE_ID)
                            print(f"[NAMENODE] {len(affected_files)} archivos afectados por {failed_datanode_id}")
                            
                            # Re-replicar cada archivo
                            from namenode.manager import get_file_by_id
                            rereplicated_count = 0
                            failed_count = 0
                            
                            for file_id in affected_files:
                                file_data = get_file_by_id(file_id, node_id=NODE_ID)
                                if not file_data:
                                    continue
                                
                                hash_value = file_data.get("hash", "")
                                file_hash = hash_value[7:] if hash_value.startswith("sha256:") else hash_value
                                
                                if rereplicate_file(file_id, file_hash, failed_datanode_id, node_id_db=NODE_ID):
                                    rereplicated_count += 1
                                else:
                                    failed_count += 1
                            
                            print(f"[NAMENODE] Re-replicación completada para {failed_datanode_id}: {rereplicated_count} exitosas, {failed_count} fallidas")
                            
                except Exception as e:
                    print(f"[NAMENODE] Error en monitoreo de DataNodes: {e}")
                    import traceback
                    traceback.print_exc()
    
    datanode_monitor_thread = threading.Thread(target=datanode_monitor_loop, daemon=True)
    datanode_monitor_thread.start()
    
    # Intentar elección inicial después de un delay
    time.sleep(5)
    start_election()
    
    yield
    
    # Shutdown
    registry_client.stop()
    print(f"[NAMENODE] Nodo deteniéndose...")


app = FastAPI(title="TBFS MetaNameNode (Distributed)", lifespan=lifespan)


@app.get("/")
def root():
    """Endpoint de estado del MetaNameNode"""
    with cluster_lock:
        cluster_data = {
            "node_id": cluster_state["node_id"],
            "is_leader": cluster_state["is_leader"],
            "leader_id": cluster_state["leader_id"],
            "term": cluster_state["term"]
        }
        leader_id = cluster_state["leader_id"]
        is_leader_flag = cluster_state["is_leader"]
    
    # Obtener estadísticas de la base de datos
    db_path = get_db_path(NODE_ID)
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
        try:
            cursor.execute("SELECT COUNT(*) FROM files")
            total_files = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM tags")
            total_tags = cursor.fetchone()[0]
        finally:
            close_connection(conn)
    
    # Incluir URL del líder
    leader_url = None
    if is_leader_flag:
        # Si este nodo es el líder, devolver su propia URL
        # Construir la URL usando el nombre del servicio Docker
        node_service_name = f"tbfs-{cluster_state['node_id']}"
        leader_url = f"http://{node_service_name}:{NAMENODE_PORT}"
        print(f"[NAMENODE] Endpoint /: Este nodo es el líder. leader_url={leader_url}")
    elif leader_id:
        # Si no es líder, devolver la URL del líder
        leader_url = get_peer_url(leader_id)
        print(f"[NAMENODE] Endpoint /: leader_id={leader_id}, leader_url={leader_url}")
    
    return {
        "message": "MetaNameNode funcionando",
        **cluster_data,
        "total_files": total_files,
        "total_tags": total_tags,
        "leader_url": leader_url  # URL del líder para que el cliente pueda usarla directamente
    }


# ========== ENDPOINTS DE GESTIÓN DE DATANODES ==========

class DataNodeRegistration(BaseModel):
    node_id: str
    url: str
    port: int
    ip: Optional[str] = None
    total_space: int
    free_space: int


class DataNodeHeartbeat(BaseModel):
    free_space: int
    total_space: int


@app.post("/datanodes/register")
def register_datanode_endpoint(registration: DataNodeRegistration):
    """
    Registra un nuevo DataNode o actualiza uno existente.
    Solo el líder puede registrar DataNodes.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/datanodes/register",
                json=registration.dict(),
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    print(f"[NAMENODE] Registrando DataNode: {registration.node_id}")
    
    success = register_datanode(
        node_id=registration.node_id,
        url=registration.url,
        port=registration.port,
        ip=registration.ip,
        total_space=registration.total_space,
        free_space=registration.free_space,
        node_id_db=NODE_ID
    )
    
    if success:
        return {"success": True, "message": f"DataNode {registration.node_id} registrado correctamente"}
    else:
        raise HTTPException(status_code=500, detail="Error al registrar DataNode")


@app.get("/datanodes")
def list_datanodes_endpoint(status: Optional[str] = Query(None)):
    """
    Lista todos los DataNodes registrados.
    Cualquier nodo puede responder (lee de su propia base de datos).
    """
    datanodes = list_datanodes(status=status, node_id_db=NODE_ID)
    return {"datanodes": datanodes, "total": len(datanodes)}


@app.get("/datanodes/{node_id}")
def get_datanode_endpoint(node_id: str):
    """
    Obtiene información de un DataNode específico.
    """
    datanode = get_datanode(node_id, node_id_db=NODE_ID)
    if not datanode:
        raise HTTPException(status_code=404, detail=f"DataNode {node_id} no encontrado")
    return datanode


@app.post("/datanodes/{node_id}/heartbeat")
def datanode_heartbeat_endpoint(node_id: str, heartbeat: DataNodeHeartbeat):
    """
    Recibe un heartbeat de un DataNode.
    Solo el líder procesa heartbeats.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/datanodes/{node_id}/heartbeat",
                json=heartbeat.dict(),
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    print(f"[NAMENODE] Heartbeat recibido de DataNode: {node_id}")
    
    success = update_datanode_heartbeat(
        node_id=node_id,
        free_space=heartbeat.free_space,
        total_space=heartbeat.total_space,
        node_id_db=NODE_ID
    )
    
    if success:
        return {"success": True, "message": "Heartbeat procesado"}
    else:
        raise HTTPException(status_code=404, detail=f"DataNode {node_id} no encontrado")


@app.post("/datanodes/{node_id}/drain")
def drain_datanode_endpoint(node_id: str):
    """
    Inicia el drenaje de un DataNode: re-replica todos sus archivos y evita nuevas asignaciones.
    Solo el líder puede ejecutar esta operación.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(f"{leader_url}/datanodes/{node_id}/drain", timeout=60)
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Verificar que el DataNode existe
    datanode = get_datanode(node_id, node_id_db=NODE_ID)
    if not datanode:
        raise HTTPException(status_code=404, detail=f"DataNode {node_id} no encontrado")
    
    print(f"[NAMENODE] Iniciando drenaje de DataNode: {node_id}")
    
    # Ejecutar drenaje
    result = drain_datanode(node_id, node_id_db=NODE_ID)
    
    return {
        "success": result["drained"],
        "message": f"Drenaje completado: {result['rereplicated']}/{result['total_files']} archivos re-replicados",
        "statistics": result
    }


@app.post("/datanodes/{node_id}/undrain")
def undrain_datanode_endpoint(node_id: str):
    """
    Desmarca un DataNode del proceso de drenaje, permitiendo nuevas asignaciones.
    Solo el líder puede ejecutar esta operación.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(f"{leader_url}/datanodes/{node_id}/undrain", timeout=5)
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    success = unmark_datanode_draining(node_id, node_id_db=NODE_ID)
    
    if success:
        return {"success": True, "message": f"DataNode {node_id} desmarcado del drenaje"}
    else:
        raise HTTPException(status_code=404, detail=f"DataNode {node_id} no encontrado")


# ========== ENDPOINTS DE METADATOS ==========

# @app.post("/files")
# def add_file(metadata: FileMetadata):
#     """Agrega metadatos de un archivo (solo el líder)"""
#     leader_url = get_leader_url()
#     if leader_url:
#         try:
#             response = requests.post(
#                 f"{leader_url}/files",
#                 json=metadata.dict(),
#                 timeout=5
#             )
#             return response.json()
#         except Exception as e:
#             raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
#     if not is_leader():
#         raise HTTPException(status_code=503, detail="No hay líder disponible")
    
#     # Agregar metadatos localmente
#     file_id = add_file_metadata(
#         name=metadata.name,
#         tags=metadata.tags,
#         size=metadata.size,
#         hash_value=metadata.hash,
#         node_id=NODE_ID
#     )
    
#     if not file_id:
#         raise HTTPException(status_code=400, detail="No se pudo agregar el archivo")
    
#     # Replicar operación a los seguidores
#     operation = OperationLog(
#         operation="add_file",
#         data={
#             "name": metadata.name,
#             "tags": metadata.tags,
#             "size": metadata.size,
#             "hash": metadata.hash
#         },
#         term=cluster_state["term"],
#         timestamp=time.time()
#     )
    
#     with log_lock:
#         operation_log.append(operation)
    
#     replicate_to_peers(operation)
    
#     return {
#         "success": True,
#         "message": f"Metadatos de '{metadata.name}' agregados correctamente",
#         "file_id": file_id
#     }


# @app.get("/files")
# def list_files(tags: Optional[List[str]] = Query(None)):
#     """Lista archivos por tags (cualquier nodo puede responder)"""
#     files = query_files(query_tags=tags, node_id=NODE_ID)
    
#     result = []
#     for file_id, name, tags_str in files:
#         tags_list = tags_str.split(",") if tags_str else []
#         result.append({
#             "id": file_id,
#             "name": name,
#             "tags": tags_list
#         })
    
#     return {"files": result}


# @app.get("/files/{file_id}")
# def get_file(file_id: int):
#     """Obtiene metadatos de un archivo específico"""
#     file_data = get_file_by_id(file_id, node_id=NODE_ID)
#     if not file_data:
#         raise HTTPException(status_code=404, detail="Archivo no encontrado")
#     return file_data


# @app.delete("/files/{file_id}")
# def delete_file(file_id: int):
#     """Elimina metadatos de un archivo (solo el líder)"""
#     leader_url = get_leader_url()
#     if leader_url:
#         try:
#             response = requests.delete(f"{leader_url}/files/{file_id}", timeout=5)
#             return response.json()
#         except Exception as e:
#             raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
#     if not is_leader():
#         raise HTTPException(status_code=503, detail="No hay líder disponible")
    
#     file_data = get_file_by_id(file_id, node_id=NODE_ID)
#     if not file_data:
#         raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
#     deleted = delete_file_metadata(file_id, node_id=NODE_ID)
    
#     if deleted:
#         # Replicar operación
#         operation = OperationLog(
#             operation="delete_file",
#             data={"file_id": file_id},
#             term=cluster_state["term"],
#             timestamp=time.time()
#         )
#         with log_lock:
#             operation_log.append(operation)
#         replicate_to_peers(operation)
    
#     return {"success": deleted, "message": "Metadatos eliminados" if deleted else "No se encontró el archivo"}


# @app.delete("/files")
# def delete_files_by_query(tags: str = Query(...)):
#     """Elimina archivos por tags (solo el líder)"""
#     leader_url = get_leader_url()
#     if leader_url:
#         try:
#             response = requests.delete(f"{leader_url}/files", params={"tags": tags}, timeout=5)
#             return response.json()
#         except Exception as e:
#             raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
#     if not is_leader():
#         raise HTTPException(status_code=503, detail="No hay líder disponible")
    
#     tag_list = [t.strip() for t in tags.split(",") if t.strip()]
#     deleted = delete_files_by_tags(tag_list, node_id=NODE_ID)
    
#     if deleted:
#         operation = OperationLog(
#             operation="delete_files_by_tags",
#             data={"tags": tag_list},
#             term=cluster_state["term"],
#             timestamp=time.time()
#         )
#         with log_lock:
#             operation_log.append(operation)
#         replicate_to_peers(operation)
    
#     return {"success": deleted, "message": "Archivos eliminados" if deleted else "No se encontraron coincidencias"}


# @app.post("/files/tags/add")
# def add_tags(query: str = Query(...), new_tags: str = Query(...)):
#     """Agrega etiquetas a archivos (solo el líder)"""
#     leader_url = get_leader_url()
#     if leader_url:
#         try:
#             response = requests.post(
#                 f"{leader_url}/files/tags/add",
#                 params={"query": query, "new_tags": new_tags},
#                 timeout=5
#             )
#             return response.json()
#         except Exception as e:
#             raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
#     if not is_leader():
#         raise HTTPException(status_code=503, detail="No hay líder disponible")
    
#     query_tags = [t.strip() for t in query.split(",") if t.strip()]
#     new_tags_list = [t.strip() for t in new_tags.split(",") if t.strip()]
    
#     ok = add_tags_to_files(query_tags, new_tags_list, node_id=NODE_ID)
    
#     if ok:
#         operation = OperationLog(
#             operation="add_tags",
#             data={"query_tags": query_tags, "new_tags": new_tags_list},
#             term=cluster_state["term"],
#             timestamp=time.time()
#         )
#         with log_lock:
#             operation_log.append(operation)
#         replicate_to_peers(operation)
    
#     return {"success": ok}


# @app.post("/files/tags/delete")
# def delete_tags(query: str = Query(...), del_tags: str = Query(...)):
#     """Elimina etiquetas de archivos (solo el líder)"""
#     leader_url = get_leader_url()
#     if leader_url:
#         try:
#             response = requests.post(
#                 f"{leader_url}/files/tags/delete",
#                 params={"query": query, "del_tags": del_tags},
#                 timeout=5
#             )
#             return response.json()
#         except Exception as e:
#             raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
#     if not is_leader():
#         raise HTTPException(status_code=503, detail="No hay líder disponible")
    
#     query_tags = [t.strip() for t in query.split(",") if t.strip()]
#     del_tags_list = [t.strip() for t in del_tags.split(",") if t.strip()]
    
#     ok = delete_tags_from_files(query_tags, del_tags_list, node_id=NODE_ID)
    
#     if ok:
#         operation = OperationLog(
#             operation="delete_tags",
#             data={"query_tags": query_tags, "del_tags": del_tags_list},
#             term=cluster_state["term"],
#             timestamp=time.time()
#         )
#         with log_lock:
#             operation_log.append(operation)
#         replicate_to_peers(operation)
    
#     return {"success": ok}


# ========== ENDPOINTS DE COMPATIBILIDAD (formato antiguo del cliente) ==========

@app.post("/add")
async def add_file_compat(file: UploadFile, tags: str = Form(...)):
    """
    Endpoint de compatibilidad: recibe archivo y guarda solo metadatos.
    NOTA: El archivo físico no se almacena aquí (se enviará a DataNodes en el futuro).
    Por ahora solo guardamos los metadatos.
    """
    print(f"[NAMENODE] POST /add recibido: archivo={file.filename}, tags={tags}")
    
    leader_url = get_leader_url()
    if leader_url:
        print(f"[NAMENODE] Redirigiendo a líder: {leader_url}")
        try:
            # Leer el archivo una vez
            file_content = await file.read()
            # Redirigir al líder
            files = {"file": (file.filename, file_content)}
            response = requests.post(
                f"{leader_url}/add",
                files=files,
                data={"tags": tags},
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[NAMENODE] Error redirigiendo a líder: {e}")
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        print(f"[NAMENODE] Este nodo no es líder, pero no hay líder disponible")
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    print(f"[NAMENODE] Procesando en líder (nodo {NODE_ID})")
    
    # Leer el archivo temporalmente para calcular hash y tamaño
    file_content = await file.read()
    file_size = len(file_content)
    
    print(f"[NAMENODE] Archivo leído: tamaño={file_size} bytes")
    
    # Calcular hash del archivo
    file_hash = hashlib.sha256(file_content).hexdigest()
    hash_value = f"sha256:{file_hash}"
    
    # Parsear tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    
    print(f"[NAMENODE] Agregando metadatos: name={file.filename}, tags={tag_list}, hash={hash_value[:16]}...")
    
    # Agregar metadatos primero
    file_id = add_file_metadata(
        name=file.filename,
        tags=tag_list,
        size=file_size,
        hash_value=hash_value,
        node_id=NODE_ID
    )
    
    if not file_id:
        print(f"[NAMENODE] Error: No se pudo agregar metadatos")
        raise HTTPException(status_code=400, detail="No se pudo agregar el archivo")
    
    print(f"[NAMENODE] Metadatos agregados con file_id={file_id}")
    
    # Asignar réplicas a DataNodes (usar file_hash sin prefijo "sha256:")
    datanode_ids = assign_replicas(file_hash, file_size, node_id_db=NODE_ID)
    
    if not datanode_ids:
        print(f"[NAMENODE] Error: No se pudieron asignar réplicas. Eliminando metadatos...")
        delete_file_metadata(file_id, node_id=NODE_ID)
        raise HTTPException(
            status_code=503, 
            detail="No hay suficientes DataNodes disponibles para almacenar el archivo"
        )
    
    print(f"[NAMENODE] Réplicas asignadas: {datanode_ids}")
    
    # Intentar guardar el archivo en DataNodes con reintentos
    max_attempts = 3
    final_datanode_ids = datanode_ids.copy()
    success_count = 0
    successful_datanodes = []
    failed_datanodes = []
    
    for attempt in range(max_attempts):
        if success_count >= 2:
            # Ya tenemos suficientes réplicas, salir
            break
        
        if attempt > 0:
            print(f"[NAMENODE] Reintento {attempt + 1}/{max_attempts} para almacenar archivo...")
            # Reasignar réplicas excluyendo los DataNodes que ya fallaron
            remaining_datanodes = [dn_id for dn_id in final_datanode_ids if dn_id not in failed_datanodes]
            if len(remaining_datanodes) < 3:
                # Necesitamos más DataNodes, reasignar completamente
                new_datanode_ids = assign_replicas(file_hash, file_size, node_id_db=NODE_ID)
                if new_datanode_ids:
                    # Excluir los que ya fallaron
                    available_datanodes = [dn_id for dn_id in new_datanode_ids if dn_id not in failed_datanodes]
                    if len(available_datanodes) >= 2:
                        final_datanode_ids = available_datanodes[:3] if len(available_datanodes) >= 3 else available_datanodes
                    else:
                        final_datanode_ids = new_datanode_ids
                else:
                    print(f"[NAMENODE] No hay más DataNodes disponibles para reasignar")
                    break
            else:
                # Usar los DataNodes restantes
                final_datanode_ids = remaining_datanodes[:3]
        
        # Obtener URLs de los DataNodes
        from namenode.datanode_manager import get_datanode
        datanode_urls = []
        for dn_id in final_datanode_ids:
            if dn_id in successful_datanodes:
                continue  # Ya se guardó exitosamente en este DataNode
            dn_info = get_datanode(dn_id, node_id_db=NODE_ID)
            if dn_info:
                url = dn_info["url"]
                if not url.startswith("http"):
                    url = f"http://{url}:{dn_info['port']}"
                datanode_urls.append((dn_id, url))
        
        if not datanode_urls:
            break  # No hay más DataNodes para intentar
        
        print(f"[NAMENODE] Enviando archivo a {len(datanode_urls)} DataNodes (intento {attempt + 1}/{max_attempts})...")
        
        # Enviar archivo a los DataNodes
        for dn_id, dn_url in datanode_urls:
            if dn_id in successful_datanodes:
                continue  # Ya se guardó exitosamente
            
            try:
                files = {"file": (file.filename, file_content)}
                data = {"file_id": file_hash}
                
                response = requests.post(
                    f"{dn_url}/store",
                    files=files,
                    data=data,
                    timeout=30
                )
                response.raise_for_status()
                print(f"[NAMENODE] Archivo almacenado en {dn_id} ({dn_url})")
                success_count += 1
                successful_datanodes.append(dn_id)
            except Exception as e:
                print(f"[NAMENODE] Error almacenando en {dn_id} ({dn_url}): {e}")
                if dn_id not in failed_datanodes:
                    failed_datanodes.append(dn_id)
    
    # Verificar que al menos 2 de 3 réplicas se guardaron (tolerancia a fallos)
    if success_count < 2:
        print(f"[NAMENODE] Error: Solo {success_count}/3 réplicas se guardaron después de {max_attempts} intentos. Eliminando metadatos...")
        # Intentar eliminar de los DataNodes que sí recibieron el archivo
        for dn_id in successful_datanodes:
            dn_info = get_datanode(dn_id, node_id_db=NODE_ID)
            if dn_info:
                url = dn_info["url"]
                if not url.startswith("http"):
                    url = f"http://{url}:{dn_info['port']}"
                try:
                    requests.delete(f"{url}/delete/{file_hash}", timeout=10)
                except:
                    pass
        delete_file_metadata(file_id, node_id=NODE_ID)
        raise HTTPException(
            status_code=507,
            detail=f"No se pudo almacenar el archivo en suficientes DataNodes después de {max_attempts} intentos ({success_count}/3)"
        )
    
    # Guardar asignación de réplicas con los DataNodes exitosos
    final_replicas = successful_datanodes[:3] if len(successful_datanodes) >= 3 else successful_datanodes
    save_file_replicas(file_id, final_replicas, node_id_db=NODE_ID)
    
    print(f"[NAMENODE] Archivo almacenado exitosamente en {success_count} DataNodes: {successful_datanodes}")
    
    # Replicar operación a otros MetaNameNodes
    operation = OperationLog(
        operation="add_file",
        data={
            "name": file.filename,
            "tags": tag_list,
            "size": file_size,
            "hash": hash_value,
            "datanode_ids": datanode_ids
        },
        term=cluster_state["term"],
        timestamp=time.time()
    )
    
    with log_lock:
        operation_log.append(operation)
    
    replicate_to_peers(operation)
    
    print(f"[NAMENODE] Operación replicada a peers")
    
    return {
        "success": True,
        "message": f"Archivo '{file.filename}' agregado correctamente",
        "file_id": file_id,
        "replicas": datanode_ids,
        "replicas_stored": success_count
    }


@app.get("/list")
def list_files_compat(tags: Optional[List[str]] = Query(None)):
    """Endpoint de compatibilidad: lista archivos (cualquier nodo puede responder)"""
    files = query_files(query_tags=tags, node_id=NODE_ID)
    formatted = [
        {"id": fid, "name": name, "tags": tags, "path": ""}
        for fid, name, tags in files
    ]
    return {"files": formatted}


@app.delete("/delete")
def delete_files_compat(tags: str = Query(...)):
    """Endpoint de compatibilidad: elimina archivos por tags (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.delete(f"{leader_url}/delete", params={"tags": tags}, timeout=5)
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    deleted = delete_files_by_tags(tag_list, node_id=NODE_ID)
    
    if deleted:
        operation = OperationLog(
            operation="delete_files_by_tags",
            data={"tags": tag_list},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        replicate_to_peers(operation)
    
    return {
        "success": deleted,
        "message": "Archivos eliminados" if deleted else "No se encontró coincidencia"
    }


@app.post("/add-tags")
def add_tags_compat(query: str = Query(...), new_tags: str = Query(...)):
    """Endpoint de compatibilidad: agrega etiquetas (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/add-tags",
                params={"query": query, "new_tags": new_tags},
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    query_tags = [t.strip() for t in query.split(",") if t.strip()]
    new_tags_list = [t.strip() for t in new_tags.split(",") if t.strip()]
    
    ok = add_tags_to_files(query_tags, new_tags_list, node_id=NODE_ID)
    
    if ok:
        operation = OperationLog(
            operation="add_tags",
            data={"query_tags": query_tags, "new_tags": new_tags_list},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        replicate_to_peers(operation)
    
    return {"success": ok}


@app.post("/delete-tags")
def delete_tags_compat(query: str = Query(...), del_tags: str = Query(...)):
    """Endpoint de compatibilidad: elimina etiquetas (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/delete-tags",
                params={"query": query, "del_tags": del_tags},
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    query_tags = [t.strip() for t in query.split(",") if t.strip()]
    del_tags_list = [t.strip() for t in del_tags.split(",") if t.strip()]
    
    ok = delete_tags_from_files(query_tags, del_tags_list, node_id=NODE_ID)
    
    if ok:
        operation = OperationLog(
            operation="delete_tags",
            data={"query_tags": query_tags, "del_tags": del_tags_list},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        replicate_to_peers(operation)
    
    return {"success": ok}


@app.get("/download/{file_name}")
def download_file_compat(file_name: str):
    """
    Endpoint de compatibilidad: descarga de archivo desde DataNodes.
    Busca el archivo por nombre, obtiene sus réplicas y descarga desde un DataNode disponible.
    """
    print(f"[NAMENODE] GET /download/{file_name} (nodo: {NODE_ID})")
    
    # Verificar si somos el líder
    if not is_leader():
        # Si no somos el líder, intentar obtener la URL del líder y redirigir
        leader_url = get_leader_url()
        if leader_url:
            print(f"[NAMENODE] Redirigiendo descarga a líder: {leader_url}/download/{file_name}")
            return RedirectResponse(
                url=f"{leader_url}/download/{file_name}",
                status_code=307
            )
        else:
            # No hay líder disponible
            print(f"[NAMENODE] ERROR: No hay líder disponible para redirigir")
            raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Buscar archivo por nombre en metadatos
    files = query_files(query_tags=None, node_id=NODE_ID)
    file_data = None
    file_id = None
    file_hash = None
    
    for fid, name, _ in files:
        if name == file_name:
            file_data = get_file_by_id(fid, node_id=NODE_ID)
            if file_data:
                file_id = fid
                # Extraer hash del formato "sha256:hash"
                hash_value = file_data.get("hash", "")
                if hash_value.startswith("sha256:"):
                    file_hash = hash_value[7:]  # Remover prefijo "sha256:"
                else:
                    file_hash = hash_value
                break
    
    if not file_data or not file_hash:
        raise HTTPException(status_code=404, detail=f"Archivo '{file_name}' no encontrado")
    
    print(f"[NAMENODE] Archivo encontrado: file_id={file_id}, hash={file_hash[:16]}...")
    
    # Obtener todas las réplicas disponibles para lectura
    replicas = get_all_replicas_for_read(file_id, node_id_db=NODE_ID)
    
    if not replicas:
        print(f"[NAMENODE] WARNING: No hay réplicas registradas para file_id={file_id}, intentando descubrir desde DataNodes...")
        # Intentar descubrir réplicas consultando todos los DataNodes activos
        replicas = discover_file_replicas(file_hash, file_id, node_id_db=NODE_ID)
        
        if not replicas:
            print(f"[NAMENODE] ERROR: No hay réplicas disponibles para file_id={file_id}")
            raise HTTPException(
                status_code=503,
                detail="No hay réplicas disponibles del archivo en DataNodes activos"
            )
    
    print(f"[NAMENODE] Réplicas disponibles para lectura: {len(replicas)}")
    for r in replicas:
        print(f"[NAMENODE]   - {r['datanode_id']} ({r['replica_type']}): {r['url']}")
    
    # Intentar leer desde las réplicas en orden de prioridad (con fallback)
    last_error = None
    for replica in replicas:
        datanode_url = replica["url"]
        replica_type = replica["replica_type"]
        
        try:
            print(f"[NAMENODE] Intentando leer desde {replica['datanode_id']} ({replica_type})...")
            response = requests.get(
                f"{datanode_url}/retrieve/{file_hash}",
                timeout=30
            )
            response.raise_for_status()
            
            # Obtener el contenido del archivo
            file_content = response.content
            
            print(f"[NAMENODE] Archivo leído exitosamente desde {replica['datanode_id']} ({len(file_content)} bytes)")
            
            # Retornar archivo como respuesta
            from fastapi.responses import Response
            return Response(
                content=file_content,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{file_name}"',
                    "Content-Length": str(len(file_content))
                }
            )
            
        except requests.RequestException as e:
            print(f"[NAMENODE] Error leyendo desde {replica['datanode_id']}: {e}")
            last_error = e
            continue  # Intentar con la siguiente réplica
    
    # Si todas las réplicas fallaron
    raise HTTPException(
        status_code=503,
        detail=f"No se pudo leer el archivo desde ningún DataNode disponible. Último error: {last_error}"
    )


# ========== ENDPOINTS INTERNOS PARA REPLICACIÓN ==========

@app.post("/internal/replicate")
def internal_replicate(data: Dict):
    """Endpoint interno para recibir replicación del líder"""
    try:
        operation = data.get("operation")
        operation_data = data.get("data")
        term = data.get("term")
        timestamp = data.get("timestamp")
        
        with cluster_lock:
            current_term = cluster_state["term"]
            
            # Actualizar término si es mayor
            if term > current_term:
                cluster_state["term"] = term
                cluster_state["is_leader"] = False
                cluster_state["leader_id"] = data.get("leader_id")
                cluster_state["last_heartbeat_time"] = time.time()
        
        # Aplicar operación localmente
        if operation == "add_file":
            file_id = add_file_metadata(
                name=operation_data["name"],
                tags=operation_data["tags"],
                size=operation_data.get("size"),
                hash_value=operation_data.get("hash"),
                node_id=NODE_ID
            )
            # También guardar las réplicas si están en los datos de la operación
            if file_id and "datanode_ids" in operation_data:
                from namenode.datanode_manager import save_file_replicas
                datanode_ids = operation_data["datanode_ids"]
                if datanode_ids:
                    save_file_replicas(file_id, datanode_ids, node_id_db=NODE_ID)
                    print(f"[NAMENODE] Réplicas replicadas para file_id={file_id}: {datanode_ids}")
        elif operation == "delete_file":
            delete_file_metadata(operation_data["file_id"], node_id=NODE_ID)
        elif operation == "delete_files_by_tags":
            delete_files_by_tags(operation_data["tags"], node_id=NODE_ID)
        elif operation == "add_tags":
            add_tags_to_files(
                operation_data["query_tags"],
                operation_data["new_tags"],
                node_id=NODE_ID
            )
        elif operation == "delete_tags":
            delete_tags_from_files(
                operation_data["query_tags"],
                operation_data["del_tags"],
                node_id=NODE_ID
            )
        
        return {"success": True}
    except Exception as e:
        print(f"[NAMENODE] Error en internal_replicate: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


@app.post("/internal/vote")
def internal_vote(request: VoteRequest):
    """Endpoint interno para votar en elecciones"""
    with cluster_lock:
        if request.term > cluster_state["term"]:
            cluster_state["term"] = request.term
            cluster_state["is_leader"] = False
            cluster_state["leader_id"] = None
            return VoteResponse(granted=True, term=request.term)
        elif request.term == cluster_state["term"] and not cluster_state["is_leader"]:
            return VoteResponse(granted=True, term=request.term)
        else:
            return VoteResponse(granted=False, term=cluster_state["term"])


@app.post("/internal/heartbeat")
def internal_heartbeat(data: Dict):
    """Endpoint interno para recibir heartbeats del líder"""
    term = data.get("term")
    leader_id = data.get("leader_id")
    
    with cluster_lock:
        if term >= cluster_state["term"]:
            cluster_state["term"] = term
            cluster_state["leader_id"] = leader_id
            cluster_state["is_leader"] = False
            cluster_state["last_heartbeat_time"] = time.time()
    
    return {"success": True}

