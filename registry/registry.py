"""
Registry Service - Servicio de descubrimiento distribuido
Mantiene registro de todos los servidores de datos activos con replicación.
Soporta cualquier número de nodos registry en el cluster.
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

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import verify_service_token, validate_service_request
from security.rate_limit import RateLimitMiddleware

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
    "last_election_time": 0,  # Timestamp de la última elección para evitar elecciones frecuentes
    "peers": [],  # Lista de otros nodos del cluster
    # Fase 3: Tracking de conectividad para detección de reunificación
    "peers_connectivity": {},  # {peer_id: {"ever_contacted": bool, "last_seen": float, "currently_connected": bool}}
    "reconciliation_in_progress": False  # Flag para evitar reconciliaciones simultáneas
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
    peers_list = [p.strip() for p in PEERS_ENV.split(",") if p.strip()]
    cluster_state["peers"] = peers_list
    # Fase 3: Inicializar tracking de conectividad para cada peer
    for peer in peers_list:
        if peer not in cluster_state["peers_connectivity"]:
            cluster_state["peers_connectivity"][peer] = {
                "ever_contacted": False,
                "last_seen": 0.0,
                "currently_connected": False
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


def update_peer_connectivity(peer_id: str, connected: bool):
    """
    Fase 3: Actualiza el estado de conectividad con un peer.
    
    Args:
        peer_id: ID del peer
        connected: True si el peer está conectado, False si no
    """
    with cluster_lock:
        if peer_id not in cluster_state["peers_connectivity"]:
            cluster_state["peers_connectivity"][peer_id] = {
                "ever_contacted": False,
                "last_seen": 0.0,
                "currently_connected": False
            }
        
        peer_info = cluster_state["peers_connectivity"][peer_id]
        was_connected = peer_info["currently_connected"]
        
        if connected:
            peer_info["ever_contacted"] = True
            peer_info["last_seen"] = time.time()
            peer_info["currently_connected"] = True
        else:
            peer_info["currently_connected"] = False
        
        # Detectar reunificación: peer que estaba desconectado ahora está conectado
        if not was_connected and connected and peer_info["ever_contacted"]:
            return True  # Indica que se detectó reunificación
        
        return False


def check_peer_connectivity(peer_id: str, timeout: float = 2.0) -> bool:
    """
    Fase 3: Verifica si un peer está disponible.
    
    Args:
        peer_id: ID del peer a verificar
        timeout: Timeout para la verificación
    
    Returns:
        True si el peer está disponible, False en caso contrario
    """
    try:
        peer_url = get_peer_url(peer_id)
        response = requests.get(f"{peer_url}/", timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def detect_network_reunification() -> List[str]:
    """
    Fase 3: Detecta si hay peers que han vuelto a estar disponibles (reunificación).
    
    Returns:
        Lista de peer IDs que han vuelto a estar disponibles
    """
    reunited_peers = []
    
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        peers_connectivity = cluster_state["peers_connectivity"].copy()
    
    for peer in peers:
        if peer not in peers_connectivity:
            continue
        
        peer_info = peers_connectivity[peer]
        was_connected = peer_info.get("currently_connected", False)
        ever_contacted = peer_info.get("ever_contacted", False)
        
        # Solo verificar peers que alguna vez fueron contactados
        if not ever_contacted:
            # Primera vez que intentamos contactar - verificar y marcar
            is_connected = check_peer_connectivity(peer)
            if is_connected:
                update_peer_connectivity(peer, True)
            continue
        
        # Verificar si el peer está disponible ahora
        is_connected = check_peer_connectivity(peer)
        
        # Si estaba desconectado y ahora está conectado, es reunificación
        if not was_connected and is_connected:
            reunited = update_peer_connectivity(peer, True)
            if reunited:
                reunited_peers.append(peer)
                print(f"[REGISTRY] FASE 3: ¡Peer {peer} ha vuelto a estar disponible! (Reunificación detectada)")
        elif is_connected:
            # Actualizar last_seen aunque ya estaba conectado
            update_peer_connectivity(peer, True)
        else:
            # Peer no está disponible
            update_peer_connectivity(peer, False)
    
    return reunited_peers


def get_peer_servers(peer_id: str) -> Optional[Dict]:
    """
    Fase 4: Obtiene el estado de servidores de un peer.
    
    Args:
        peer_id: ID del peer del cual obtener servidores
    
    Returns:
        Dict con servidores o None si hay error
    """
    try:
        peer_url = get_peer_url(peer_id)
        
        # Obtener token de servicio para autenticación
        try:
            from security.service_auth import generate_service_token
            service_token = generate_service_token(cluster_state["node_id"], "service")
        except Exception:
            service_token = os.getenv("REGISTRY_SERVICE_TOKEN", "registry-service-token")
        
        response = requests.get(
            f"{peer_url}/internal/servers",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"[REGISTRY] FASE 4: Error obteniendo servidores de {peer_id}: HTTP {response.status_code}")
            return None
        
        data = response.json()
        return data.get("servers", {})
        
    except Exception as e:
        print(f"[REGISTRY] FASE 4: Error obteniendo servidores de {peer_id}: {e}")
        return None


def merge_servers(local_servers: Dict, peer_servers: Dict) -> Dict:
    """
    Fase 4: Hace merge inteligente de servidores de dos particiones.
    
    Estrategia:
    - Si un servidor solo existe en una partición, agregarlo
    - Si un servidor existe en ambas, mantener el que tiene heartbeat más reciente
    
    Args:
        local_servers: Servidores locales
        peer_servers: Servidores del peer
    
    Returns:
        Dict con servidores mergeados
    """
    merged = local_servers.copy()
    conflicts_resolved = 0
    new_servers = 0
    
    for server_id, peer_info in peer_servers.items():
        if server_id not in merged:
            # Servidor nuevo de la otra partición, agregarlo
            merged[server_id] = peer_info
            new_servers += 1
            print(f"[REGISTRY] FASE 4: Agregando servidor de otra partición: {server_id}")
        else:
            # Servidor existe en ambas particiones, resolver conflicto
            local_info = merged[server_id]
            local_heartbeat = local_info.get("last_heartbeat", 0)
            peer_heartbeat = peer_info.get("last_heartbeat", 0)
            
            # Si el peer tiene heartbeat más reciente, actualizar
            if peer_heartbeat > local_heartbeat:
                # Verificar si la URL cambió (conflicto real)
                if local_info.get("url") != peer_info.get("url"):
                    print(f"[REGISTRY] FASE 4: Conflicto en {server_id}: URL cambió")
                    print(f"[REGISTRY] FASE 4:   Local: {local_info.get('url')} (heartbeat: {local_heartbeat})")
                    print(f"[REGISTRY] FASE 4:   Peer: {peer_info.get('url')} (heartbeat: {peer_heartbeat})")
                    conflicts_resolved += 1
                
                # Usar versión con heartbeat más reciente (last-write-wins)
                merged[server_id] = peer_info
                print(f"[REGISTRY] FASE 4: Actualizando {server_id} con versión más reciente (heartbeat: {peer_heartbeat} > {local_heartbeat})")
            else:
                # Mantener versión local (más reciente)
                print(f"[REGISTRY] FASE 4: Manteniendo versión local de {server_id} (heartbeat: {local_heartbeat} >= {peer_heartbeat})")
    
    print(f"[REGISTRY] FASE 4: Merge completado - Nuevos: {new_servers}, Conflictos resueltos: {conflicts_resolved}")
    return merged


def perform_registry_reconciliation(reunited_peers: List[str]):
    """
    Fase 4: Realiza reconciliación completa del registry después de particionamiento.
    
    Proceso:
    1. Obtener servidores de todos los peers reunificados
    2. Hacer merge inteligente (no sobrescribir, unir)
    3. Resolver conflictos usando last_heartbeat (más reciente gana)
    4. Actualizar estado local con servidores mergeados
    5. Replicar estado mergeado a todos los peers
    
    Args:
        reunited_peers: Lista de peer IDs que han vuelto a estar disponibles
    """
    print(f"[REGISTRY] FASE 4: Iniciando reconciliación completa con {len(reunited_peers)} peers")
    
    with cluster_lock:
        if cluster_state["reconciliation_in_progress"]:
            print(f"[REGISTRY] FASE 4: Reconciliación ya en progreso, ignorando nueva detección")
            return
        
        cluster_state["reconciliation_in_progress"] = True
    
    try:
        # Paso 1: Obtener servidores de todos los peers reunificados
        peer_servers_dict = {}
        for peer in reunited_peers:
            print(f"[REGISTRY] FASE 4: Obteniendo servidores de {peer}...")
            peer_servers = get_peer_servers(peer)
            if peer_servers:
                peer_servers_dict[peer] = peer_servers
                print(f"[REGISTRY] FASE 4: Obtenidos {len(peer_servers)} servidores de {peer}")
            else:
                print(f"[REGISTRY] FASE 4: No se pudo obtener servidores de {peer}")
        
        if not peer_servers_dict:
            print(f"[REGISTRY] FASE 4: No se pudieron obtener servidores de ningún peer, abortando reconciliación")
            return
        
        # Paso 2: Obtener servidores locales
        with servers_lock:
            local_servers = servers.copy()
        
        print(f"[REGISTRY] FASE 4: Servidores locales: {len(local_servers)}")
        
        # Paso 3: Hacer merge con cada peer
        merged_servers = local_servers.copy()
        for peer, peer_servers in peer_servers_dict.items():
            print(f"[REGISTRY] FASE 4: Haciendo merge con {peer}...")
            merged_servers = merge_servers(merged_servers, peer_servers)
        
        # Paso 4: Actualizar estado local
        with servers_lock:
            servers.clear()
            servers.update(merged_servers)
        
        print(f"[REGISTRY] FASE 4: Estado actualizado - Total servidores: {len(merged_servers)}")
        
        # Paso 5: Replicar estado mergeado a todos los peers
        with cluster_lock:
            term = cluster_state["term"]
        
        print(f"[REGISTRY] FASE 4: Replicando estado mergeado a todos los peers...")
        replicate_to_peers(merged_servers, term)
        
        print(f"[REGISTRY] FASE 4: Reconciliación completa finalizada")
        
    finally:
        with cluster_lock:
            cluster_state["reconciliation_in_progress"] = False


def trigger_registry_reconciliation(reunited_peers: List[str]):
    """
    Fase 3: Dispara el proceso de reconciliación del registry cuando se detecta reunificación.
    
    Args:
        reunited_peers: Lista de peer IDs que han vuelto a estar disponibles
    """
    if not reunited_peers:
        return
    
    print(f"[REGISTRY] FASE 3: Iniciando reconciliación con peers reunificados: {reunited_peers}")
    print(f"[REGISTRY] FASE 3: Term actual: {cluster_state['term']}")
    
    # Fase 4: Implementar reconciliación completa
    perform_registry_reconciliation(reunited_peers)


def connectivity_monitor_loop():
    """
    Fase 3: Loop que monitorea la conectividad con peers y detecta reunificación.
    Se ejecuta periódicamente para verificar si peers que estaban desconectados vuelven a estar disponibles.
    """
    # Esperar un poco al inicio para que el sistema se estabilice
    time.sleep(10)
    
    while True:
        try:
            # Verificar conectividad cada 10 segundos
            time.sleep(10)
            
            # Solo monitorear si hay peers configurados
            with cluster_lock:
                peers = cluster_state["peers"].copy()
            
            if not peers:
                continue
            
            # Detectar reunificación
            reunited_peers = detect_network_reunification()
            
            # Si se detectó reunificación, disparar reconciliación
            if reunited_peers:
                trigger_registry_reconciliation(reunited_peers)
                
        except Exception as e:
            print(f"[REGISTRY] FASE 3: Error en monitoreo de conectividad: {e}")
            import traceback
            traceback.print_exc()


def replicate_to_peers(servers_data: Dict, term: int):
    """
    Replica el estado a los peers del cluster.
    
    Maneja correctamente:
    - Cualquier número de nodos en el cluster
    - Tolerancia a fallos: continúa funcionando aunque algunos nodos estén desconectados
    - Quorum dinámico basado en nodos disponibles
    - Modo degradado: si solo queda 1 nodo, continúa operando
    
    Returns:
        True si la replicación fue exitosa (o si el sistema puede continuar en modo degradado)
    """
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        leader_id = cluster_state["node_id"]
    
    # Si no hay peers configurados, no hay nada que replicar
    # Esto es normal cuando hay solo 1 nodo en el cluster
    if not peers:
        return True
    
    # Obtener token de servicio para autenticación
    try:
        from security.service_auth import generate_service_token
        service_token = generate_service_token(leader_id, "service")
    except Exception:
        service_token = os.getenv("REGISTRY_SERVICE_TOKEN", "registry-service-token")
    
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
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=3
            )
            if response.status_code == 200:
                success_count += 1
                # Fase 3: Actualizar conectividad
                update_peer_connectivity(peer, True)
            else:
                # Fase 3: Marcar como desconectado si no responde correctamente
                update_peer_connectivity(peer, False)
        except Exception as e:
            # Fase 3: Marcar como desconectado si hay excepción
            update_peer_connectivity(peer, False)
            print(f"[REGISTRY] Error replicando a {peer}: {e}")
    
    # Calcular quorum basado en nodos configurados
    # Fórmula: (total_nodes // 2) + 1
    # Ejemplos: 3 nodos -> quorum=2, 5 nodos -> quorum=3, 7 nodos -> quorum=4
    total_nodes = len(peers) + 1  # +1 por este nodo
    quorum = (total_nodes // 2) + 1
    
    # Nodos que recibieron replicación exitosamente (incluyendo este nodo)
    nodes_with_replica = success_count + 1  # +1 por este nodo
    
    # CASO 1: Si solo queda este nodo disponible (tolerancia a fallos)
    # Permite que el sistema continúe funcionando aunque otros nodos estén caídos
    if success_count == 0 and total_nodes > 1:
        print(f"[REGISTRY] ⚠️ Solo este nodo está disponible (0/{len(peers)} peers respondieron)")
        print(f"[REGISTRY] Continuando operación en modo degradado (tolerancia a fallos activa)")
        return True
    
    # CASO 2: Calcular quorum basado en nodos disponibles
    # Si hay nodos disponibles, usar quorum de nodos disponibles
    available_nodes = nodes_with_replica
    if available_nodes > 0:
        available_quorum = (available_nodes // 2) + 1
        
        # Si tenemos quorum de nodos disponibles, la replicación es exitosa
        if nodes_with_replica >= available_quorum:
            if nodes_with_replica < quorum:
                print(f"[REGISTRY] ✓ Replicación exitosa a {nodes_with_replica}/{total_nodes} nodos (quorum disponible: {available_quorum}, quorum total: {quorum})")
            else:
                print(f"[REGISTRY] ✓ Replicación exitosa a {nodes_with_replica}/{total_nodes} nodos (quorum: {quorum})")
            return True
    
    # CASO 3: Si tenemos quorum del total configurado (caso ideal)
    if nodes_with_replica >= quorum:
        print(f"[REGISTRY] ✓ Replicación exitosa a {nodes_with_replica}/{total_nodes} nodos (quorum: {quorum})")
        return True
    
    # Si no se alcanzó ningún quorum, mostrar advertencia pero continuar
    print(f"[REGISTRY] WARNING: Solo se replicó a {nodes_with_replica}/{total_nodes} nodos (quorum: {quorum})")
    print(f"[REGISTRY] Continuando operación en modo degradado")
    return True  # Retornar True para permitir que el sistema continúe funcionando


def request_vote(candidate_id: str, term: int) -> bool:
    """
    Solicita votos para elección de líder.
    
    Maneja correctamente:
    - Cualquier número de nodos en el cluster (no limitado a cantidad fija)
    - Casos donde algunos peers están desconectados
    - Tolerancia a fallos: si solo queda 1 nodo disponible, ese nodo se convierte en líder
    - Quorum dinámico basado en nodos disponibles
    
    Ejemplos:
    - 3 nodos configurados, 2 caen: el nodo restante se convierte en líder
    - 5 nodos configurados, 3 caen: los 2 restantes pueden elegir un líder
    - 1 nodo configurado: ese nodo es automáticamente líder
    """
    with cluster_lock:
        peers = cluster_state["peers"].copy()
    
    # Si no hay peers, este nodo es el único y es líder
    if not peers:
        print(f"[REGISTRY] [ELECCIÓN] Sin peers configurados, {candidate_id} es automáticamente líder")
        return True
    
    total_nodes = len(peers) + 1  # +1 por este nodo
    quorum = (total_nodes // 2) + 1  # Mayoría simple
    
    print(f"[REGISTRY] [ELECCIÓN] Solicitando votos para {candidate_id} (término {term})")
    print(f"[REGISTRY] [ELECCIÓN] Total nodos: {total_nodes}, Quorum necesario: {quorum}")
    
    # Obtener token de servicio para autenticación
    try:
        from security.service_auth import generate_service_token
        service_token = generate_service_token(candidate_id, "service")
    except Exception:
        service_token = os.getenv("REGISTRY_SERVICE_TOKEN", "registry-service-token")
    
    votes = 1  # Voto propio
    successful_contacts = 1  # Contamos este nodo
    contacted_peers = []
    unreachable_peers = []
    
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/vote",
                json={"candidate_id": candidate_id, "term": term},
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=2
            )
            if response.status_code == 200:
                successful_contacts += 1
                contacted_peers.append(peer)
                # Fase 3: Actualizar conectividad
                update_peer_connectivity(peer, True)
                data = response.json()
                if data.get("granted"):
                    votes += 1
                    print(f"[REGISTRY] [ELECCIÓN] ✓ Voto obtenido de {peer}")
                else:
                    print(f"[REGISTRY] [ELECCIÓN] ✗ Voto denegado por {peer} (término: {data.get('term')})")
            else:
                # Fase 3: Marcar como desconectado si no responde correctamente
                update_peer_connectivity(peer, False)
                unreachable_peers.append(peer)
                print(f"[REGISTRY] [ELECCIÓN] ✗ {peer} respondió con error (status {response.status_code})")
        except Exception as e:
            # Fase 3: Marcar como desconectado si hay excepción
            update_peer_connectivity(peer, False)
            unreachable_peers.append(peer)
            print(f"[REGISTRY] [ELECCIÓN] ✗ No se pudo contactar a {peer}: {e}")
    
    # Resumen de la elección
    print(f"[REGISTRY] [ELECCIÓN] Resumen: Votos={votes}/{quorum}, Contactados={successful_contacts}/{total_nodes}")
    if contacted_peers:
        print(f"[REGISTRY] [ELECCIÓN] Peers contactados: {contacted_peers}")
    if unreachable_peers:
        print(f"[REGISTRY] [ELECCIÓN] Peers inaccesibles: {unreachable_peers}")
    
    # CASO 1: Si solo queda este nodo disponible (tolerancia a fallos)
    # Permite que un nodo siga funcionando aunque otros estén caídos
    if successful_contacts == 1:
        print(f"[REGISTRY] [ELECCIÓN] ⚠️ Solo este nodo está disponible ({len(unreachable_peers)} nodos inaccesibles de {total_nodes} totales)")
        print(f"[REGISTRY] [ELECCIÓN] Este nodo se convierte en líder para mantener el servicio activo")
        return True
    
    # CASO 2: Calcular quorum basado en nodos disponibles (no configurados)
    # Esto permite que el sistema funcione con cualquier número de nodos disponibles
    available_nodes = successful_contacts  # Nodos que respondieron (incluyendo este)
    available_quorum = (available_nodes // 2) + 1  # Quorum de nodos disponibles
    
    print(f"[REGISTRY] [ELECCIÓN] Nodos disponibles: {available_nodes}/{total_nodes}, Quorum disponible: {available_quorum}")
    
    # Si tenemos mayoría de votos de los nodos disponibles
    if votes >= available_quorum:
        print(f"[REGISTRY] [ELECCIÓN] ✓ Mayoría obtenida de nodos disponibles ({votes}/{available_quorum} votos de {available_nodes} nodos disponibles)")
        return True
    
    # CASO 3: Si tenemos mayoría del quorum total configurado (caso ideal)
    if votes >= quorum and successful_contacts >= quorum:
        print(f"[REGISTRY] [ELECCIÓN] ✓ Mayoría obtenida del quorum total ({votes}/{quorum} votos, {successful_contacts}/{total_nodes} nodos contactados)")
        return True
    
    print(f"[REGISTRY] [ELECCIÓN] ✗ No se obtuvo mayoría: {votes} votos (quorum disponible: {available_quorum}, quorum total: {quorum})")
    print(f"[REGISTRY] [ELECCIÓN] Nodos disponibles: {available_nodes}/{total_nodes}")
    return False


def start_election():
    """Inicia una elección de líder"""
    current_time = time.time()
    
    with cluster_lock:
        # Evitar elecciones muy frecuentes (cooldown de 5 segundos, reducido para detectar fallos más rápido)
        time_since_last_election = current_time - cluster_state.get("last_election_time", 0)
        if time_since_last_election < 5:
            print(f"[REGISTRY] Elección reciente hace {time_since_last_election:.1f}s, esperando cooldown...")
            return False
        
        cluster_state["term"] += 1
        cluster_state["last_election_time"] = current_time
        candidate_id = cluster_state["node_id"]
        term = cluster_state["term"]
        peers = cluster_state["peers"].copy()
        cluster_state["is_leader"] = False
        cluster_state["leader_id"] = None
    
    # Si no hay peers configurados, este nodo es automáticamente el líder
    # Esto es normal cuando hay solo 1 nodo en el cluster
    if not peers:
        with cluster_lock:
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[REGISTRY] Nodo único en el cluster, automáticamente líder (término {term})")
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
    """
    Verifica si el líder sigue activo (para seguidores).
    Detecta inactividad del líder e inicia elección entre nodos disponibles.
    """
    # Verificar frecuentemente para detectar fallos rápidamente
    CHECK_INTERVAL = 3  # Verificar cada 3 segundos
    
    # Tiempo máximo sin heartbeat antes de considerar al líder inactivo
    MAX_HEARTBEAT_AGE = ELECTION_TIMEOUT  # 15 segundos por defecto
    
    consecutive_failures = 0  # Contador de fallos consecutivos al contactar al líder
    MAX_CONSECUTIVE_FAILURES = 2  # Después de 2 intentos fallidos, iniciar elección
    
    while True:
        time.sleep(CHECK_INTERVAL)
        
        if is_leader():
            consecutive_failures = 0  # Reset si este nodo es líder
            continue
        
        with cluster_lock:
            time_since_heartbeat = time.time() - cluster_state["last_heartbeat_time"]
            leader_id = cluster_state["leader_id"]
        
        # Si hay un líder conocido, verificar activamente si está respondiendo
        if leader_id:
            # Verificar si ha pasado demasiado tiempo sin heartbeat
            if time_since_heartbeat > MAX_HEARTBEAT_AGE:
                # Intentar contactar al líder una última vez antes de iniciar elección
                print(f"[REGISTRY] ⚠️ No se ha recibido heartbeat del líder {leader_id} en {time_since_heartbeat:.1f}s (timeout: {MAX_HEARTBEAT_AGE}s)")
                print(f"[REGISTRY] Verificando conectividad con líder {leader_id}...")
                
                try:
                    leader_url = get_peer_url(leader_id)
                    response = requests.get(f"{leader_url}/", timeout=2)
                    
                    if response.status_code == 200:
                        # El líder está vivo, actualizar heartbeat time
                        print(f"[REGISTRY] ✓ Líder {leader_id} responde correctamente, heartbeat actualizado")
                        with cluster_lock:
                            cluster_state["last_heartbeat_time"] = time.time()
                        consecutive_failures = 0
                        continue
                    else:
                        # El líder no responde correctamente
                        consecutive_failures += 1
                        print(f"[REGISTRY] ⚠️ Líder {leader_id} no responde correctamente (status {response.status_code}, fallos consecutivos: {consecutive_failures})")
                        
                except Exception as e:
                    # No se puede contactar al líder
                    consecutive_failures += 1
                    print(f"[REGISTRY] ⚠️ No se puede contactar al líder {leader_id} (fallos consecutivos: {consecutive_failures}): {e}")
                
                # Si hemos fallado múltiples veces o ha pasado mucho tiempo, iniciar elección
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES or time_since_heartbeat > (MAX_HEARTBEAT_AGE * 1.2):
                    print(f"[REGISTRY] 🚨 Líder {leader_id} considerado inactivo después de {consecutive_failures} intentos fallidos")
                    print(f"[REGISTRY] Iniciando elección entre nodos disponibles...")
                    with cluster_lock:
                        cluster_state["leader_id"] = None
                    start_election()
                    consecutive_failures = 0  # Reset después de iniciar elección
            else:
                # Aún no ha pasado el timeout, pero verificar periódicamente la conectividad
                # Esto ayuda a detectar problemas de red temprano
                if time_since_heartbeat > (MAX_HEARTBEAT_AGE * 0.6):  # Verificar cuando ha pasado 60% del timeout
                    try:
                        leader_url = get_peer_url(leader_id)
                        response = requests.get(f"{leader_url}/", timeout=2)
                        if response.status_code == 200:
                            # El líder está vivo, resetear contador de fallos
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            print(f"[REGISTRY] ⚠️ Líder {leader_id} responde con error (status {response.status_code})")
                    except Exception as e:
                        consecutive_failures += 1
                        print(f"[REGISTRY] ⚠️ Advertencia: No se puede contactar al líder {leader_id} (último heartbeat hace {time_since_heartbeat:.1f}s): {e}")
                        
                        # Si hay múltiples fallos consecutivos incluso antes del timeout, iniciar elección
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            print(f"[REGISTRY] 🚨 Múltiples fallos al contactar al líder, iniciando elección preventiva...")
                            with cluster_lock:
                                cluster_state["leader_id"] = None
                            start_election()
                            consecutive_failures = 0
        else:
            # No hay líder conocido
            if time_since_heartbeat > MAX_HEARTBEAT_AGE:
                print(f"[REGISTRY] No hay líder conocido después de {time_since_heartbeat:.1f}s, intentando elección...")
                start_election()
            consecutive_failures = 0  # Reset si no hay líder


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
    """
    Loop que reintenta elecciones si no hay líder (respaldo para follower_heartbeat_check).
    Actúa como red de seguridad para asegurar que siempre haya un líder.
    """
    time.sleep(10)  # Esperar más tiempo para que todos los nodos estén listos
    
    while True:
        # Verificar periódicamente como respaldo (cada ELECTION_TIMEOUT)
        time.sleep(ELECTION_TIMEOUT)
        
        with cluster_lock:
            is_leader = cluster_state["is_leader"]
            leader_id = cluster_state["leader_id"]
            peers = cluster_state["peers"].copy()
            last_heartbeat = cluster_state["last_heartbeat_time"]
        
        # Solo verificar si no somos líder
        if is_leader:
            continue
        
        current_time = time.time()
        time_since_heartbeat = current_time - last_heartbeat
        
        # Si no hay líder conocido y ha pasado el timeout, intentar elección
        if not leader_id:
            if time_since_heartbeat > ELECTION_TIMEOUT:
                print(f"[REGISTRY] [RESPALDO] No hay líder detectado después de {time_since_heartbeat:.1f}s, intentando elección...")
                start_election()
        elif leader_id:
            # Hay un líder conocido, verificar que sigue activo como respaldo
            # El follower_heartbeat_check ya maneja esto, pero este loop actúa como red de seguridad
            if time_since_heartbeat > (ELECTION_TIMEOUT * 1.5):  # Más tolerante que el check principal
                print(f"[REGISTRY] [RESPALDO] Verificando estado del líder {leader_id} (último heartbeat hace {time_since_heartbeat:.1f}s)...")
                try:
                    leader_url = get_peer_url(leader_id)
                    response = requests.get(f"{leader_url}/", timeout=3)
                    if response.status_code == 200:
                        # El líder está vivo, actualizar heartbeat time
                        print(f"[REGISTRY] [RESPALDO] Líder {leader_id} responde correctamente")
                        with cluster_lock:
                            cluster_state["last_heartbeat_time"] = current_time
                    else:
                        # El líder no responde correctamente - iniciar elección
                        print(f"[REGISTRY] [RESPALDO] ⚠️ Líder {leader_id} no responde correctamente (status {response.status_code}), iniciando elección...")
                        with cluster_lock:
                            cluster_state["leader_id"] = None
                        start_election()
                except Exception as e:
                    # No se puede contactar al líder - iniciar elección
                    print(f"[REGISTRY] [RESPALDO] ⚠️ No se puede contactar al líder {leader_id} después de {time_since_heartbeat:.1f}s: {e}")
                    print(f"[REGISTRY] [RESPALDO] Iniciando elección entre nodos disponibles...")
                    with cluster_lock:
                        cluster_state["leader_id"] = None
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
    
    # Fase 3: Iniciar monitoreo de conectividad para detectar reunificación
    connectivity_thread = threading.Thread(target=connectivity_monitor_loop, daemon=True)
    connectivity_thread.start()
    print(f"[REGISTRY] FASE 3: Monitoreo de conectividad iniciado")
    
    # Intentar elección inicial después de un delay para que todos los nodos estén listos
    time.sleep(5)
    start_election()
    
    yield
    
    # Shutdown
    print(f"[REGISTRY] Nodo deteniéndose...")


app = FastAPI(title="TBFS Registry Service (Distributed)", lifespan=lifespan)

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
def register_server(
    registration: ServerRegistration,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Registra un nuevo servidor de datos (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    if not validate_service_request(registration.server_id, token):
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
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
def receive_heartbeat(
    heartbeat: Heartbeat,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Recibe heartbeat de un servidor (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    if not validate_service_request(heartbeat.server_id, token):
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
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
def internal_replicate(
    data: ReplicationData,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint interno para recibir replicación del líder (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro registry
    service_id = payload.get("service_id") or payload.get("sub", "")
    if not service_id.startswith("registry-"):
        raise HTTPException(status_code=403, detail="Solo registries pueden replicar")
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
            elif data.term == current_term and data.leader_id == current_leader:
                # Mismo término y mismo líder: actualizar heartbeat (replicación periódica del líder)
                cluster_state["last_heartbeat_time"] = time.time()
                # No imprimir para evitar spam en logs, pero actualizar el tiempo
        
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
def internal_vote(
    request: VoteRequest,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint interno para votar en elecciones (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro registry
    service_id = payload.get("service_id") or payload.get("sub", "")
    if not service_id.startswith("registry-"):
        raise HTTPException(status_code=403, detail="Solo registries pueden votar")
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


@app.get("/internal/servers")
def get_servers_endpoint(
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Fase 4: Endpoint interno para obtener el estado de servidores.
    Usado para reconciliación después de particionamiento.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro registry
    service_id = payload.get("service_id") or payload.get("sub", "")
    if not service_id.startswith("registry-"):
        raise HTTPException(status_code=403, detail="Solo registries pueden obtener servidores")
    
    # Obtener servidores
    with servers_lock:
        servers_copy = servers.copy()
    
    return {
        "node_id": cluster_state["node_id"],
        "term": cluster_state["term"],
        "servers": servers_copy,
        "total": len(servers_copy)
    }
