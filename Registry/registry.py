"""
Registry Service - Servicio de descubrimiento distribuido con 3 nodos
Mantiene registro de todos los servidores de datos activos con replicación
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict
import time
import threading
from datetime import datetime
import os
import requests
from contextlib import asynccontextmanager

app = FastAPI(title="TBFS Registry Service (Distributed)")

# Almacenamiento en memoria de servidores registrados
# Estructura: {server_id: {url, last_heartbeat, status, registered_at}}
servers = {}
servers_lock = threading.Lock()

# Estado del cluster
cluster_state = {
    "node_id": os.getenv("NODE_ID", "registry-1"),
    "is_leader": False,
    "leader_id": None,
    "term": 0,  # Término de liderazgo
    "last_heartbeat_time": 0,
    "peers": []  # Lista de otros nodos del cluster
}
cluster_lock = threading.Lock()

# Configuración
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "10"))
LEADER_HEARTBEAT_INTERVAL = int(os.getenv("LEADER_HEARTBEAT_INTERVAL", "5"))
ELECTION_TIMEOUT = int(os.getenv("ELECTION_TIMEOUT", "15"))
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "9000"))

# Parsear lista de peers desde variable de entorno
PEERS_ENV = os.getenv("PEERS", "")
if PEERS_ENV:
    cluster_state["peers"] = [p.strip() for p in PEERS_ENV.split(",") if p.strip()]


class ServerRegistration(BaseModel):
    server_id: str
    url: str
    port: int = 8000
    ip: Optional[str] = None  # IP del servidor como alternativa de acceso


class Heartbeat(BaseModel):
    server_id: str


class ServerInfo(BaseModel):
    server_id: str
    url: str
    ip: Optional[str] = None  # IP del servidor como alternativa de acceso
    status: str
    last_heartbeat: str
    registered_at: str
    uptime_seconds: int


class ReplicationData(BaseModel):
    servers: Dict
    term: int
    leader_id: str


class VoteRequest(BaseModel):
    candidate_id: str
    term: int


class VoteResponse(BaseModel):
    granted: bool
    term: int


def get_full_url(url: str, port: int) -> str:
    """Construye la URL completa del servidor"""
    if not url.startswith("http"):
        url = f"http://{url}"
    if port != 80 and port != 443:
        url = f"{url}:{port}"
    return url


def get_peer_url(peer: str) -> str:
    """Obtiene la URL completa de un peer"""
    if not peer.startswith("http"):
        return f"http://{peer}:{REGISTRY_PORT}"
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


def replicate_to_peers(servers_data: Dict, term: int):
    """Replica el estado a los peers del cluster"""
    with cluster_lock:
        peers = cluster_state["peers"].copy()
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
                    "servers": servers_data,
                    "term": term,
                    "leader_id": leader_id
                },
                timeout=3
            )
            if response.status_code == 200:
                success_count += 1
        except Exception as e:
            print(f"[REGISTRY] Error replicando a {peer}: {e}")
    
    # Se necesita mayoría (quorum): al menos 2 de 3 nodos
    total_nodes = len(peers) + 1  # +1 por este nodo
    quorum = (total_nodes // 2) + 1
    replicated = success_count + 1 >= quorum  # +1 por este nodo
    
    # Si solo queda este nodo disponible, la replicación es exitosa (tolerancia a fallos)
    if success_count == 1 and total_nodes > 1:
        # Este es el único nodo disponible, pero hay otros configurados
        # Esto es normal en caso de fallos, no es un warning crítico
        print(f"[REGISTRY] Solo este nodo está disponible ({success_count + 1}/{total_nodes} nodos). Continuando operación (tolerancia a fallos activa).")
        return True
    
    if not replicated:
        print(f"[REGISTRY] WARNING: Solo se replicó a {success_count + 1}/{total_nodes} nodos (quorum: {quorum})")
    
    return replicated


def request_vote(candidate_id: str, term: int) -> bool:
    """Solicita votos para elección de líder"""
    with cluster_lock:
        peers = cluster_state["peers"].copy()
    
    # Si no hay peers, este nodo es el único y es líder
    if not peers:
        return True
    
    votes = 1  # Voto propio
    successful_contacts = 1  # Contamos este nodo
    
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
            print(f"[REGISTRY] Error solicitando voto a {peer}: {e}")
    
    total_nodes = len(peers) + 1
    quorum = (total_nodes // 2) + 1
    
    # Si tenemos mayoría de votos Y al menos quorum de nodos respondieron
    if votes >= quorum and successful_contacts >= quorum:
        return True
    
    # Si no pudimos contactar a ningún peer, pero este nodo está activo,
    # asumimos que es el único disponible y se convierte en líder
    # Esto permite tolerancia a fallos: si 2 de 3 nodos fallan, el restante sigue funcionando
    if successful_contacts == 1:
        print(f"[REGISTRY] No se pudo contactar a ningún peer. Este nodo es el único disponible, convirtiéndose en líder.")
        return True
    
    print(f"[REGISTRY] Votos obtenidos: {votes}/{quorum}, Nodos contactados: {successful_contacts}/{total_nodes}")
    return False


def start_election():
    """Inicia una elección de líder"""
    with cluster_lock:
        cluster_state["term"] += 1
        candidate_id = cluster_state["node_id"]
        term = cluster_state["term"]
        peers = cluster_state["peers"].copy()
        cluster_state["is_leader"] = False
        cluster_state["leader_id"] = None
    
    # Si no hay peers, este nodo es automáticamente el líder (modo desarrollo)
    if not peers:
        with cluster_lock:
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[REGISTRY] Modo desarrollo: nodo único, automáticamente líder (término {term})")
        return True
    
    print(f"[REGISTRY] Iniciando elección (término {term})...")
    
    if request_vote(candidate_id, term):
        with cluster_lock:
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[REGISTRY] ¡Elegido como líder! (término {term})")
        return True
    else:
        print(f"[REGISTRY] No se obtuvo mayoría en la elección (término {term})")
        return False


def leader_heartbeat_loop():
    """Loop que envía heartbeats a los seguidores"""
    while True:
        time.sleep(LEADER_HEARTBEAT_INTERVAL)
        
        if not is_leader():
            continue
        
        with servers_lock:
            servers_copy = servers.copy()
        with cluster_lock:
            term = cluster_state["term"]
        
        # Replicar estado a los seguidores
        replicate_to_peers(servers_copy, term)
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
            if time_since_heartbeat > ELECTION_TIMEOUT:
                print(f"[REGISTRY] Líder inactivo detectado, iniciando elección...")
                start_election()


def cleanup_inactive_servers():
    """Hilo que limpia servidores que no han enviado heartbeat"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        
        # Solo el líder ejecuta limpieza
        if not is_leader():
            continue
        
        current_time = time.time()
        with servers_lock:
            inactive = []
            for server_id, info in list(servers.items()):
                time_since_heartbeat = current_time - info["last_heartbeat"]
                if time_since_heartbeat > HEARTBEAT_TIMEOUT:
                    inactive.append(server_id)
                    print(f"[REGISTRY] Servidor inactivo detectado: {server_id}")
            
            for server_id in inactive:
                servers[server_id]["status"] = "inactive"
        
        # Replicar cambios
        if inactive:
            with servers_lock:
                servers_copy = servers.copy()
            with cluster_lock:
                term = cluster_state["term"]
            replicate_to_peers(servers_copy, term)


def election_retry_loop():
    """Loop que reintenta elecciones si no hay líder"""
    time.sleep(5)  # Esperar a que todos los nodos estén listos
    
    while True:
        time.sleep(ELECTION_TIMEOUT)
        
        with cluster_lock:
            is_leader = cluster_state["is_leader"]
            leader_id = cluster_state["leader_id"]
            peers = cluster_state["peers"].copy()
        
        # Si no hay líder, intentar elección
        if not is_leader and not leader_id:
            print(f"[REGISTRY] No hay líder detectado, intentando elección...")
            start_election()
        elif not is_leader and leader_id:
            # Hay un líder pero no es este nodo, verificar que sigue activo
            try:
                leader_url = get_peer_url(leader_id)
                response = requests.get(f"{leader_url}/", timeout=2)
                if response.status_code != 200:
                    print(f"[REGISTRY] Líder {leader_id} no responde, iniciando elección...")
                    start_election()
            except Exception as e:
                print(f"[REGISTRY] No se puede contactar al líder {leader_id}, iniciando elección...")
                start_election()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el ciclo de vida de la aplicación"""
    # Startup
    print(f"[REGISTRY] Nodo iniciado: {cluster_state['node_id']}")
    print(f"[REGISTRY] Peers: {cluster_state['peers']}")
    
    # Iniciar hilos
    cleanup_thread = threading.Thread(target=cleanup_inactive_servers, daemon=True)
    cleanup_thread.start()
    
    heartbeat_thread = threading.Thread(target=leader_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    
    follower_thread = threading.Thread(target=follower_heartbeat_check, daemon=True)
    follower_thread.start()
    
    election_thread = threading.Thread(target=election_retry_loop, daemon=True)
    election_thread.start()
    
    # Intentar elección inicial después de un delay para que todos los nodos estén listos
    time.sleep(5)
    start_election()
    
    yield
    
    # Shutdown
    print(f"[REGISTRY] Nodo deteniéndose...")


app = FastAPI(title="TBFS Registry Service (Distributed)", lifespan=lifespan)


@app.get("/")
def root():
    """Endpoint de estado del registry"""
    # Obtener datos de forma segura sin bloquear demasiado tiempo
    # Usar timeouts más largos para evitar problemas de bloqueo
    servers_data = {}
    cluster_data = {}
    
    # Obtener datos de servidores con timeout
    if servers_lock.acquire(timeout=2):
        try:
            servers_data = {
                "total_servers": len(servers),
                "active_servers": sum(1 for s in servers.values() if s["status"] == "active"),
                "inactive_servers": sum(1 for s in servers.values() if s["status"] == "inactive")
            }
        finally:
            servers_lock.release()
    else:
        # Si no podemos obtener el lock, usar valores por defecto
        servers_data = {"total_servers": 0, "active_servers": 0, "inactive_servers": 0}
    
    # Obtener datos del cluster con timeout
    # El node_id nunca cambia después de la inicialización, así que es seguro leerlo sin lock
    node_id = cluster_state["node_id"]  # Siempre disponible, se establece al inicio
    
    if cluster_lock.acquire(timeout=2):
        try:
            cluster_data = {
                "node_id": node_id,
                "is_leader": cluster_state["is_leader"],
                "leader_id": cluster_state["leader_id"],
                "term": cluster_state["term"]
            }
        finally:
            cluster_lock.release()
    else:
        # Si no podemos obtener el lock, usar valores seguros
        # node_id siempre está disponible, otros valores pueden estar desactualizados pero no críticos
        cluster_data = {
            "node_id": node_id,
            "is_leader": False,  # Valor conservador si no podemos leer
            "leader_id": None,
            "term": 0
        }
    
    return {
        "message": "Registry Service funcionando",
        **cluster_data,
        **servers_data
    }


@app.post("/register")
def register_server(registration: ServerRegistration):
    """Registra un nuevo servidor de datos (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        # Redirigir al líder
        try:
            response = requests.post(
                f"{leader_url}/register",
                json=registration.dict(),
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    server_id = registration.server_id
    full_url = get_full_url(registration.url, registration.port)
    server_ip = registration.ip
    current_time = time.time()
    
    with servers_lock:
        # Verificar si ya existe un servidor con este ID
        is_new = server_id not in servers
        
        # Evitar IDs duplicados: si ya existe un servidor con este ID, actualizamos su información
        # Esto permite que el mismo servidor se re-registre con información actualizada
        if not is_new:
            existing_server = servers[server_id]
            existing_url = existing_server.get("url")
            existing_ip = existing_server.get("ip")
            
            # Si la URL o IP cambió, es una actualización del mismo servidor
            if existing_url != full_url or existing_ip != server_ip:
                print(f"[REGISTRY] Actualizando servidor existente {server_id}: URL/IP cambiaron")
                print(f"[REGISTRY]   URL anterior: {existing_url} -> nueva: {full_url}")
                print(f"[REGISTRY]   IP anterior: {existing_ip} -> nueva: {server_ip}")
            else:
                print(f"[REGISTRY] Servidor {server_id} ya registrado, actualizando heartbeat")
        
        # Registrar o actualizar el servidor (evitamos IDs duplicados actualizando el existente)
        servers[server_id] = {
            "url": full_url,
            "ip": server_ip,  # Guardar IP como alternativa de acceso
            "last_heartbeat": current_time,
            "status": "active",
            "registered_at": current_time if is_new else servers[server_id].get("registered_at", current_time)
        }
        servers_copy = servers.copy()
    
    with cluster_lock:
        term = cluster_state["term"]
    
    # Replicar a los seguidores
    if not replicate_to_peers(servers_copy, term):
        print(f"[REGISTRY] WARNING: No se pudo replicar a mayoría de nodos")
    
    action = "registrado" if is_new else "actualizado"
    print(f"[REGISTRY] Servidor {action}: {server_id} -> {full_url}")
    
    return {
        "success": True,
        "message": f"Servidor {action} correctamente",
        "server_id": server_id,
        "url": full_url
    }


@app.post("/heartbeat")
def receive_heartbeat(heartbeat: Heartbeat):
    """Recibe heartbeat de un servidor (solo el líder)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/heartbeat",
                json=heartbeat.dict(),
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    server_id = heartbeat.server_id
    current_time = time.time()
    
    with servers_lock:
        if server_id not in servers:
            raise HTTPException(
                status_code=404,
                detail=f"Servidor {server_id} no está registrado. Debe registrarse primero."
            )
        
        servers[server_id]["last_heartbeat"] = current_time
        if servers[server_id]["status"] == "inactive":
            servers[server_id]["status"] = "active"
            print(f"[REGISTRY] Servidor recuperado: {server_id}")
        
        servers_copy = servers.copy()
    
    with cluster_lock:
        term = cluster_state["term"]
    
    # Replicar a los seguidores
    replicate_to_peers(servers_copy, term)
    
    return {"success": True, "message": "Heartbeat recibido"}


@app.get("/servers", response_model=List[ServerInfo])
def list_servers(status: Optional[str] = None):
    """Lista todos los servidores registrados (cualquier nodo puede responder)"""
    current_time = time.time()
    result = []
    
    with servers_lock:
        for server_id, info in servers.items():
            if status and info["status"] != status:
                continue
            
            uptime = int(current_time - info["registered_at"])
            result.append(ServerInfo(
                server_id=server_id,
                url=info["url"],
                ip=info.get("ip"),  # Incluir IP como alternativa de acceso
                status=info["status"],
                last_heartbeat=datetime.fromtimestamp(info["last_heartbeat"]).isoformat(),
                registered_at=datetime.fromtimestamp(info["registered_at"]).isoformat(),
                uptime_seconds=uptime
            ))
    
    return result


@app.get("/servers/active", response_model=List[ServerInfo])
def list_active_servers():
    """Lista solo los servidores activos"""
    return list_servers(status="active")


@app.get("/servers/{server_id}")
def get_server(server_id: str):
    """Obtiene información de un servidor específico"""
    with servers_lock:
        if server_id not in servers:
            raise HTTPException(status_code=404, detail="Servidor no encontrado")
        
        info = servers[server_id]
        current_time = time.time()
        uptime = int(current_time - info["registered_at"])
        
        return ServerInfo(
            server_id=server_id,
            url=info["url"],
            ip=info.get("ip"),  # Incluir IP como alternativa de acceso
            status=info["status"],
            last_heartbeat=datetime.fromtimestamp(info["last_heartbeat"]).isoformat(),
            registered_at=datetime.fromtimestamp(info["registered_at"]).isoformat(),
            uptime_seconds=uptime
        )


# Endpoints internos para replicación y elección

@app.post("/internal/replicate")
def internal_replicate(data: ReplicationData):
    """Endpoint interno para recibir replicación del líder"""
    try:
        with cluster_lock:
            current_term = cluster_state["term"]
            current_leader = cluster_state["leader_id"]
            
            # Actualizar término y líder si es mayor
            if data.term > current_term:
                cluster_state["term"] = data.term
                cluster_state["leader_id"] = data.leader_id
                cluster_state["is_leader"] = False
                cluster_state["last_heartbeat_time"] = time.time()
                print(f"[REGISTRY] Actualizado a término {data.term}, líder: {data.leader_id}")
            elif data.term < current_term:
                # Término menor, rechazar
                return {"success": False, "message": f"Término {data.term} menor que término actual {current_term}"}
            elif data.leader_id != current_leader:
                # Mismo término pero diferente líder, actualizar
                cluster_state["leader_id"] = data.leader_id
                cluster_state["is_leader"] = False
                cluster_state["last_heartbeat_time"] = time.time()
                print(f"[REGISTRY] Líder actualizado: {data.leader_id}")
        
        # Actualizar servidores (fuera del cluster_lock para evitar deadlocks)
        with servers_lock:
            # data.servers es un dict con server_id como keys
            for server_id, server_info in data.servers.items():
                servers[server_id] = server_info
            print(f"[REGISTRY] Estado replicado: {len(data.servers)} servidores")
        
        return {"success": True}
    except Exception as e:
        print(f"[REGISTRY] Error en internal_replicate: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


@app.post("/internal/vote")
def internal_vote(request: VoteRequest):
    """Endpoint interno para votar en elecciones"""
    with cluster_lock:
        # Votar si el término es mayor o igual
        if request.term > cluster_state["term"]:
            cluster_state["term"] = request.term
            cluster_state["is_leader"] = False
            cluster_state["leader_id"] = None
            return VoteResponse(granted=True, term=request.term)
        elif request.term == cluster_state["term"] and not cluster_state["is_leader"]:
            # Ya votamos en este término, pero podemos votar de nuevo si no somos líder
            return VoteResponse(granted=True, term=request.term)
        else:
            return VoteResponse(granted=False, term=cluster_state["term"])
