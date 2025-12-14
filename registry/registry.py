"""
Registry Service - Servicio de descubrimiento distribuido con protocolo Gossip
Mantiene registro de todos los servidores de datos activos mediante diseminación de información.
Cada nodo propaga su estado a otros nodos del cluster de forma aleatoria y periódica.
"""
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import time
import threading
from datetime import datetime
import os
import requests
from contextlib import asynccontextmanager
import sys
from pathlib import Path
import random

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import verify_service_token, validate_service_request
from security.rate_limit import RateLimitMiddleware

app = FastAPI(title="TBFS Registry Service (Gossip-based)")

# Almacenamiento en memoria de servidores registrados
# Estructura: {server_id: {url, last_heartbeat, status, registered_at, version}}
# version: versión del registro (incrementa en cada actualización para resolver conflictos)
servers = {}
servers_lock = threading.Lock()

# Estado del cluster para Gossip
cluster_state = {
    "node_id": os.getenv("NODE_ID", "registry-1"),
    "peers": [],  # Lista de otros nodos del cluster
    "peer_status": {},  # {peer_id: {"last_seen": float, "status": "alive"/"suspected"/"dead"}}
    "version": 0,  # Versión del estado local (incrementa en cada cambio)
}
cluster_lock = threading.Lock()

# Configuración
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "10"))
GOSSIP_INTERVAL = int(os.getenv("GOSSIP_INTERVAL", "3"))  # Intervalo entre rondas de gossip
GOSSIP_FANOUT = int(os.getenv("GOSSIP_FANOUT", "3"))  # Número de peers a contactar en cada ronda
PEER_FAILURE_TIMEOUT = int(os.getenv("PEER_FAILURE_TIMEOUT", "30"))  # Tiempo antes de marcar peer como muerto
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "9000"))

# Parsear lista de peers desde variable de entorno
PEERS_ENV = os.getenv("PEERS", "")
if PEERS_ENV:
    peers_list = [p.strip() for p in PEERS_ENV.split(",") if p.strip()]
    cluster_state["peers"] = peers_list
    # Inicializar estado de peers
    for peer in peers_list:
        cluster_state["peer_status"][peer] = {
            "last_seen": 0.0,
            "status": "unknown"
        }


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


class GossipState(BaseModel):
    """Estado del nodo para intercambio en Gossip"""
    node_id: str
    servers: Dict  # {server_id: {url, last_heartbeat, status, registered_at, version}}
    version: int  # Versión del estado
    timestamp: float  # Timestamp de cuando se generó este estado


class GossipExchange(BaseModel):
    """Intercambio de estado en Gossip"""
    sender_id: str
    sender_version: int
    servers: Dict
    known_peers: List[str]  # Lista de peers conocidos por el nodo remoto
    timestamp: float


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


def update_peer_status(peer_id: str, alive: bool):
    """
    Actualiza el estado de un peer basado en si está vivo o no.
    
    Args:
        peer_id: ID del peer
        alive: True si el peer está vivo, False si no
    """
    with cluster_lock:
        if peer_id not in cluster_state["peer_status"]:
            cluster_state["peer_status"][peer_id] = {
                "last_seen": 0.0,
                "status": "unknown"
            }
        
        peer_info = cluster_state["peer_status"][peer_id]
        
        if alive:
            peer_info["last_seen"] = time.time()
            peer_info["status"] = "alive"
        else:
            # Si ha pasado mucho tiempo sin ver al peer, marcarlo como suspected o dead
            time_since_seen = time.time() - peer_info["last_seen"]
            if time_since_seen > PEER_FAILURE_TIMEOUT:
                if peer_info["status"] == "alive":
                    peer_info["status"] = "suspected"
                    print(f"[REGISTRY] Peer {peer_id} marcado como suspected (sin contacto por {time_since_seen:.1f}s)")
                elif peer_info["status"] == "suspected" and time_since_seen > (PEER_FAILURE_TIMEOUT * 2):
                    peer_info["status"] = "dead"
                    print(f"[REGISTRY] Peer {peer_id} marcado como dead (sin contacto por {time_since_seen:.1f}s)")


def get_peers_to_contact() -> List[str]:
    """
    Obtiene la lista de peers a contactar para gossip.
    Incluye peers con estado "alive", "unknown" (para bootstrap) y los configurados inicialmente.
    
    Returns:
        Lista de peer IDs que se deben contactar (excluyendo este nodo)
    """
    with cluster_lock:
        # Obtener todos los peers únicos conocidos (excluyendo este nodo)
        unique_peers = set([p for p in cluster_state["peers"] if p != cluster_state["node_id"]])
        peers_to_contact = []
        
        # Incluir todos los peers configurados (incluso si no tienen estado aún)
        for peer_id in unique_peers:
            if peer_id != cluster_state["node_id"]:
                # Incluir si está en la lista de peers configurados
                # o si tiene estado "alive" o "unknown" (para bootstrap)
                if peer_id not in cluster_state["peer_status"]:
                    peers_to_contact.append(peer_id)
                elif cluster_state["peer_status"][peer_id]["status"] in ["alive", "unknown"]:
                    peers_to_contact.append(peer_id)
                # No incluir si está marcado como "dead" o "suspected" (a menos que sea uno configurado inicialmente)
                # Pero para bootstrap, intentamos contactar incluso los "suspected" después de un tiempo
        
        return list(set(peers_to_contact))  # Eliminar duplicados


def merge_servers_state(local_servers: Dict, remote_servers: Dict) -> Dict:
    """
    Hace merge de dos estados de servidores usando last-write-wins.
    Si un servidor tiene la misma versión pero diferentes datos, usa el más reciente según timestamp.
    
    Args:
        local_servers: Servidores locales
        remote_servers: Servidores remotos recibidos
    
    Returns:
        Dict con servidores mergeados
    """
    merged = local_servers.copy()
    updates = 0
    additions = 0
    
    for server_id, remote_info in remote_servers.items():
        if server_id not in merged:
            # Servidor nuevo, agregarlo
            merged[server_id] = remote_info.copy()  # Hacer copia para evitar referencias
            additions += 1
        else:
            # Servidor existe en ambos, resolver conflicto
            local_info = merged[server_id]
            
            # Comparar versiones (mayor versión gana)
            local_version = local_info.get("version", 0)
            remote_version = remote_info.get("version", 0)
            
            if remote_version > local_version:
                # Versión remota es más nueva, usar esa
                merged[server_id] = remote_info.copy()  # Hacer copia
                updates += 1
            elif remote_version == local_version:
                # Misma versión, usar el que tiene heartbeat más reciente
                local_heartbeat = local_info.get("last_heartbeat", 0)
                remote_heartbeat = remote_info.get("last_heartbeat", 0)
                
                if remote_heartbeat > local_heartbeat:
                    merged[server_id] = remote_info.copy()  # Hacer copia
                    updates += 1
            # Si local_version > remote_version, mantener local (ya está en merged)
    
    if additions > 0 or updates > 0:
        print(f"[REGISTRY] [GOSSIP] Merge completado - Nuevos: {additions}, Actualizados: {updates}, Total servidores: {len(merged)}")
    
    return merged


def gossip_exchange(peer_id: str) -> bool:
    """
    Realiza un intercambio de estado Gossip con un peer.
    
    Args:
        peer_id: ID del peer con el que hacer intercambio
    
    Returns:
        True si el intercambio fue exitoso, False en caso contrario
    """
    try:
        peer_url = get_peer_url(peer_id)
        
        # Obtener token de servicio para autenticación
        try:
            from security.service_auth import generate_service_token
            service_token = generate_service_token(cluster_state["node_id"], "service")
            print(f"[REGISTRY] [GOSSIP] Token generado para {cluster_state['node_id']}")
        except Exception as e:
            print(f"[REGISTRY] [GOSSIP] Error generando token, usando token de entorno: {e}")
            service_token = os.getenv("REGISTRY_SERVICE_TOKEN", "registry-service-token")
        
        # Obtener estado local
        with servers_lock:
            local_servers = servers.copy()
        with cluster_lock:
            local_version = cluster_state["version"]
            sender_id = cluster_state["node_id"]
        
        # Obtener lista de peers conocidos (incluyendo nosotros mismos)
        with cluster_lock:
            known_peers = list(set(cluster_state["peers"] + [sender_id]))
        
        # Enviar nuestro estado al peer
        response = requests.post(
            f"{peer_url}/internal/gossip",
            json={
                "sender_id": sender_id,
                "sender_version": local_version,
                "servers": local_servers,
                "known_peers": known_peers,
                "timestamp": time.time()
            },
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=3
        )
        
        if response.status_code == 200:
            # Recibir estado del peer
            data = response.json()
            remote_servers = data.get("servers", {})
            remote_version = data.get("version", 0)
            remote_known_peers = data.get("known_peers", [])
            
            # Hacer merge con estado remoto
            servers_changed = False
            with servers_lock:
                local_servers_before = servers.copy()
                merged_servers = merge_servers_state(servers, remote_servers)
                # Verificar si hubo cambios comparando el contenido
                if merged_servers != local_servers_before:
                    servers.clear()
                    servers.update(merged_servers)
                    servers_changed = True
                    print(f"[REGISTRY] [GOSSIP] Estado de servidores actualizado: {len(servers)} servidores (antes: {len(local_servers_before)})")
            
            # Actualizar versión del cluster si la remota es mayor o si hubo cambios en servidores
            with cluster_lock:
                # Actualizar versión al máximo entre local y remota
                max_version = max(local_version, remote_version)
                if servers_changed:
                    # Si hubo cambios, asegurar que la versión refleje el estado más actualizado
                    max_version = max(max_version, cluster_state["version"] + 1)
                
                if max_version > cluster_state["version"]:
                    old_version = cluster_state["version"]
                    cluster_state["version"] = max_version
                    print(f"[REGISTRY] [GOSSIP] Versión actualizada de {old_version} a {max_version} (de {peer_id}, cambios: {servers_changed})")
                
                # Agregar nuevos peers descubiertos a la lista de peers conocidos
                # Remover duplicados y el node_id primero
                cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
                cluster_state["peers"] = list(set(cluster_state["peers"]))
                our_peers = set(cluster_state["peers"] + [cluster_state["node_id"]])
                for remote_peer in remote_known_peers:
                    # Nunca agregar este nodo a la lista de peers
                    if remote_peer != cluster_state["node_id"] and remote_peer not in our_peers:
                        if remote_peer not in cluster_state["peers"]:
                            cluster_state["peers"].append(remote_peer)
                            print(f"[REGISTRY] [GOSSIP] Nuevo peer descubierto: {remote_peer}")
                        # Inicializar estado del nuevo peer si no existe
                        if remote_peer not in cluster_state["peer_status"]:
                            cluster_state["peer_status"][remote_peer] = {
                                "last_seen": 0.0,
                                "status": "unknown"
                            }
                # Asegurar que no hay duplicados ni el node_id después de agregar
                cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
                cluster_state["peers"] = list(set(cluster_state["peers"]))
            
            # Actualizar estado del peer como vivo
            update_peer_status(peer_id, True)
            
            print(f"[REGISTRY] [GOSSIP] Intercambio exitoso con {peer_id} - Servidores locales: {len(servers)}, Servidores remotos recibidos: {len(remote_servers)}, Cambios: {servers_changed}")
            return True
        else:
            update_peer_status(peer_id, False)
            print(f"[REGISTRY] [GOSSIP] Error en intercambio con {peer_id}: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        update_peer_status(peer_id, False)
        print(f"[REGISTRY] [GOSSIP] Error en intercambio con {peer_id}: {e}")
        return False


def gossip_loop():
    """
    Loop principal de Gossip que periódicamente selecciona peers aleatorios y hace intercambio.
    """
    # Esperar un poco al inicio para que todos los nodos estén listos
    time.sleep(5)
    
    # Intentar contacto inicial con todos los peers configurados
    print(f"[REGISTRY] [GOSSIP] Iniciando loop de gossip...")
    with cluster_lock:
        initial_peers = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
    
    if initial_peers:
        print(f"[REGISTRY] [GOSSIP] Intentando contacto inicial con {len(initial_peers)} peers: {initial_peers}")
        for peer in initial_peers:
            # Inicializar estado si no existe
            update_peer_status(peer, False)  # Esto lo inicializa como "unknown"
            thread = threading.Thread(target=gossip_exchange, args=(peer,), daemon=True)
            thread.start()
    
    while True:
        try:
            time.sleep(GOSSIP_INTERVAL)
            
            # Obtener lista de peers a contactar (incluye "alive" y "unknown" para bootstrap)
            peers_to_contact = get_peers_to_contact()
            
            if not peers_to_contact:
                # Si no hay peers, verificar si hay peers configurados que no están en peer_status
                with cluster_lock:
                    configured_peers = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
                    for peer in configured_peers:
                        if peer not in cluster_state["peer_status"]:
                            update_peer_status(peer, False)  # Inicializar como "unknown"
                            peers_to_contact.append(peer)
            
            if not peers_to_contact:
                print(f"[REGISTRY] [GOSSIP] No hay peers para contactar")
                continue
            
            # Seleccionar número aleatorio de peers (fanout)
            num_peers = min(GOSSIP_FANOUT, len(peers_to_contact))
            selected_peers = random.sample(peers_to_contact, num_peers) if len(peers_to_contact) > 0 else []
            
            print(f"[REGISTRY] [GOSSIP] Contactando {len(selected_peers)} de {len(peers_to_contact)} peers disponibles: {selected_peers}")
            
            # Hacer intercambio con cada peer seleccionado
            for peer in selected_peers:
                # No hacer gossip con nosotros mismos
                if peer == cluster_state["node_id"]:
                    continue
                
                # Ejecutar en un hilo separado para no bloquear
                thread = threading.Thread(target=gossip_exchange, args=(peer,), daemon=True)
                thread.start()
                
        except Exception as e:
            print(f"[REGISTRY] [GOSSIP] Error en gossip loop: {e}")
            import traceback
            traceback.print_exc()


def cleanup_inactive_servers():
    """Hilo que limpia servidores que no han enviado heartbeat"""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        
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
                # Incrementar versión cuando se marca como inactivo
                servers[server_id]["version"] = servers[server_id].get("version", 0) + 1
            
            if inactive:
                # Incrementar versión del estado del cluster
                with cluster_lock:
                    cluster_state["version"] += 1


def monitor_peer_status():
    """
    Monitorea periódicamente el estado de los peers y actualiza su estado.
    """
    while True:
        time.sleep(10)  # Verificar cada 10 segundos
        
        current_time = time.time()
        with cluster_lock:
            peers_to_check = list(cluster_state["peer_status"].keys())
        
        for peer_id in peers_to_check:
            # No verificar nosotros mismos
            if peer_id == cluster_state["node_id"]:
                continue
            
            # Verificar si el peer sigue vivo
            is_alive = False
            try:
                peer_url = get_peer_url(peer_id)
                response = requests.get(f"{peer_url}/", timeout=2)
                if response.status_code == 200:
                    is_alive = True
            except Exception:
                is_alive = False
            
            update_peer_status(peer_id, is_alive)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el ciclo de vida de la aplicación"""
    # Startup
    print(f"[REGISTRY] Nodo iniciado: {cluster_state['node_id']}")
    print(f"[REGISTRY] Peers configurados: {cluster_state['peers']}")
    print(f"[REGISTRY] Protocolo: Gossip (fanout={GOSSIP_FANOUT}, interval={GOSSIP_INTERVAL}s)")
    
    # Iniciar hilos
    cleanup_thread = threading.Thread(target=cleanup_inactive_servers, daemon=True)
    cleanup_thread.start()
    
    gossip_thread = threading.Thread(target=gossip_loop, daemon=True)
    gossip_thread.start()
    print(f"[REGISTRY] Gossip loop iniciado")
    
    monitor_thread = threading.Thread(target=monitor_peer_status, daemon=True)
    monitor_thread.start()
    print(f"[REGISTRY] Monitor de peers iniciado")
    
    yield
    
    # Shutdown
    print(f"[REGISTRY] Nodo deteniéndose...")


app = FastAPI(title="TBFS Registry Service (Gossip-based)", lifespan=lifespan)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurar rate limiting
app.add_middleware(RateLimitMiddleware, max_requests=100, time_window=60)


@app.get("/")
def root():
    """Endpoint de estado del registry"""
    with servers_lock:
        servers_data = {
            "total_servers": len(servers),
            "active_servers": sum(1 for s in servers.values() if s["status"] == "active"),
            "inactive_servers": sum(1 for s in servers.values() if s["status"] == "inactive")
        }
    
    with cluster_lock:
        # Filtrar el node_id de la lista de peers (no debe estar incluido)
        unique_peers = set([p for p in cluster_state["peers"] if p != cluster_state["node_id"]])
        # total_peers = número de peers (excluyendo este nodo) + este nodo
        total_known_peers = len(unique_peers) + 1  # +1 por este nodo
        
        # Contar peers vivos: este nodo siempre está vivo + otros peers con estado "alive"
        alive_count = 1  # Este nodo siempre está vivo
        for peer_id in unique_peers:
            if peer_id in cluster_state["peer_status"]:
                if cluster_state["peer_status"][peer_id]["status"] == "alive":
                    alive_count += 1
        
        cluster_data = {
            "node_id": cluster_state["node_id"],
            "protocol": "gossip",
            "version": cluster_state["version"],
            "total_peers": total_known_peers,
            "alive_peers": alive_count
        }
    
    return {
        "message": "Registry Service funcionando (Gossip)",
        **cluster_data,
        **servers_data
    }


@app.post("/register")
def register_server(
    registration: ServerRegistration,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Registra un nuevo servidor de datos (requiere token de servicio).
    En Gossip, cualquier nodo puede procesar registros (no hay líder).
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    if not validate_service_request(registration.server_id, token):
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    server_id = registration.server_id
    full_url = get_full_url(registration.url, registration.port)
    server_ip = registration.ip
    current_time = time.time()
    
    with servers_lock:
        # Verificar si ya existe un servidor con este ID
        is_new = server_id not in servers
        
        # Evitar IDs duplicados: si ya existe un servidor con este ID, actualizamos su información
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
        
        # Registrar o actualizar el servidor
        servers[server_id] = {
            "url": full_url,
            "ip": server_ip,
            "last_heartbeat": current_time,
            "status": "active",
            "registered_at": current_time if is_new else servers[server_id].get("registered_at", current_time),
            "version": (servers[server_id].get("version", 0) + 1) if not is_new else 1
        }
    
    # Incrementar versión del estado del cluster
    with cluster_lock:
        cluster_state["version"] += 1
    
    action = "registrado" if is_new else "actualizado"
    print(f"[REGISTRY] Servidor {action}: {server_id} -> {full_url}")
    
    return {
        "success": True,
        "message": f"Servidor {action} correctamente",
        "server_id": server_id,
        "url": full_url
    }


@app.post("/heartbeat")
def receive_heartbeat(
    heartbeat: Heartbeat,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Recibe heartbeat de un servidor (requiere token de servicio).
    En Gossip, cualquier nodo puede procesar heartbeats (no hay líder).
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    if not validate_service_request(heartbeat.server_id, token):
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    server_id = heartbeat.server_id
    current_time = time.time()
    
    with servers_lock:
        if server_id not in servers:
            raise HTTPException(
                status_code=404,
                detail=f"Servidor {server_id} no está registrado. Debe registrarse primero."
            )
        
        # Actualizar heartbeat y versión
        servers[server_id]["last_heartbeat"] = current_time
        servers[server_id]["version"] = servers[server_id].get("version", 0) + 1
        
        if servers[server_id]["status"] == "inactive":
            servers[server_id]["status"] = "active"
            print(f"[REGISTRY] Servidor recuperado: {server_id}")
    
    # Incrementar versión del estado del cluster
    with cluster_lock:
        cluster_state["version"] += 1
    
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
                ip=info.get("ip"),
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
            ip=info.get("ip"),
            status=info["status"],
            last_heartbeat=datetime.fromtimestamp(info["last_heartbeat"]).isoformat(),
            registered_at=datetime.fromtimestamp(info["registered_at"]).isoformat(),
            uptime_seconds=uptime
        )


@app.get("/registries")
def list_registries(
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Lista todos los registries conocidos en el cluster.
    Útil para que los MetaNameNodes descubran todos los registries disponibles.
    Requiere autenticación de servicio.
    """
    # Verificar autenticación de servicio (similar a otros endpoints)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        # Intentar validación con token pre-compartido
        from security.service_auth import get_service_token_for_service
        valid_token = False
        # Intentar con diferentes tipos de servicios
        for service_type in ["namenode", "datanode", "client", "registry"]:
            expected_token = get_service_token_for_service(service_type)
            if expected_token and token == expected_token:
                valid_token = True
                break
        
        if not valid_token:
            raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    with cluster_lock:
        # Obtener todos los registries conocidos (incluyendo este nodo)
        all_registries = set(cluster_state["peers"] + [cluster_state["node_id"]])
        registries_info = []
        
        for registry_id in sorted(all_registries):
            # Obtener estado del registry
            status = "unknown"
            last_seen = None
            if registry_id in cluster_state["peer_status"]:
                status = cluster_state["peer_status"][registry_id]["status"]
                last_seen = cluster_state["peer_status"][registry_id]["last_seen"]
            elif registry_id == cluster_state["node_id"]:
                # Este nodo siempre está vivo
                status = "alive"
                last_seen = time.time()
            
            # Construir URL del registry
            registry_url = get_peer_url(registry_id)
            
            registries_info.append({
                "registry_id": registry_id,
                "url": registry_url,
                "status": status,
                "last_seen": datetime.fromtimestamp(last_seen).isoformat() if last_seen else None,
                "is_local": registry_id == cluster_state["node_id"]
            })
        
        return {
            "registries": registries_info,
            "total": len(registries_info),
            "node_id": cluster_state["node_id"]
        }


# Endpoint interno para Gossip

@app.post("/internal/gossip")
def internal_gossip(
    exchange: GossipExchange,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Endpoint interno para intercambio de estado en protocolo Gossip.
    Recibe estado de un peer y retorna el estado local.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        print(f"[REGISTRY] [GOSSIP] Error: Se requiere token de servicio")
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        print(f"[REGISTRY] [GOSSIP] Error: Token de servicio inválido o expirado")
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro registry
    service_id = payload.get("service_id") or payload.get("sub", "")
    # Aceptar "registry-", "tbfs-registry-", o cualquier ID que contenga "registry"
    if "registry" not in service_id.lower():
        print(f"[REGISTRY] [GOSSIP] Error: service_id '{service_id}' no es un registry")
        raise HTTPException(status_code=403, detail="Solo registries pueden hacer gossip")
    
    sender_id = exchange.sender_id
    
    # Actualizar estado del peer como vivo
    update_peer_status(sender_id, True)
    
    # Obtener estado local y hacer merge
    servers_changed = False
    with servers_lock:
        local_servers_before = servers.copy()
        local_servers = servers.copy()
        # Hacer merge con estado recibido
        merged_servers = merge_servers_state(local_servers, exchange.servers)
        # Verificar si hubo cambios comparando el contenido
        if merged_servers != local_servers_before:
            servers.clear()
            servers.update(merged_servers)
            servers_changed = True
            print(f"[REGISTRY] [GOSSIP] Estado de servidores actualizado desde {sender_id}: {len(servers)} servidores (antes: {len(local_servers_before)})")
    
    # Actualizar versión y peers conocidos
    with cluster_lock:
        local_version = cluster_state["version"]
        # Actualizar versión al máximo entre local y remota
        max_version = max(local_version, exchange.sender_version)
        if servers_changed:
            # Si hubo cambios, asegurar que la versión refleje el estado más actualizado
            max_version = max(max_version, local_version + 1)
        
        if max_version > cluster_state["version"]:
            old_version = cluster_state["version"]
            cluster_state["version"] = max_version
            print(f"[REGISTRY] [GOSSIP] Versión actualizada de {old_version} a {max_version} (de {sender_id}, cambios: {servers_changed})")
        local_version = cluster_state["version"]
        
        # Remover duplicados y el node_id primero
        cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
        cluster_state["peers"] = list(set(cluster_state["peers"]))
        
        # Agregar nuevos peers descubiertos a la lista de peers conocidos
        our_peers = set(cluster_state["peers"] + [cluster_state["node_id"]])
        for remote_peer in exchange.known_peers:
            # Nunca agregar este nodo a la lista de peers
            if remote_peer != cluster_state["node_id"] and remote_peer not in our_peers:
                if remote_peer not in cluster_state["peers"]:
                    cluster_state["peers"].append(remote_peer)
                    print(f"[REGISTRY] [GOSSIP] Nuevo peer descubierto: {remote_peer}")
                # Inicializar estado del nuevo peer si no existe
                if remote_peer not in cluster_state["peer_status"]:
                    cluster_state["peer_status"][remote_peer] = {
                        "last_seen": 0.0,
                        "status": "unknown"
                    }
        
        # Asegurar que no hay duplicados ni el node_id después de agregar
        cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
        cluster_state["peers"] = list(set(cluster_state["peers"]))
        
        # Preparar lista de peers conocidos para retornar (sin duplicados, incluyendo este nodo)
        known_peers = list(set([p for p in cluster_state["peers"] if p != cluster_state["node_id"]] + [cluster_state["node_id"]]))
    
    # Retornar nuestro estado actualizado
    with servers_lock:
        current_servers = servers.copy()
    
    return {
        "success": True,
        "node_id": cluster_state["node_id"],
        "version": local_version,
        "servers": current_servers,
        "known_peers": known_peers,
        "timestamp": time.time()
    }