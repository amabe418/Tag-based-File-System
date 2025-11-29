"""
MetaNameNode - Servicio distribuido con 3 réplicas
Mantiene metadatos de archivos (nombres y etiquetas) con replicación Raft-like
Los archivos físicos se almacenan en DataNodes, no aquí.
"""
from fastapi import FastAPI, HTTPException, Query, UploadFile, Form
from fastapi.responses import FileResponse
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
    
    return {
        "message": "MetaNameNode funcionando",
        **cluster_data,
        "total_files": total_files,
        "total_tags": total_tags
    }


# ========== ENDPOINTS DE METADATOS ==========

@app.post("/files")
def add_file(metadata: FileMetadata):
    """Agrega metadatos de un archivo (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/files",
                json=metadata.dict(),
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Agregar metadatos localmente
    file_id = add_file_metadata(
        name=metadata.name,
        tags=metadata.tags,
        size=metadata.size,
        hash_value=metadata.hash,
        node_id=NODE_ID
    )
    
    if not file_id:
        raise HTTPException(status_code=400, detail="No se pudo agregar el archivo")
    
    # Replicar operación a los seguidores
    operation = OperationLog(
        operation="add_file",
        data={
            "name": metadata.name,
            "tags": metadata.tags,
            "size": metadata.size,
            "hash": metadata.hash
        },
        term=cluster_state["term"],
        timestamp=time.time()
    )
    
    with log_lock:
        operation_log.append(operation)
    
    replicate_to_peers(operation)
    
    return {
        "success": True,
        "message": f"Metadatos de '{metadata.name}' agregados correctamente",
        "file_id": file_id
    }


@app.get("/files")
def list_files(tags: Optional[List[str]] = Query(None)):
    """Lista archivos por tags (cualquier nodo puede responder)"""
    files = query_files(query_tags=tags, node_id=NODE_ID)
    
    result = []
    for file_id, name, tags_str in files:
        tags_list = tags_str.split(",") if tags_str else []
        result.append({
            "id": file_id,
            "name": name,
            "tags": tags_list
        })
    
    return {"files": result}


@app.get("/files/{file_id}")
def get_file(file_id: int):
    """Obtiene metadatos de un archivo específico"""
    file_data = get_file_by_id(file_id, node_id=NODE_ID)
    if not file_data:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return file_data


@app.delete("/files/{file_id}")
def delete_file(file_id: int):
    """Elimina metadatos de un archivo (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.delete(f"{leader_url}/files/{file_id}", timeout=5)
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    file_data = get_file_by_id(file_id, node_id=NODE_ID)
    if not file_data:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
    deleted = delete_file_metadata(file_id, node_id=NODE_ID)
    
    if deleted:
        # Replicar operación
        operation = OperationLog(
            operation="delete_file",
            data={"file_id": file_id},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        replicate_to_peers(operation)
    
    return {"success": deleted, "message": "Metadatos eliminados" if deleted else "No se encontró el archivo"}


@app.delete("/files")
def delete_files_by_query(tags: str = Query(...)):
    """Elimina archivos por tags (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.delete(f"{leader_url}/files", params={"tags": tags}, timeout=5)
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
    
    return {"success": deleted, "message": "Archivos eliminados" if deleted else "No se encontraron coincidencias"}


@app.post("/files/tags/add")
def add_tags(query: str = Query(...), new_tags: str = Query(...)):
    """Agrega etiquetas a archivos (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/files/tags/add",
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


@app.post("/files/tags/delete")
def delete_tags(query: str = Query(...), del_tags: str = Query(...)):
    """Elimina etiquetas de archivos (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/files/tags/delete",
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
    
    # Agregar solo metadatos (el archivo físico se enviará a DataNodes más adelante)
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
    
    # Replicar operación
    operation = OperationLog(
        operation="add_file",
        data={
            "name": file.filename,
            "tags": tag_list,
            "size": file_size,
            "hash": hash_value
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
        "message": f"Metadatos de '{file.filename}' agregados correctamente (archivo pendiente de almacenar en DataNodes)"
    }


@app.get("/list")
def list_files_compat(tags: Optional[List[str]] = Query(None)):
    """Endpoint de compatibilidad: lista archivos (cualquier nodo puede responder)"""
    files = query_files(query_tags=tags, node_id=NODE_ID)
    
    result = []
    for file_id, name, tags_str in files:
        tags_list = tags_str.split(",") if tags_str else []
        # Formato compatible con el cliente antiguo
        result.append({
            "id": file_id,
            "name": name,
            "tags": tags_list,
            "path": ""  # No hay path físico en el namenode
        })
    
    return {"files": result}


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
    Endpoint de compatibilidad: descarga de archivo.
    NOTA: Por ahora retorna error porque los archivos físicos están en DataNodes.
    Esto se implementará cuando se integren los DataNodes.
    """
    raise HTTPException(
        status_code=501,
        detail="La descarga de archivos se implementará cuando los DataNodes estén disponibles. Por ahora solo se manejan metadatos."
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
            add_file_metadata(
                name=operation_data["name"],
                tags=operation_data["tags"],
                size=operation_data.get("size"),
                hash_value=operation_data.get("hash"),
                node_id=NODE_ID
            )
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

