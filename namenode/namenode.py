"""
MetaNameNode - Servicio distribuido con 3 réplicas
Mantiene metadatos de archivos (nombres y etiquetas) con replicación Raft-like
Los archivos físicos se almacenan en DataNodes, no aquí.
"""
from fastapi import FastAPI, HTTPException, Query, UploadFile, Form, Depends, Header
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional, Dict
import time
import threading
from datetime import datetime, timedelta
import os
import requests
from contextlib import asynccontextmanager
import json
import sqlite3
import hashlib
import sys
import socket
import uuid
from pathlib import Path

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

# Importar modelos primero (necesarios para los tipos)
from security.models import (
    User,
    Role,
    Permission,
    UserLogin,
    UserCreate,
    TokenResponse,
    PasswordChange,
    UserSignup,
)

from security.auth import (
    create_access_token,
    verify_token,
    get_current_user as _get_current_user_base,
    get_current_service,
    require_role as _require_role_base,
    require_permission as _require_permission_base,
    authenticate_user,
    create_user,
    get_user,
    change_password
)
from security.service_auth import (
    verify_service_token,
    validate_service_request,
    generate_service_token,
    generate_client_upload_token
)
from security.rate_limit import RateLimitMiddleware

# NODE_ID se define más abajo, así que usaremos una función que lo obtiene dinámicamente
def _get_node_id() -> str:
    """Obtiene el NODE_ID del namenode"""
    return os.getenv("NODE_ID", "namenode-1")

# Wrapper para get_current_user que usa el node_id del namenode
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer())
) -> User:
    """Wrapper que obtiene el usuario actual usando el node_id del namenode"""
    return await _get_current_user_base(credentials, node_id=_get_node_id())

# Wrapper para require_role que usa el node_id del namenode
def require_role(allowed_roles: List[Role]):
    """Wrapper para require_role que usa el node_id del namenode"""
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Se requiere uno de los roles: {[r.value for r in allowed_roles]}"
            )
        return current_user
    return role_checker

# Wrapper para require_permission que usa el node_id del namenode
def require_permission(permission: Permission):
    """Wrapper para require_permission que usa el node_id del namenode"""
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Se requiere el permiso: {permission.value}"
            )
        return current_user
    return permission_checker

# Dependency especial para eliminar archivos: permite a usuarios eliminar sus propios archivos
def require_delete_permission():
    """
    Dependency que permite eliminar archivos si:
    - El usuario tiene el permiso DELETE_FILES (ADMIN o SERVICE), O
    - El usuario es USER (puede eliminar sus propios archivos, que se filtran por user_id)
    """
    async def delete_checker(current_user: User = Depends(get_current_user)) -> User:
        # Si tiene el permiso DELETE_FILES, permitir
        if current_user.has_permission(Permission.DELETE_FILES):
            return current_user
        # Si es USER, también permitir (solo podrá eliminar sus propios archivos)
        if current_user.role == Role.USER:
            return current_user
        # Si no cumple ninguna condición, denegar
        raise HTTPException(
            status_code=403,
            detail="No tienes permiso para eliminar archivos"
        )
    return delete_checker

from namenode.database import init_db, get_db_path, get_connection, close_connection, db_lock
from namenode.manager import (
    add_file_metadata, query_files, get_file_by_id, delete_file_metadata,
    delete_files_by_tags, delete_files_by_exact_tags, add_tags_to_files, delete_tags_from_files
)
from namenode.datanode_manager import (
    register_datanode, update_datanode_heartbeat, get_datanode, list_datanodes,
    assign_replicas, save_file_replicas, get_file_replicas, detect_inactive_datanodes,
    get_best_datanode_for_read, get_all_replicas_for_read, delete_file_from_datanodes,
    get_files_affected_by_datanode, rereplicate_file, mark_datanode_draining,
    unmark_datanode_draining, drain_datanode, discover_file_replicas
)
from namenode.datanode_transfer import send_chunks_from_datanode

app = FastAPI(title="TBFS MetaNameNode (Distributed)")

# Configurar CORS para permitir peticiones desde el frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción, especificar dominios específicos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurar rate limiting (100 peticiones por minuto por IP/usuario)
app.add_middleware(RateLimitMiddleware, max_requests=100, time_window=60)

# Estado del cluster
cluster_state = {
    "node_id": os.getenv("NODE_ID", "namenode-1"),
    "is_leader": False,
    "leader_id": None,
    "term": 0,
    "last_heartbeat_time": 0,
    "last_election_time": 0,
    "peers": [],  # Lista de peers conocidos (descubiertos mediante gossip)
    "peer_status": {},  # {peer_id: {"last_seen": float, "status": "alive"/"suspected"/"dead"}}
    "version": 0,  # Versión del estado local (incrementa en cada cambio de peers)
    "reconciliation_in_progress": False,  # Flag para evitar reconciliaciones simultáneas
    "active_followers": []  # Lista de seguidores activos según el líder
}
cluster_lock = threading.Lock()

# Configuración
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
LEADER_HEARTBEAT_INTERVAL = int(os.getenv("LEADER_HEARTBEAT_INTERVAL", "5"))
ELECTION_TIMEOUT = int(os.getenv("ELECTION_TIMEOUT", "15"))
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
# Configuración de Gossip
GOSSIP_INTERVAL = int(os.getenv("GOSSIP_INTERVAL", "5"))  # Intervalo entre rondas de gossip (segundos)
GOSSIP_FANOUT = int(os.getenv("GOSSIP_FANOUT", "2"))  # Número de peers a contactar en cada ronda
NAMENODE_SERVICE = os.getenv("NAMENODE_SERVICE", "namenode")  # Alias DNS de Docker para descubrimiento

# Los peers se descubren automáticamente via DNS de Docker
print(f"[NAMENODE] 📋 Descubrimiento de peers via DNS de Docker (servicio: {NAMENODE_SERVICE})")

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


class GossipExchange(BaseModel):
    """Intercambio de estado en protocolo Gossip entre namenodes"""
    sender_id: str
    sender_version: int
    known_peers: List[str]  # Lista de peers conocidos por el nodo remoto
    is_leader: bool  # Si el sender es el líder
    leader_id: Optional[str]  # ID del líder conocido por el sender
    timestamp: float


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

# Almacenamiento de uploads en progreso (upload_id -> info)
active_uploads = {}
uploads_lock = threading.Lock()


def save_operation_to_log(operation: OperationLog, node_id: str = None):
    """
    Guarda una operación en el log persistente (base de datos de operaciones).
    Fase 2: Log persistente para reconciliación después de particionamiento.
    """
    import json
    from namenode.database import get_operations_db_path, get_connection, close_connection, operations_db_lock
    
    if node_id is None:
        node_id = NODE_ID
    
    db_path = get_operations_db_path(node_id)
    
    with operations_db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            cursor.execute("""
                INSERT INTO operation_log (operation, data, term, timestamp, node_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                operation.operation,
                json.dumps(operation.data),
                operation.term,
                operation.timestamp,
                node_id
            ))
            conn.commit()
        except Exception as e:
            print(f"[ERROR] Error guardando operación en log persistente: {e}")
            conn.rollback()
        finally:
            close_connection(conn)


def load_operation_log(node_id: str = None) -> List[OperationLog]:
    """
    Carga el log de operaciones desde la base de datos de operaciones.
    Fase 2: Cargar log al iniciar para reconstruir estado.
    """
    import json
    from namenode.database import get_operations_db_path, get_connection, close_connection, operations_db_lock
    
    if node_id is None:
        node_id = NODE_ID
    
    db_path = get_operations_db_path(node_id)
    operations = []
    
    with operations_db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            cursor.execute("""
                SELECT operation, data, term, timestamp, node_id
                FROM operation_log
                ORDER BY term, timestamp
            """)
            rows = cursor.fetchall()
            
            for row in rows:
                try:
                    operation_data = json.loads(row[1])  # data es JSON string
                    operation = OperationLog(
                        operation=row[0],
                        data=operation_data,
                        term=row[2],
                        timestamp=row[3]
                    )
                    operations.append(operation)
                except Exception as e:
                    print(f"[WARNING] Error cargando operación del log: {e}")
                    continue
        except Exception as e:
            print(f"[ERROR] Error cargando log de operaciones: {e}")
        finally:
            close_connection(conn)
    
    return operations


def get_peer_url(peer: str) -> str:
    """Obtiene la URL completa de un peer"""
    if not peer.startswith("http"):
        # Si el peer ya tiene el prefijo "tbfs-", usarlo directamente
        # Si el peer es "namenode-1" o "tbfs-namenode-1", construir "tbfs-namenode-1"
        if not peer.startswith("tbfs-"):
            if peer.startswith("namenode-"):
                peer = f"tbfs-{peer}"
            # Si no empieza con "namenode-" ni "tbfs-", asumir que necesita el prefijo
            elif "namenode" in peer.lower():
                # Si contiene "namenode" pero no tiene prefijo, agregarlo
                if not peer.startswith("tbfs-"):
                    peer = f"tbfs-{peer}"
        return f"http://{peer}:{NAMENODE_PORT}"
    return peer


def discover_peers_dns() -> List[str]:
    """
    Descubre otros namenodes usando el DNS de Docker.
    Docker DNS devuelve todas las IPs de los contenedores con el alias 'namenode'.
    
    Returns:
        Lista de nombres/IPs de peers descubiertos (excluyendo este nodo)
    """
    discovered_peers = []
    
    try:
        # Resolver DNS para obtener todas las IPs de los namenodes
        addr_info = socket.getaddrinfo(
            NAMENODE_SERVICE, 
            NAMENODE_PORT,
            proto=socket.IPPROTO_TCP
        )
        
        # Obtener IP local para excluirla
        local_ip = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        
        # Extraer IPs únicas
        for info in addr_info:
            ip = info[4][0]  # info[4] es (ip, port)
            # Excluir la IP local
            if ip != local_ip and ip not in discovered_peers:
                discovered_peers.append(ip)
        
        if discovered_peers:
            print(f"[NAMENODE] 🔍 DNS descubrió {len(discovered_peers)} peers: {discovered_peers}")
        
    except socket.gaierror as e:
        print(f"[NAMENODE] ⚠️  Error DNS resolviendo '{NAMENODE_SERVICE}': {e}")
    except Exception as e:
        print(f"[NAMENODE] ⚠️  Error inesperado en descubrimiento DNS: {e}")
    
    return discovered_peers


def refresh_peers_from_dns():
    """
    Actualiza la lista de peers usando DNS de Docker.
    Combina peers existentes con nuevos descubiertos.
    """
    global cluster_state
    
    dns_peers = discover_peers_dns()
    
    if not dns_peers:
        return
    
    with cluster_lock:
        current_node_id = cluster_state["node_id"]
        current_peers = set(cluster_state["peers"])
        
        # Agregar nuevos peers descubiertos
        for peer_ip in dns_peers:
            # No agregarnos a nosotros mismos
            if peer_ip != current_node_id and peer_ip not in current_peers:
                cluster_state["peers"].append(peer_ip)
                # Inicializar estado del peer
                if peer_ip not in cluster_state["peer_status"]:
                    cluster_state["peer_status"][peer_ip] = {
                        "last_seen": 0.0,
                        "status": "unknown"
                    }
                print(f"[NAMENODE] 🆕 Nuevo peer descubierto via DNS: {peer_ip}")
        
        # Limpiar duplicados
        cluster_state["peers"] = list(set(cluster_state["peers"]))
        # Asegurar que no estamos en nuestra propia lista
        cluster_state["peers"] = [p for p in cluster_state["peers"] if p != current_node_id]


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


def update_peer_status(peer_id: str, alive: bool):
    """
    Actualiza el estado de un peer basado en si está vivo o no (para gossip).
    Detecta reunificación cuando un peer pasa de "dead"/"suspected" a "alive".
    
    Args:
        peer_id: ID del peer
        alive: True si el peer está vivo, False si no
    """
    reunited_peers = []
    
    with cluster_lock:
        if peer_id not in cluster_state["peer_status"]:
            cluster_state["peer_status"][peer_id] = {
                "last_seen": 0.0,
                "status": "unknown"
            }
        
        peer_info = cluster_state["peer_status"][peer_id]
        previous_status = peer_info["status"]
        
        if alive:
            peer_info["last_seen"] = time.time()
            # Detectar reunificación: si el peer estaba "dead" o "suspected" y ahora está "alive"
            if previous_status in ["dead", "suspected"]:
                print(f"[NAMENODE] [GOSSIP] 🔄 Peer {peer_id} reunificado: {previous_status} -> alive")
                reunited_peers.append(peer_id)
            peer_info["status"] = "alive"
        else:
            # Si ha pasado mucho tiempo sin ver al peer, marcarlo como suspected o dead
            time_since_seen = time.time() - peer_info["last_seen"]
            PEER_FAILURE_TIMEOUT = 30  # 30 segundos
            if time_since_seen > PEER_FAILURE_TIMEOUT:
                if peer_info["status"] == "alive":
                    peer_info["status"] = "suspected"
                    print(f"[NAMENODE] [GOSSIP] Peer {peer_id} marcado como suspected (sin contacto por {time_since_seen:.1f}s)")
                elif peer_info["status"] == "suspected" and time_since_seen > (PEER_FAILURE_TIMEOUT * 2):
                    peer_info["status"] = "dead"
                    print(f"[NAMENODE] [GOSSIP] Peer {peer_id} marcado como dead (sin contacto por {time_since_seen:.1f}s)")
    
    # Disparar reconciliación si se detectó reunificación
    if reunited_peers:
        print(f"[NAMENODE] [GOSSIP] 🔄 ========== DETECCIÓN DE REUNIFICACIÓN ==========")
        print(f"[NAMENODE] [GOSSIP] 🔄 Peers reunificados detectados: {reunited_peers}")
        print(f"[NAMENODE] [GOSSIP] 🔄 Timestamp: {datetime.now().isoformat()}")
        print(f"[NAMENODE] [GOSSIP] 🔄 Disparando reconciliación en hilo separado...")
        # Ejecutar en un hilo separado para no bloquear gossip
        threading.Thread(target=trigger_reconciliation, args=(reunited_peers,), daemon=True).start()


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
        
        return list(set(peers_to_contact))  # Eliminar duplicados


def gossip_exchange(peer_id: str) -> bool:
    """
    Realiza un intercambio de estado Gossip con un peer namenode.
    
    Args:
        peer_id: ID del peer con el que hacer intercambio
    
    Returns:
        True si el intercambio fue exitoso, False en caso contrario
    """
    try:
        peer_url = get_peer_url(peer_id)
        
        # Obtener token de servicio para autenticación
        try:
            service_token = generate_service_token(cluster_state["node_id"], "service")
        except Exception as e:
            print(f"[NAMENODE] [GOSSIP] Error generando token, usando token de entorno: {e}")
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Obtener estado local
        with cluster_lock:
            local_version = cluster_state["version"]
            sender_id = cluster_state["node_id"]
            is_leader = cluster_state["is_leader"]
            leader_id = cluster_state["leader_id"]
            # Obtener lista de peers conocidos (incluyendo nosotros mismos y el líder si existe)
            known_peers_list = cluster_state["peers"].copy()
            known_peers_list.append(sender_id)  # Incluir este nodo
            # Incluir el líder si existe y es diferente del sender_id
            if leader_id and leader_id != sender_id:
                if leader_id not in known_peers_list:
                    known_peers_list.append(leader_id)
            known_peers = list(set(known_peers_list))
        
        print(f"[NAMENODE] [GOSSIP] 📤 Enviando intercambio a {peer_id}")
        print(f"[NAMENODE] [GOSSIP] 📋 Peers conocidos que se envían ({len(known_peers)}): {sorted(known_peers)}")
        print(f"[NAMENODE] [GOSSIP] 📋 Detalle: peers={sorted(cluster_state['peers'])}, node_id={sender_id}, leader_id={leader_id}, is_leader={is_leader}")
        
        # Enviar nuestro estado al peer
        response = requests.post(
            f"{peer_url}/internal/gossip",
            json={
                "sender_id": sender_id,
                "sender_version": local_version,
                "known_peers": known_peers,
                "is_leader": is_leader,
                "leader_id": leader_id,
                "timestamp": time.time()
            },
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=3
        )
        
        if response.status_code == 200:
            # Recibir estado del peer
            data = response.json()
            remote_version = data.get("version", 0)
            remote_known_peers = data.get("known_peers", [])
            remote_is_leader = data.get("is_leader", False)
            remote_leader_id = data.get("leader_id")
            
            print(f"[NAMENODE] [GOSSIP] 📥 Respuesta recibida de {peer_id}")
            print(f"[NAMENODE] [GOSSIP] 📋 Peers conocidos recibidos ({len(remote_known_peers)}): {sorted(remote_known_peers)}")
            print(f"[NAMENODE] [GOSSIP] 📋 Detalle remoto: is_leader={remote_is_leader}, leader_id={remote_leader_id}, version={remote_version}")
            
            # Actualizar versión del cluster si la remota es mayor
            peers_changed = False
            with cluster_lock:
                peers_before = cluster_state["peers"].copy()
                # Actualizar versión al máximo entre local y remota
                max_version = max(local_version, remote_version)
                if max_version > cluster_state["version"]:
                    old_version = cluster_state["version"]
                    cluster_state["version"] = max_version
                    print(f"[NAMENODE] [GOSSIP] Versión actualizada de {old_version} a {max_version} (de {peer_id})")
                
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
                            print(f"[NAMENODE] [GOSSIP] Nuevo peer descubierto: {remote_peer}")
                        # Inicializar estado del nuevo peer si no existe
                        if remote_peer not in cluster_state["peer_status"]:
                            cluster_state["peer_status"][remote_peer] = {
                                "last_seen": 0.0,
                                "status": "unknown"
                            }
                        peers_changed = True
                
                # Asegurar que no hay duplicados ni el node_id después de agregar
                cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
                cluster_state["peers"] = list(set(cluster_state["peers"]))
                
                # Actualizar información del líder si el peer remoto es líder o conoce un líder
                # IMPORTANTE: No cambiar is_leader si este nodo ya es el líder
                if remote_is_leader and remote_leader_id == peer_id:
                    # El peer remoto dice ser el líder
                    if cluster_state["is_leader"]:
                        # Este nodo es el líder, ignorar la información del peer
                        print(f"[NAMENODE] [GOSSIP] ⚠️ Ignorando info de líder de {peer_id} - este nodo es el líder")
                    elif cluster_state["leader_id"] != peer_id:
                        # No somos líder, actualizar leader_id
                        print(f"[NAMENODE] [GOSSIP] Líder actualizado desde gossip: {peer_id}")
                        cluster_state["leader_id"] = peer_id
                elif remote_leader_id and remote_leader_id != cluster_state["leader_id"]:
                    # El peer remoto conoce un líder diferente
                    if not cluster_state["is_leader"]:
                        print(f"[NAMENODE] [GOSSIP] Líder conocido actualizado desde gossip: {remote_leader_id}")
                        cluster_state["leader_id"] = remote_leader_id
            
            # Actualizar estado del peer como vivo
            update_peer_status(peer_id, True)
            
            with cluster_lock:
                peers_after = cluster_state["peers"].copy()
                current_leader_id = cluster_state["leader_id"]
                # Construir lista completa de peers conocidos (incluyendo líder)
                all_known_peers_complete = peers_after.copy()
                if current_leader_id and current_leader_id not in all_known_peers_complete:
                    all_known_peers_complete.append(current_leader_id)
            
            print(f"[NAMENODE] [GOSSIP] ✅ Intercambio exitoso con {peer_id}")
            print(f"[NAMENODE] [GOSSIP] 📊 Resumen: Peers antes={len(peers_before)}, Peers después={len(peers_after)}, Cambios={peers_changed}")
            print(f"[NAMENODE] [GOSSIP] 📋 Lista completa de peers conocidos ({len(all_known_peers_complete)}): {sorted(all_known_peers_complete)}")
            print(f"[NAMENODE] [GOSSIP] 📋 Líder conocido: {current_leader_id}")
            return True
        else:
            update_peer_status(peer_id, False)
            print(f"[NAMENODE] [GOSSIP] Error en intercambio con {peer_id}: HTTP {response.status_code}")
        return False

    except requests.exceptions.ConnectionError as e:
        # Errores de conexión (DNS, red, etc.) - esperados cuando el peer no está disponible
        update_peer_status(peer_id, False)
        error_msg = str(e)
        if "Failed to resolve" in error_msg or "name resolution" in error_msg.lower():
            print(f"[NAMENODE] [GOSSIP] ⚠️  Peer {peer_id} no disponible (no se puede resolver DNS)")
        elif "Connection refused" in error_msg or "refused" in error_msg.lower():
            print(f"[NAMENODE] [GOSSIP] ⚠️  Peer {peer_id} no disponible (conexión rechazada)")
        else:
            print(f"[NAMENODE] [GOSSIP] ⚠️  Peer {peer_id} no disponible: {type(e).__name__}")
        return False
    except requests.exceptions.Timeout:
        update_peer_status(peer_id, False)
        print(f"[NAMENODE] [GOSSIP] ⚠️  Peer {peer_id} no responde (timeout)")
        return False
    except Exception as e:
        update_peer_status(peer_id, False)
        print(f"[NAMENODE] [GOSSIP] ⚠️  Error en intercambio con {peer_id}: {type(e).__name__}: {e}")
        return False


def gossip_loop():
    """
    Loop principal de Gossip que periódicamente selecciona peers aleatorios y hace intercambio.
    Usa DNS de Docker para descubrir peers además de la lista configurada.
    """
    import random
    
    # Esperar un poco al inicio para que todos los nodos estén listos
    time.sleep(5)
    
    # Descubrir peers via DNS de Docker al inicio
    print(f"[NAMENODE] [GOSSIP] Descubriendo peers via DNS de Docker...")
    refresh_peers_from_dns()
    
    # Intentar contacto inicial con todos los peers configurados
    print(f"[NAMENODE] [GOSSIP] Iniciando loop de gossip...")
    with cluster_lock:
        initial_peers = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
    
    if initial_peers:
        print(f"[NAMENODE] [GOSSIP] Intentando contacto inicial con {len(initial_peers)} peers: {initial_peers}")
        for peer in initial_peers:
            # Inicializar estado si no existe
            update_peer_status(peer, False)  # Esto lo inicializa como "unknown"
            thread = threading.Thread(target=gossip_exchange, args=(peer,), daemon=True)
            thread.start()
    
    # Contador para refrescar DNS periódicamente
    dns_refresh_counter = 0
    DNS_REFRESH_INTERVAL = 6  # Refrescar DNS cada 6 ciclos de gossip (~30 segundos)
    
    while True:
        try:
            time.sleep(GOSSIP_INTERVAL)
            
            # Refrescar peers via DNS periódicamente
            dns_refresh_counter += 1
            if dns_refresh_counter >= DNS_REFRESH_INTERVAL:
                dns_refresh_counter = 0
                refresh_peers_from_dns()
            
            # Obtener lista de peers a contactar (incluye "alive" y "unknown" para bootstrap)
            peers_to_contact = get_peers_to_contact()
            
            if not peers_to_contact:
                # Si no hay peers, intentar descubrir via DNS
                refresh_peers_from_dns()
                # Verificar si hay peers configurados que no están en peer_status
                with cluster_lock:
                    configured_peers = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
                    for peer in configured_peers:
                        if peer not in cluster_state["peer_status"]:
                            update_peer_status(peer, False)  # Inicializar como "unknown"
                            peers_to_contact.append(peer)
            
            if not peers_to_contact:
                print(f"[NAMENODE] [GOSSIP] No hay peers para contactar (DNS tampoco encontró peers)")
                continue
            
            # Seleccionar número aleatorio de peers (fanout)
            num_peers = min(GOSSIP_FANOUT, len(peers_to_contact))
            selected_peers = random.sample(peers_to_contact, num_peers) if len(peers_to_contact) > 0 else []
            
            print(f"[NAMENODE] [GOSSIP] Contactando {len(selected_peers)} de {len(peers_to_contact)} peers disponibles: {selected_peers}")
            
            # Hacer intercambio con cada peer seleccionado
            for peer in selected_peers:
                # No hacer gossip con nosotros mismos
                if peer == cluster_state["node_id"]:
                    continue
                
                # Ejecutar en un hilo separado para no bloquear
                thread = threading.Thread(target=gossip_exchange, args=(peer,), daemon=True)
                thread.start()
        
            # Verificar si el líder está desconectado basado en el estado de gossip
            # (solo si no somos el líder)
            if not is_leader():
                with cluster_lock:
                    leader_id = cluster_state.get("leader_id")
                    if leader_id:
                        # Verificar estado del líder en peer_status
                        if leader_id in cluster_state.get("peer_status", {}):
                            leader_status = cluster_state["peer_status"][leader_id].get("status")
                            time_since_seen = time.time() - cluster_state["peer_status"][leader_id].get("last_seen", 0)
                            
                            # Si el líder está marcado como "dead" o ha estado "suspected" por mucho tiempo
                            if leader_status == "dead":
                                print(f"[NAMENODE] [GOSSIP] 🗳️  Líder {leader_id} detectado como dead mediante gossip, iniciando elección...")
                                cluster_state["leader_id"] = None
                                cluster_state["last_heartbeat_time"] = 0
                                # Iniciar elección en un hilo separado para no bloquear gossip
                                threading.Thread(target=start_election, daemon=True).start()
                            elif leader_status == "suspected" and time_since_seen > (ELECTION_TIMEOUT * 1.5):
                                print(f"[NAMENODE] [GOSSIP] 🗳️  Líder {leader_id} suspected por {time_since_seen:.1f}s, iniciando elección...")
                                cluster_state["leader_id"] = None
                                cluster_state["last_heartbeat_time"] = 0
                                threading.Thread(target=start_election, daemon=True).start()
                
        except Exception as e:
            print(f"[NAMENODE] [GOSSIP] ⚠️  Error en gossip loop: {type(e).__name__}: {e}")
            # No imprimir traceback completo para errores esperados


def trigger_reconciliation(reunited_peers: List[str]):
    """
    Fase 3: Dispara el proceso de reconciliación cuando se detecta reunificación.
    
    Esta función será implementada en la Fase 4, por ahora solo registra el evento.
    
    Args:
        reunited_peers: Lista de peer IDs que han vuelto a estar disponibles
    """
    if not reunited_peers:
        return
    
    with cluster_lock:
        if cluster_state["reconciliation_in_progress"]:
            print(f"[NAMENODE] FASE 3: Reconciliación ya en progreso, ignorando nueva detección")
            return
        
        cluster_state["reconciliation_in_progress"] = True
    
    try:
        start_time = time.time()
        print(f"[NAMENODE] 🔄 ========== INICIANDO RECONCILIACIÓN ==========")
        print(f"[NAMENODE] 🔄 Timestamp inicio: {datetime.now().isoformat()}")
        print(f"[NAMENODE] 🔄 Nodo actual: {cluster_state['node_id']}")
        print(f"[NAMENODE] 🔄 Peers reunificados: {reunited_peers}")
        print(f"[NAMENODE] 🔄 Term actual: {cluster_state['term']}")
        print(f"[NAMENODE] 🔄 Es líder: {cluster_state['is_leader']}")
        print(f"[NAMENODE] 🔄 Líder conocido: {cluster_state.get('leader_id', 'None')}")
        
        # TODO Fase 4: Implementar reconciliación completa
        # Por ahora, solo registramos el evento y comparamos términos
        with cluster_lock:
            current_term = cluster_state["term"]
            current_node_id = cluster_state["node_id"]
        
        # Comparar términos con los peers reunificados
        print(f"[NAMENODE] 🔄 Comparando términos con {len(reunited_peers)} peers...")
        for peer in reunited_peers:
            try:
                peer_url = get_peer_url(peer)
                print(f"[NAMENODE] 🔄 Consultando peer {peer} en {peer_url}...")
                response = requests.get(f"{peer_url}/", timeout=3)
                if response.status_code == 200:
                    peer_data = response.json()
                    peer_term = peer_data.get("term", 0)
                    peer_is_leader = peer_data.get("is_leader", False)
                    peer_total_files = peer_data.get("total_files", 0)
                    peer_total_tags = peer_data.get("total_tags", 0)
                    
                    print(f"[NAMENODE] 🔄 Peer {peer}:")
                    print(f"[NAMENODE] 🔄   - Term: {peer_term}")
                    print(f"[NAMENODE] 🔄   - Es líder: {peer_is_leader}")
                    print(f"[NAMENODE] 🔄   - Total archivos: {peer_total_files}")
                    print(f"[NAMENODE] 🔄   - Total tags: {peer_total_tags}")
                    
                    # Si el peer tiene un term mayor, debería ser el líder válido
                    if peer_term > current_term:
                        print(f"[NAMENODE] 🔄 ⚠️  Peer {peer} tiene term mayor ({peer_term} > {current_term}), este nodo debería sincronizarse")
                    elif peer_term < current_term:
                        print(f"[NAMENODE] 🔄 ⚠️  Este nodo tiene term mayor ({current_term} > {peer_term}), peer {peer} debería sincronizarse")
                    else:
                        print(f"[NAMENODE] 🔄 ✅ Términos iguales ({current_term}), se requiere reconciliación detallada")
                else:
                    print(f"[NAMENODE] 🔄 ❌ Error HTTP {response.status_code} consultando peer {peer}")
            except Exception as e:
                print(f"[NAMENODE] 🔄 ❌ Error obteniendo información de peer {peer}: {type(e).__name__}: {e}")
        
        # Fase 4: Implementar reconciliación completa
        print(f"[NAMENODE] 🔄 Llamando a perform_full_reconciliation()...")
        perform_full_reconciliation(reunited_peers)
        
        elapsed_time = time.time() - start_time
        print(f"[NAMENODE] 🔄 ========== RECONCILIACIÓN COMPLETADA ==========")
        print(f"[NAMENODE] 🔄 Tiempo total: {elapsed_time:.2f} segundos")
        print(f"[NAMENODE] 🔄 Timestamp fin: {datetime.now().isoformat()}")
        
    finally:
        with cluster_lock:
            cluster_state["reconciliation_in_progress"] = False


def get_peer_operation_log(peer_id: str) -> Optional[List[OperationLog]]:
    """
    Fase 4: Obtiene el log de operaciones de un peer.
    
    Args:
        peer_id: ID del peer del cual obtener el log
    
    Returns:
        Lista de operaciones o None si hay error
    """
    try:
        peer_url = get_peer_url(peer_id)
        
        # Obtener token de servicio para autenticación
        try:
            service_token = generate_service_token(cluster_state["node_id"], "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        response = requests.get(
            f"{peer_url}/internal/operation-log",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=10
        )
        
        if response.status_code != 200:
            print(f"[NAMENODE] FASE 4: Error obteniendo log de {peer_id}: HTTP {response.status_code}")
            return None
        
        data = response.json()
        operations_data = data.get("operations", [])
        
        operations = []
        for op_data in operations_data:
            operations.append(OperationLog(
                operation=op_data["operation"],
                data=op_data["data"],
                term=op_data["term"],
                timestamp=op_data["timestamp"]
            ))
        
        return operations
        
    except Exception as e:
        print(f"[NAMENODE] FASE 4: Error obteniendo log de {peer_id}: {e}")
        return None


def get_operation_key(operation: OperationLog) -> str:
    """
    Fase 4: Obtiene una clave única para una operación para comparación.
    
    Args:
        operation: Operación a procesar
    
    Returns:
        Clave única para la operación
    """
    if operation.operation == "add_file":
        # Clave: nombre de archivo + user_id
        return f"{operation.data.get('name', '')}:{operation.data.get('user_id', '')}"
    elif operation.operation == "delete_file":
        # Clave: file_id
        return str(operation.data.get("file_id", ""))
    elif operation.operation == "delete_files_by_tags":
        # Clave: tags ordenadas + user_id
        tags = sorted(operation.data.get("tags", []))
        return f"{','.join(tags)}:{operation.data.get('user_id', '')}"
    elif operation.operation in ["add_tags", "delete_tags"]:
        # Clave: query_tags + user_id
        query_tags = sorted(operation.data.get("query_tags", []))
        return f"{','.join(query_tags)}:{operation.data.get('user_id', '')}"
    elif operation.operation == "create_user":
        # Clave: username
        return operation.data.get("username", "")
    elif operation.operation == "change_password":
        # Clave: username
        return operation.data.get("username", "")
    else:
        # Clave genérica: operación + datos serializados
        return f"{operation.operation}:{str(operation.data)}"


def compare_operation_logs(local_log: List[OperationLog], peer_log: List[OperationLog]) -> Dict:
    """
    Fase 4: Compara dos logs de operaciones para encontrar diferencias.
    
    Returns:
        Dict con:
        - missing_in_local: operaciones del peer que no están en local
        - missing_in_peer: operaciones locales que no están en peer
        - conflicts: operaciones que existen en ambos pero con diferencias
    """
    # Crear índices por (operation, data_key) para búsqueda rápida
    local_index = {}
    for op in local_log:
        key = (op.operation, get_operation_key(op))
        if key not in local_index:
            local_index[key] = []
        local_index[key].append(op)
    
    peer_index = {}
    for op in peer_log:
        key = (op.operation, get_operation_key(op))
        if key not in peer_index:
            peer_index[key] = []
        peer_index[key].append(op)
    
    missing_in_local = []
    missing_in_peer = []
    conflicts = []
    
    # Encontrar operaciones en peer que no están en local
    for key, peer_ops in peer_index.items():
        if key not in local_index:
            missing_in_local.extend(peer_ops)
        else:
            # Verificar si hay diferencias (conflictos)
            local_ops = local_index[key]
            for peer_op in peer_ops:
                # Buscar operación equivalente en local
                found_match = False
                for local_op in local_ops:
                    # Comparación más robusta: mismo term y timestamp similar (hasta 5 segundos de diferencia)
                    # o mismo term y mismo hash de datos (para operaciones de archivos)
                    timestamp_match = abs(local_op.timestamp - peer_op.timestamp) < 5.0
                    term_match = local_op.term == peer_op.term
                    
                    # Para operaciones de archivos, también comparar hash si está disponible
                    hash_match = True
                    if peer_op.operation == "add_file" and local_op.operation == "add_file":
                        peer_hash = peer_op.data.get("hash", "")
                        local_hash = local_op.data.get("hash", "")
                        if peer_hash and local_hash:
                            hash_match = peer_hash == local_hash
                    
                    if term_match and timestamp_match and hash_match:
                        # Misma operación
                        found_match = True
                        break
                
                if not found_match:
                    # Misma operación pero diferente term/timestamp/hash = conflicto potencial
                    conflicts.append({
                        "local": local_ops[0] if local_ops else None,
                        "peer": peer_op
                    })
    
    # Encontrar operaciones en local que no están en peer
    for key, local_ops in local_index.items():
        if key not in peer_index:
            missing_in_peer.extend(local_ops)
    
    return {
        "missing_in_local": missing_in_local,
        "missing_in_peer": missing_in_peer,
        "conflicts": conflicts
    }


def apply_operation_safely(operation: OperationLog, node_id: str = None):
    """
    Fase 4: Aplica una operación de forma segura, verificando que no cause conflictos.
    
    Args:
        operation: Operación a aplicar
        node_id: ID del nodo (opcional)
    """
    if node_id is None:
        node_id = NODE_ID
    
    try:
        operation_data = operation.data
        
        if operation.operation == "add_file":
            # Verificar si el archivo ya existe
            from namenode.manager import get_file_by_id, query_files
            existing_files = query_files(
                query_tags=operation_data.get("tags", []),
                node_id=node_id,
                user_id=operation_data.get("user_id", "system")
            )
            
            # Buscar archivo con mismo nombre y usuario
            file_exists = False
            for file_id, name, _ in existing_files:
                file_info = get_file_by_id(file_id, node_id=node_id)
                if file_info and file_info.get("name") == operation_data.get("name"):
                    file_exists = True
                    # Verificar si es conflicto (diferente hash)
                    if file_info.get("hash") != operation_data.get("hash"):
                        print(f"[NAMENODE] FASE 4: Conflicto detectado - archivo {operation_data.get('name')} tiene diferentes hashes")
                        # Resolver conflicto: last-write-wins
                        file_version = file_info.get("version", 1)
                        file_timestamp = file_info.get("last_modified_timestamp", 0)
                        if operation.timestamp > file_timestamp:
                            print(f"[NAMENODE] FASE 4: Aplicando versión más reciente (timestamp: {operation.timestamp} > {file_timestamp})")
                            # Actualizar archivo existente
                            add_file_metadata(
                                name=operation_data["name"],
                                tags=operation_data["tags"],
                                size=operation_data.get("size"),
                                hash_value=operation_data.get("hash"),
                                node_id=node_id,
                                user_id=operation_data.get("user_id", "system"),
                                term=operation.term
                            )
                            # Guardar réplicas si están en los datos (también cuando se actualiza)
                            if "datanode_ids" in operation_data:
                                from namenode.datanode_manager import save_file_replicas
                                # Obtener file_id del archivo actualizado
                                updated_files = query_files(
                                    query_tags=operation_data.get("tags", []),
                                    node_id=node_id,
                                    user_id=operation_data.get("user_id", "system")
                                )
                                for fid, name, _ in updated_files:
                                    file_info_updated = get_file_by_id(fid, node_id=node_id)
                                    if file_info_updated and file_info_updated.get("name") == operation_data.get("name"):
                                        save_file_replicas(fid, operation_data["datanode_ids"], node_id_db=NODE_ID)
                                        print(f"[NAMENODE] FASE 4: Réplicas actualizadas para archivo existente: {fid}")
                                        break
                    break
            
            if not file_exists:
                # Archivo no existe, agregarlo
                file_id = add_file_metadata(
                    name=operation_data["name"],
                    tags=operation_data["tags"],
                    size=operation_data.get("size"),
                    hash_value=operation_data.get("hash"),
                    node_id=node_id,
                    user_id=operation_data.get("user_id", "system"),
                    term=operation.term
                )
                # Guardar réplicas si están en los datos
                # IMPORTANTE: Siempre usar NODE_ID del contenedor actual, no el node_id del parámetro
                # para asegurar que las réplicas se guarden en la base de datos correcta
                if file_id and "datanode_ids" in operation_data:
                    from namenode.datanode_manager import save_file_replicas
                    save_file_replicas(file_id, operation_data["datanode_ids"], node_id_db=NODE_ID)
        
        elif operation.operation == "delete_file":
            delete_file_metadata(
                operation_data["file_id"],
                node_id=node_id,
                term=operation.term
            )
        
        elif operation.operation == "delete_files_by_tags":
            delete_files_by_tags(
                operation_data["tags"],
                node_id=node_id,
                user_id=operation_data.get("user_id", "system")
            )
        
        elif operation.operation == "add_tags":
            add_tags_to_files(
                operation_data["query_tags"],
                operation_data["new_tags"],
                node_id=node_id,
                user_id=operation_data.get("user_id", "system"),
                term=operation.term
            )
        
        elif operation.operation == "delete_tags":
            delete_tags_from_files(
                operation_data["query_tags"],
                operation_data["del_tags"],
                node_id=node_id,
                user_id=operation_data.get("user_id", "system"),
                term=operation.term
            )
        
        elif operation.operation == "create_user":
            # Replicar creación de usuario
            from security.auth import get_users_db_path
            db_path = get_users_db_path(node_id)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT username FROM users WHERE username = ?", (operation_data["username"],))
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO users (username, password_hash, role, is_active)
                        VALUES (?, ?, ?, ?)
                    """, (
                        operation_data["username"],
                        operation_data["password_hash"],
                        operation_data["role"],
                        1 if operation_data.get("is_active", True) else 0
                    ))
                    conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[NAMENODE] FASE 4: Error aplicando create_user: {e}")
            finally:
                conn.close()
        
        elif operation.operation == "change_password":
            # Replicar cambio de contraseña - SIEMPRE usar write_node_id (NODE_ID)
            from security.auth import get_users_db_path
            db_path = get_users_db_path(NODE_ID)  # Usar NODE_ID del contenedor actual
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    UPDATE users 
                    SET password_hash = ?
                    WHERE username = ?
                """, (operation_data["new_password_hash"], operation_data["username"]))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[NAMENODE] FASE 4: Error aplicando change_password: {e}")
            finally:
                conn.close()
        
        print(f"[NAMENODE] 🔄 [APPLY] ✅ Operación {operation.operation} aplicada correctamente")
        if operation.operation == "add_file":
            print(f"[NAMENODE] 🔄 [APPLY]   - Archivo: {operation_data.get('name', 'N/A')}")
            print(f"[NAMENODE] 🔄 [APPLY]   - Hash: {operation_data.get('hash', 'N/A')[:32]}...")
            print(f"[NAMENODE] 🔄 [APPLY]   - Tamaño: {operation_data.get('size', 0)} bytes")
            print(f"[NAMENODE] 🔄 [APPLY]   - Tags: {operation_data.get('tags', [])}")
            print(f"[NAMENODE] 🔄 [APPLY]   - Réplicas: {operation_data.get('datanode_ids', [])}")
        
    except Exception as e:
        print(f"[NAMENODE] 🔄 [APPLY] ❌ ERROR aplicando operación {operation.operation}: {type(e).__name__}: {e}")
        print(f"[NAMENODE] 🔄 [APPLY]   - Term: {operation.term}")
        print(f"[NAMENODE] 🔄 [APPLY]   - Timestamp: {operation.timestamp}")
        print(f"[NAMENODE] 🔄 [APPLY]   - Datos: {operation.data}")
        import traceback
        traceback.print_exc()


def perform_full_reconciliation(reunited_peers: List[str]):
    """
    Fase 4: Realiza reconciliación completa después de particionamiento.
    
    Proceso:
    1. Obtener logs de operaciones de todos los peers reunificados
    2. Comparar logs para encontrar operaciones faltantes
    3. Aplicar operaciones faltantes en orden (por term y timestamp)
    4. Detectar y resolver conflictos (last-write-wins)
    5. Verificar integridad de datos físicos
    6. Re-replicar archivos faltantes
    
    Args:
        reunited_peers: Lista de peer IDs que han vuelto a estar disponibles
    """
    reconciliation_start = time.time()
    print(f"[NAMENODE] 🔄 ========== RECONCILIACIÓN COMPLETA - INICIO ==========")
    print(f"[NAMENODE] 🔄 Nodo: {NODE_ID}")
    print(f"[NAMENODE] 🔄 Peers a reconciliar: {reunited_peers}")
    print(f"[NAMENODE] 🔄 Timestamp: {datetime.now().isoformat()}")
    
    with cluster_lock:
        current_term = cluster_state["term"]
        current_node_id = cluster_state["node_id"]
    
    print(f"[NAMENODE] 🔄 Estado inicial:")
    print(f"[NAMENODE] 🔄   - Term local: {current_term}")
    print(f"[NAMENODE] 🔄   - Es líder: {cluster_state['is_leader']}")
    print(f"[NAMENODE] 🔄   - Líder conocido: {cluster_state.get('leader_id', 'None')}")
    
    # Paso 1: Obtener logs de todos los peers reunificados
    print(f"[NAMENODE] 🔄 [PASO 1] Obteniendo logs de operaciones de {len(reunited_peers)} peers...")
    peer_logs = {}
    for peer in reunited_peers:
        print(f"[NAMENODE] 🔄 [PASO 1] Obteniendo log de {peer}...")
        peer_log = get_peer_operation_log(peer)
        if peer_log:
            peer_logs[peer] = peer_log
            # Contar operaciones por tipo
            op_counts = {}
            for op in peer_log:
                op_counts[op.operation] = op_counts.get(op.operation, 0) + 1
            print(f"[NAMENODE] 🔄 [PASO 1] ✅ Log de {peer}: {len(peer_log)} operaciones totales")
            print(f"[NAMENODE] 🔄 [PASO 1]   Desglose: {op_counts}")
        else:
            print(f"[NAMENODE] 🔄 [PASO 1] ❌ No se pudo obtener log de {peer}")
    
    if not peer_logs:
        print(f"[NAMENODE] 🔄 ❌ ERROR: No se pudieron obtener logs de ningún peer, abortando reconciliación")
        return
    
    # Paso 2: Cargar log local
    print(f"[NAMENODE] 🔄 [PASO 2] Cargando log local...")
    local_log = load_operation_log(NODE_ID)
    op_counts_local = {}
    for op in local_log:
        op_counts_local[op.operation] = op_counts_local.get(op.operation, 0) + 1
    print(f"[NAMENODE] 🔄 [PASO 2] ✅ Log local: {len(local_log)} operaciones totales")
    print(f"[NAMENODE] 🔄 [PASO 2]   Desglose: {op_counts_local}")
    
    # Paso 3: Determinar líder válido (mayor term)
    print(f"[NAMENODE] 🔄 [PASO 3] Determinando líder válido (mayor term)...")
    max_term = current_term
    leader_peer = None
    
    for peer, log in peer_logs.items():
        if log:
            # Obtener el term máximo del log del peer
            peer_max_term = max((op.term for op in log), default=0)
            try:
                peer_url = get_peer_url(peer)
                response = requests.get(f"{peer_url}/", timeout=3)
                if response.status_code == 200:
                    peer_data = response.json()
                    peer_current_term = peer_data.get("term", 0)
                    peer_max_term = max(peer_max_term, peer_current_term)
            except Exception as e:
                print(f"[NAMENODE] 🔄 [PASO 3] ⚠️  Error obteniendo term actual de {peer}: {e}")
            
            print(f"[NAMENODE] 🔄 [PASO 3] Peer {peer}: term máximo = {peer_max_term}")
            if peer_max_term > max_term:
                max_term = peer_max_term
                leader_peer = peer
                print(f"[NAMENODE] 🔄 [PASO 3] ✅ Nuevo líder candidato: {peer} (term {max_term})")
    
    print(f"[NAMENODE] 🔄 [PASO 3] ✅ Term máximo encontrado: {max_term}")
    print(f"[NAMENODE] 🔄 [PASO 3] ✅ Líder identificado: {leader_peer or current_node_id}")
    
    # Paso 4: Si hay un líder con term mayor, sincronizar desde él
    if leader_peer and max_term > current_term:
        print(f"[NAMENODE] 🔄 [PASO 4] Sincronizando desde líder {leader_peer} (term {max_term} > {current_term})")
        leader_log = peer_logs[leader_peer]
        
        # Aplicar todas las operaciones del líder que no están en local
        # Ordenar por term y timestamp
        leader_log_sorted = sorted(leader_log, key=lambda op: (op.term, op.timestamp))
        print(f"[NAMENODE] 🔄 [PASO 4] Log del líder tiene {len(leader_log_sorted)} operaciones ordenadas")
        
        applied_count = 0
        skipped_count = 0
        op_type_counts = {}
        for operation in leader_log_sorted:
            # Verificar si la operación ya está en local
            already_applied = False
            for local_op in local_log:
                # Comparación más robusta
                timestamp_match = abs(local_op.timestamp - operation.timestamp) < 5.0
                term_match = local_op.term == operation.term
                operation_match = local_op.operation == operation.operation
                
                if term_match and timestamp_match and operation_match:
                    already_applied = True
                    skipped_count += 1
                    break
            
            if not already_applied:
                print(f"[NAMENODE] 🔄 [PASO 4] Aplicando operación: {operation.operation} (term {operation.term}, timestamp {operation.timestamp:.2f})")
                apply_operation_safely(operation, NODE_ID)
                # Guardar operación aplicada en el log persistente
                save_operation_to_log(operation, NODE_ID)
                # Agregar al log en memoria
                with log_lock:
                    operation_log.append(operation)
                applied_count += 1
                op_type_counts[operation.operation] = op_type_counts.get(operation.operation, 0) + 1
        
        print(f"[NAMENODE] 🔄 [PASO 4] ✅ Resumen sincronización desde líder:")
        print(f"[NAMENODE] 🔄 [PASO 4]   - Operaciones aplicadas: {applied_count}")
        print(f"[NAMENODE] 🔄 [PASO 4]   - Operaciones saltadas (ya existían): {skipped_count}")
        print(f"[NAMENODE] 🔄 [PASO 4]   - Desglose aplicadas: {op_type_counts}")
        
        # Recargar log local después de aplicar operaciones del líder
        local_log = load_operation_log(NODE_ID)
        print(f"[NAMENODE] 🔄 [PASO 4] ✅ Log local actualizado: {len(local_log)} operaciones")
        
        # Actualizar term local
        with cluster_lock:
            if max_term > cluster_state["term"]:
                old_term = cluster_state["term"]
                cluster_state["term"] = max_term
                cluster_state["leader_id"] = leader_peer
                cluster_state["is_leader"] = False
                print(f"[NAMENODE] 🔄 [PASO 4] ✅ Term actualizado: {old_term} -> {max_term}")
    else:
        print(f"[NAMENODE] 🔄 [PASO 4] ⏭️  No hay líder con term mayor, saltando sincronización desde líder")
    
    # Paso 5: Comparar con otros peers y aplicar operaciones faltantes
    print(f"[NAMENODE] 🔄 [PASO 5] Comparando con otros peers y aplicando operaciones faltantes...")
    total_peers_processed = 0
    total_operations_applied = 0
    total_conflicts_resolved = 0
    
    for peer, peer_log in peer_logs.items():
        if peer == leader_peer:
            print(f"[NAMENODE] 🔄 [PASO 5] ⏭️  Saltando {peer} (ya procesado como líder)")
            continue  # Ya procesamos el líder
        
        total_peers_processed += 1
        print(f"[NAMENODE] 🔄 [PASO 5] Comparando con peer {peer}...")
        comparison = compare_operation_logs(local_log, peer_log)
        
        missing_count = len(comparison["missing_in_local"])
        conflicts_count = len(comparison["conflicts"])
        missing_in_peer_count = len(comparison["missing_in_peer"])
        
        print(f"[NAMENODE] 🔄 [PASO 5] Resultados comparación con {peer}:")
        print(f"[NAMENODE] 🔄 [PASO 5]   - Operaciones faltantes en local: {missing_count}")
        print(f"[NAMENODE] 🔄 [PASO 5]   - Operaciones faltantes en peer: {missing_in_peer_count}")
        print(f"[NAMENODE] 🔄 [PASO 5]   - Conflictos detectados: {conflicts_count}")
        
        # Aplicar operaciones faltantes (ordenadas por term y timestamp)
        missing_ops = sorted(comparison["missing_in_local"], key=lambda op: (op.term, op.timestamp))
        applied_from_peer = 0
        op_type_counts_peer = {}
        for operation in missing_ops:
            print(f"[NAMENODE] 🔄 [PASO 5]   Aplicando operación faltante: {operation.operation} (term {operation.term}, timestamp {operation.timestamp:.2f})")
            apply_operation_safely(operation, NODE_ID)
            # Guardar operación aplicada en el log persistente
            save_operation_to_log(operation, NODE_ID)
            # Agregar al log en memoria
            with log_lock:
                operation_log.append(operation)
            applied_from_peer += 1
            total_operations_applied += 1
            op_type_counts_peer[operation.operation] = op_type_counts_peer.get(operation.operation, 0) + 1
        
        if applied_from_peer > 0:
            print(f"[NAMENODE] 🔄 [PASO 5]   ✅ Aplicadas {applied_from_peer} operaciones de {peer}")
            print(f"[NAMENODE] 🔄 [PASO 5]   Desglose: {op_type_counts_peer}")
        
        # Resolver conflictos (last-write-wins)
        conflicts_resolved = 0
        for conflict in comparison["conflicts"]:
            local_op = conflict.get("local")
            peer_op = conflict.get("peer")
            
            if local_op and peer_op:
                # Usar timestamp para decidir (last-write-wins)
                if peer_op.timestamp > local_op.timestamp:
                    print(f"[NAMENODE] 🔄 [PASO 5]   Resolviendo conflicto: {peer_op.operation} - aplicando versión de peer (timestamp: {peer_op.timestamp:.2f} > {local_op.timestamp:.2f})")
                    apply_operation_safely(peer_op, NODE_ID)
                    # Guardar operación aplicada en el log persistente
                    save_operation_to_log(peer_op, NODE_ID)
                    # Agregar al log en memoria
                    with log_lock:
                        operation_log.append(peer_op)
                    conflicts_resolved += 1
                    total_conflicts_resolved += 1
                else:
                    print(f"[NAMENODE] 🔄 [PASO 5]   Resolviendo conflicto: {peer_op.operation} - manteniendo versión local (timestamp: {local_op.timestamp:.2f} >= {peer_op.timestamp:.2f})")
        
        if conflicts_resolved > 0:
            print(f"[NAMENODE] 🔄 [PASO 5]   ✅ Resueltos {conflicts_resolved} conflictos con {peer}")
        
        # Recargar log local después de aplicar operaciones de este peer
        local_log = load_operation_log(NODE_ID)
        print(f"[NAMENODE] 🔄 [PASO 5] ✅ Log local actualizado después de procesar {peer}: {len(local_log)} operaciones")
    
    print(f"[NAMENODE] 🔄 [PASO 5] ✅ Resumen comparación con peers:")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Peers procesados: {total_peers_processed}")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Total operaciones aplicadas: {total_operations_applied}")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Total conflictos resueltos: {total_conflicts_resolved}")
    
    # Paso 6: Verificar integridad de datos físicos y re-replicar si es necesario
    print(f"[NAMENODE] 🔄 [PASO 6] Verificando integridad de réplicas...")
    verify_and_rereplicate_files(NODE_ID)
    
    # Paso 7: Reconciliar tablas operation_states y operation_state_log
    print(f"[NAMENODE] 🔄 [PASO 7] Reconciliando operation_states y operation_state_log...")
    try:
        from namenode.database import get_operations_db_path, get_connection, close_connection, operations_db_lock
        import json
        
        # Obtener operation_states locales
        local_ops_db_path = get_operations_db_path(NODE_ID)
        local_conn, local_cursor = get_connection(db_path=local_ops_db_path, db_type="operations", node_id=NODE_ID)
        
        try:
            with operations_db_lock:
                local_cursor.execute("SELECT * FROM operation_states")
                local_operation_states = {row['operation_id']: dict(row) for row in local_cursor.fetchall()}
        finally:
            close_connection(local_conn)
        
        print(f"[NAMENODE] 🔄 [PASO 7] Operation states locales: {len(local_operation_states)}")
        
        # Obtener operation_states de los peers y reconciliar
        reconciled_states = {}
        for peer in reunited_peers:
            try:
                peer_url = get_peer_url(peer)
                # Obtener token de servicio para autenticación
                try:
                    service_token = generate_service_token(NODE_ID, "service")
                except Exception:
                    service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                
                # Intentar obtener operation_states del peer (si hay endpoint)
                # Por ahora, solo registramos que intentamos reconciliar
                print(f"[NAMENODE] 🔄 [PASO 7] Peer {peer}: operation_states no se pueden obtener directamente, se reconciliarán en la próxima operación")
            except Exception as e:
                print(f"[NAMENODE] 🔄 [PASO 7] ⚠️  Error reconciliando operation_states de {peer}: {e}")
        
        # Reconciliar operation_state_log (similar proceso)
        local_conn, local_cursor = get_connection(db_path=local_ops_db_path, db_type="operations", node_id=NODE_ID)
        try:
            with operations_db_lock:
                local_cursor.execute("SELECT COUNT(*) FROM operation_state_log")
                local_log_count = local_cursor.fetchone()[0]
        finally:
            close_connection(local_conn)
        
        print(f"[NAMENODE] 🔄 [PASO 7] ✅ Operation state logs locales: {local_log_count} entradas")
        print(f"[NAMENODE] 🔄 [PASO 7] ⚠️  Nota: operation_states y operation_state_log se reconciliarán automáticamente durante las operaciones normales")
    except Exception as e:
        print(f"[NAMENODE] 🔄 [PASO 7] ❌ Error en reconciliación de operation_states: {e}")
        import traceback
        traceback.print_exc()
    
    # Paso 8: Sincronizar term final con todos los namenodes y verificar versiones
    print(f"[NAMENODE] 🔄 [PASO 8] Sincronizando term final con todos los namenodes...")
    with cluster_lock:
        final_term = cluster_state["term"]
        all_peers = list(set(reunited_peers + [current_node_id]))
        is_leader_after_reconciliation = cluster_state["is_leader"]
    
    # Si este nodo es el líder después de la reconciliación, asegurar que todos los peers tengan el term correcto
    synced_count = 0
    failed_count = 0
    peers_needing_update = []
    
    for peer in all_peers:
        if peer == current_node_id:
            # Ya tenemos el term actualizado localmente
            synced_count += 1
            continue
        
        try:
            peer_url = get_peer_url(peer)
            # Obtener token de servicio
            try:
                service_token = generate_service_token(NODE_ID, "service")
            except Exception:
                service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
            
            # Verificar si el peer necesita actualización
            try:
                response = requests.get(f"{peer_url}/", timeout=3)
                if response.status_code == 200:
                    peer_data = response.json()
                    peer_term = peer_data.get("term", 0)
                    
                    if peer_term < final_term:
                        # El peer necesita actualización - agregar a la lista
                        peers_needing_update.append((peer, peer_term))
                        print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Peer {peer} tiene term {peer_term} < {final_term}, necesita actualización")
                        failed_count += 1
                    elif peer_term == final_term:
                        print(f"[NAMENODE] 🔄 [PASO 8] ✅ Peer {peer} ya tiene el term correcto ({final_term})")
                        synced_count += 1
                    else:
                        print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Peer {peer} tiene term mayor ({peer_term} > {final_term}), debería ejecutar su propia reconciliación")
                        failed_count += 1
                else:
                    print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Error HTTP {response.status_code} consultando peer {peer}")
                    failed_count += 1
            except Exception as e:
                print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Error verificando term de peer {peer}: {e}")
                failed_count += 1
        except Exception as e:
            print(f"[NAMENODE] 🔄 [PASO 8] ❌ Error en sincronización de term con {peer}: {e}")
            failed_count += 1
    
    # Si somos el líder y hay peers que necesitan actualización, enviar heartbeat para forzar actualización
    if is_leader_after_reconciliation and peers_needing_update:
        print(f"[NAMENODE] 🔄 [PASO 8] Como líder, enviando heartbeats a {len(peers_needing_update)} peers para sincronizar term...")
        for peer, peer_term in peers_needing_update:
            try:
                peer_url = get_peer_url(peer)
                try:
                    service_token = generate_service_token(NODE_ID, "service")
                except Exception:
                    service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                
                # Enviar heartbeat con el term actualizado
                # El heartbeat actualizará automáticamente el term del peer si es menor
                try:
                    response = requests.post(
                        f"{peer_url}/internal/heartbeat",
                        json={
                            "leader_id": NODE_ID,
                            "term": final_term,
                            "peers": all_peers
                        },
                        headers={"Authorization": f"Bearer {service_token}"},
                        timeout=3
                    )
                    if response.status_code == 200:
                        print(f"[NAMENODE] 🔄 [PASO 8] ✅ Heartbeat enviado a {peer}, term debería actualizarse")
                        synced_count += 1
                        failed_count -= 1
                    else:
                        print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Error enviando heartbeat a {peer}: HTTP {response.status_code}")
                except Exception as e:
                    print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Error enviando heartbeat a {peer}: {e}")
            except Exception as e:
                print(f"[NAMENODE] 🔄 [PASO 8] ❌ Error procesando peer {peer}: {e}")
    
    # Verificar versiones en la tabla files
    print(f"[NAMENODE] 🔄 [PASO 8] Verificando campos de versión en tabla files...")
    try:
        from namenode.database import get_metadata_db_path, get_connection, close_connection, metadata_db_lock
        db_path = get_metadata_db_path(NODE_ID)
        conn, cursor = get_connection(db_path=db_path, db_type="metadata", node_id=NODE_ID)
        
        try:
            with metadata_db_lock:
                # Verificar que todos los archivos tengan campos de versión válidos
                cursor.execute("""
                    SELECT COUNT(*) as total,
                           COUNT(CASE WHEN version IS NULL THEN 1 END) as null_version,
                           COUNT(CASE WHEN last_modified_term IS NULL THEN 1 END) as null_term,
                           COUNT(CASE WHEN last_modified_timestamp IS NULL THEN 1 END) as null_timestamp
                    FROM files
                """)
                stats = cursor.fetchone()
                
                print(f"[NAMENODE] 🔄 [PASO 8] Estado de versiones en tabla files:")
                print(f"[NAMENODE] 🔄 [PASO 8]   - Total archivos: {stats['total']}")
                print(f"[NAMENODE] 🔄 [PASO 8]   - Archivos sin version: {stats['null_version']}")
                print(f"[NAMENODE] 🔄 [PASO 8]   - Archivos sin last_modified_term: {stats['null_term']}")
                print(f"[NAMENODE] 🔄 [PASO 8]   - Archivos sin last_modified_timestamp: {stats['null_timestamp']}")
                
                # Corregir archivos sin versiones válidas
                fixed_count = 0
                if stats['null_version'] > 0 or stats['null_term'] > 0 or stats['null_timestamp'] > 0:
                    cursor.execute("""
                        UPDATE files 
                        SET version = COALESCE(version, 1),
                            last_modified_term = COALESCE(last_modified_term, ?),
                            last_modified_timestamp = COALESCE(last_modified_timestamp, ?)
                        WHERE version IS NULL OR last_modified_term IS NULL OR last_modified_timestamp IS NULL
                    """, (final_term, time.time()))
                    fixed_count = cursor.rowcount
                    conn.commit()
                    
                    if fixed_count > 0:
                        print(f"[NAMENODE] 🔄 [PASO 8] ✅ Corregidos {fixed_count} archivos con campos de versión inválidos")
        finally:
            close_connection(conn)
    except Exception as e:
        print(f"[NAMENODE] 🔄 [PASO 8] ⚠️  Error verificando versiones: {e}")
    
    print(f"[NAMENODE] 🔄 [PASO 8] ✅ Sincronización de term completada: {synced_count} exitosos, {failed_count} con problemas")
    print(f"[NAMENODE] 🔄 [PASO 8] Term final: {final_term} (debe ser el mismo en todos los namenodes después de heartbeats)")
    
    # Estadísticas finales
    final_log = load_operation_log(NODE_ID)
    final_op_counts = {}
    for op in final_log:
        final_op_counts[op.operation] = final_op_counts.get(op.operation, 0) + 1
    
    elapsed_time = time.time() - reconciliation_start
    print(f"[NAMENODE] 🔄 ========== RECONCILIACIÓN COMPLETA - FINALIZADA ==========")
    print(f"[NAMENODE] 🔄 Tiempo total: {elapsed_time:.2f} segundos")
    print(f"[NAMENODE] 🔄 Timestamp fin: {datetime.now().isoformat()}")
    print(f"[NAMENODE] 🔄 Estado final:")
    print(f"[NAMENODE] 🔄   - Log local final: {len(final_log)} operaciones")
    print(f"[NAMENODE] 🔄   - Desglose final: {final_op_counts}")
    with cluster_lock:
        print(f"[NAMENODE] 🔄   - Term final: {cluster_state['term']}")
        print(f"[NAMENODE] 🔄   - Es líder: {cluster_state['is_leader']}")
        print(f"[NAMENODE] 🔄   - Líder conocido: {cluster_state.get('leader_id', 'None')}")
    print(f"[NAMENODE] 🔄 ==========================================================")


def verify_and_rereplicate_files(node_id: str = None):
    """
    Fase 4: Verifica que todos los archivos tengan suficientes réplicas y re-replica si es necesario.
    
    Args:
        node_id: ID del nodo
    """
    if node_id is None:
        node_id = NODE_ID
    
    from namenode.datanode_manager import get_file_replicas, get_active_datanodes, assign_replicas, save_file_replicas
    from namenode.manager import query_files, get_file_by_id
    from namenode.database import get_db_path, get_connection, close_connection, db_lock
    
    db_path = get_db_path(node_id)
    
    with db_lock:
        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
        try:
            # Obtener todos los archivos
            cursor.execute("SELECT id FROM files")
            file_ids = [row[0] for row in cursor.fetchall()]
        finally:
            close_connection(conn)
    
    active_datanodes = get_active_datanodes(node_id_db=node_id)
    if len(active_datanodes) < 2:
        print(f"[NAMENODE] FASE 4: No hay suficientes DataNodes activos para verificar réplicas")
        return
    
    rereplicated_count = 0
    for file_id in file_ids:
        replicas = get_file_replicas(file_id, node_id_db=node_id)
        
        # Sincronizar contador con la realidad
        active_replicas = [r for r in replicas if r.get("status") == "active"]
        active_count = len(active_replicas)
        from namenode.datanode_manager import update_replica_count
        update_replica_count(file_id, active_count, node_id_db=node_id)
        
        # Verificar que haya al menos 2 réplicas (o 1 si solo hay 1 DataNode)
        min_replicas = min(2, len(active_datanodes))
        
        if active_count < min_replicas:
            print(f"[NAMENODE] FASE 4: Archivo {file_id} tiene solo {active_count} réplicas, necesita {min_replicas}")
            
            # Obtener información del archivo
            file_info = get_file_by_id(file_id, node_id=node_id)
            if not file_info:
                continue
            
            file_hash = file_info.get("hash", "")
            if file_hash.startswith("sha256:"):
                file_hash = file_hash[7:]
            
            file_size = file_info.get("size", 0)
            
            # Obtener DataNodes que ya tienen el archivo
            existing_datanodes = [r["datanode_id"] for r in replicas]
            
            # Verificar que haya al menos una réplica activa existente para copiar desde ella
            if active_count == 0:
                print(f"[NAMENODE] FASE 4: ⚠️  Archivo {file_id} no tiene réplicas activas, no se puede re-replicar")
                print(f"[NAMENODE] FASE 4:   - Hash: {file_hash[:32]}...")
                print(f"[NAMENODE] FASE 4:   - Tamaño: {file_size} bytes")
                continue
            
            # Asignar nuevas réplicas
            new_datanode_ids = assign_replicas(
                file_hash,
                file_size,
                node_id_db=node_id,
                exclude_datanodes=existing_datanodes
            )
            
            if new_datanode_ids:
                # Transferir físicamente el archivo a los nuevos datanodes
                # Usar rereplicate_to_reach_3 que está diseñado para agregar réplicas faltantes
                # o rereplicate_file con un datanode ficticio como fallido (solo para excluir)
                from namenode.datanode_manager import rereplicate_to_reach_3, rereplicate_file
                
                # Calcular cuántas réplicas faltan
                needed_replicas = min_replicas - active_count
                
                # Intentar usar rereplicate_to_reach_3 primero (mejor para este caso)
                # pero limitarlo al mínimo necesario
                if active_count < min_replicas:
                    # Usar rereplicate_to_reach_3 que maneja la transferencia física automáticamente
                    # pero primero verificar si necesitamos al menos 2 réplicas
                    target_replicas = min(3, min_replicas)  # Usar min_replicas o 3, el menor
                    
                    if active_count < target_replicas:
                        print(f"[NAMENODE] FASE 4: Usando rereplicate_to_reach_3 para archivo {file_id} (tiene {active_count}, necesita {target_replicas})...")
                        success = rereplicate_to_reach_3(file_id, node_id_db=node_id)
                        
                        if success:
                            # Actualizar contador de réplicas
                            updated_replicas = get_file_replicas(file_id, node_id_db=node_id)
                            new_active_count = len([r for r in updated_replicas if r.get("status") == "active"])
                            added_replicas = new_active_count - active_count
                            
                            if added_replicas > 0:
                                print(f"[NAMENODE] FASE 4: ✅ Re-replicación completada para archivo {file_id}: {added_replicas} réplicas agregadas")
                                rereplicated_count += added_replicas
                            else:
                                print(f"[NAMENODE] FASE 4: ⚠️  Re-replicación completada pero no se agregaron réplicas para archivo {file_id}")
                        else:
                            print(f"[NAMENODE] FASE 4: ❌ Error en re-replicación de archivo {file_id}")
                else:
                    # Ya tiene suficientes réplicas
                    print(f"[NAMENODE] FASE 4: Archivo {file_id} ya tiene suficientes réplicas ({active_count} >= {min_replicas})")
            else:
                print(f"[NAMENODE] FASE 4: ⚠️  No se pudieron asignar nuevos DataNodes para archivo {file_id}")
    
    if rereplicated_count > 0:
        print(f"[NAMENODE] FASE 4: Re-replicación completada: {rereplicated_count} réplicas asignadas")
    else:
        print(f"[NAMENODE] FASE 4: Todas las réplicas están correctas")


def replicate_to_peers(operation: OperationLog):
    """Replica una operación a los peers del cluster"""
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        term = cluster_state["term"]
        leader_id = cluster_state["node_id"]
    
    # Si no hay peers, no hay nada que replicar (modo desarrollo)
    if not peers:
        return True
    
    # 🔍 LOG: Ver qué node_id está usando el líder
    print(f"[NAMENODE] [REPLICATE] 🔍 Líder generando token con node_id: '{leader_id}'")
    
    # Obtener token de servicio para autenticación
    try:
        service_token = generate_service_token(cluster_state["node_id"], "service")
        print(f"[NAMENODE] [REPLICATE] ✅ Token generado exitosamente (primeros 20 chars): {service_token[:20]}...")
    except Exception as e:
        print(f"[NAMENODE] [REPLICATE] ⚠️ Error generando token: {e}, usando token pre-compartido")
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
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
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=3
            )
            if response.status_code == 200:
                success_count += 1
                # Fase 3: Actualizar conectividad
                update_peer_status(peer, True)
            else:
                update_peer_status(peer, False)
        except Exception as e:
            update_peer_status(peer, False)
            print(f"[NAMENODE] Error replicando a {peer}: {e}")
    
    # Se necesita mayoría (quorum): al menos 2 de 3 nodos
    total_nodes = len(peers) + 1  # +1 por este nodo
    quorum = (total_nodes // 2) + 1
    replicated = success_count + 1 >= quorum  # +1 por este nodo
    
    if not replicated:
        print(f"[NAMENODE] WARNING: Solo se replicó a {success_count + 1}/{total_nodes} nodos (quorum: {quorum})")
    
    return replicated


def replicate_sql_writes(sql_writes: List[Dict], node_id: str = None):
    """
    Replica escrituras SQL a los peers del cluster.
    
    Args:
        sql_writes: Lista de diccionarios con 'sql', 'params', y 'db_type'
        node_id: ID del nodo (opcional)
    """
    if node_id is None:
        node_id = NODE_ID
    
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        term = cluster_state["term"]
        leader_id = cluster_state["node_id"]
    
    # Si no hay peers o no hay escrituras, no hay nada que replicar
    if not peers or not sql_writes:
        return True
    
    # Obtener token de servicio para autenticación
    try:
        service_token = generate_service_token(cluster_state["node_id"], "service")
    except Exception as e:
        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error generando token: {e}, usando token pre-compartido")
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    success_count = 0
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/replicate-sql",
                json={
                    "sql_writes": sql_writes,
                    "term": term,
                    "timestamp": time.time()
                },
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=5
            )
            if response.status_code == 200:
                success_count += 1
                update_peer_status(peer, True)
            else:
                update_peer_status(peer, False)
                print(f"[NAMENODE] [SQL_REPLICATE] Error replicando a {peer}: {response.status_code}")
        except Exception as e:
            update_peer_status(peer, False)
            print(f"[NAMENODE] [SQL_REPLICATE] Error replicando a {peer}: {e}")
    
    # Se necesita mayoría (quorum): al menos 2 de 3 nodos
    total_nodes = len(peers) + 1
    quorum = (total_nodes // 2) + 1
    replicated = success_count + 1 >= quorum
    
    if not replicated:
        print(f"[NAMENODE] [SQL_REPLICATE] WARNING: Solo se replicó a {success_count + 1}/{total_nodes} nodos (quorum: {quorum})")
    else:
        print(f"[NAMENODE] [SQL_REPLICATE] ✅ {len(sql_writes)} escrituras SQL replicadas a {success_count + 1} peers")
    
    return replicated


def request_vote(candidate_id: str, term: int) -> bool:
    """Solicita votos para elección de líder entre los nodos disponibles"""
    with cluster_lock:
        all_peers = cluster_state["peers"].copy()
        peer_status = cluster_state.get("peer_status", {}).copy()
        old_leader_id = cluster_state.get("leader_id")
    
    # Filtrar peers: excluir los marcados como "dead" y el líder caído
    peers = []
    for peer in all_peers:
        status = peer_status.get(peer, {}).get("status", "unknown")
        # Excluir peers muertos
        if status == "dead":
            print(f"[NAMENODE] 🗳️  [VOTE] Excluyendo peer {peer} (status: dead)")
            continue
        # Excluir el líder anterior si está caído
        if peer == old_leader_id and status in ["dead", "suspected"]:
            print(f"[NAMENODE] 🗳️  [VOTE] Excluyendo líder caído {peer}")
            continue
        peers.append(peer)
    
    if not peers:
        # Si no hay peers disponibles, es nodo único
        print(f"[NAMENODE] 🗳️  Solicitud de votos: No hay peers disponibles (de {len(all_peers)} conocidos)")
        return True
    
    print(f"[NAMENODE] 🗳️  Solicitando votos a {len(peers)} peers activos: {sorted(peers)}")
    
    # Obtener token de servicio para autenticación
    try:
        service_token = generate_service_token(candidate_id, "service")
        print(f"[NAMENODE] 🗳️  [VOTE] Token generado para candidato {candidate_id}")
    except Exception as e:
        print(f"[NAMENODE] 🗳️  [VOTE] Error generando token para {candidate_id}: {e}, usando token pre-compartido")
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    # Verificar que el token se puede decodificar (para diagnóstico)
    try:
        from security.service_auth import verify_service_token
        test_payload = verify_service_token(service_token)
        if test_payload:
            test_service_id = test_payload.get("service_id") or test_payload.get("sub", "")
            print(f"[NAMENODE] 🗳️  [VOTE] Token verificado localmente: service_id={test_service_id}")
        else:
            print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Token no se puede verificar localmente, usando token pre-compartido")
    except Exception as e:
        print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Error verificando token localmente: {e}")
    
    votes = 1  # Voto propio
    successful_contacts = 1
    
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            print(f"[NAMENODE] 🗳️  [VOTE] Solicitando voto a {peer} ({peer_url}/internal/vote)")
            print(f"[NAMENODE] 🗳️  [VOTE] Candidato: {candidate_id}, Term: {term}")
            print(f"[NAMENODE] 🗳️  [VOTE] Token usado: {service_token[:20]}... (primeros 20 caracteres)")
            response = requests.post(
                f"{peer_url}/internal/vote",
                json={"candidate_id": candidate_id, "term": term},
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=2
            )
            if response.status_code == 200:
                successful_contacts += 1
                # Fase 3: Actualizar conectividad
                update_peer_status(peer, True)
                data = response.json()
                if data.get("granted"):
                    votes += 1
                    print(f"[NAMENODE] 🗳️  [VOTE] ✅ Voto concedido por {peer}")
                else:
                    print(f"[NAMENODE] 🗳️  [VOTE] ❌ Voto denegado por {peer}: {data}")
            else:
                update_peer_status(peer, False)
                print(f"[NAMENODE] 🗳️  [VOTE] ❌ Error HTTP {response.status_code} de {peer}: {response.text[:200] if hasattr(response, 'text') else 'N/A'}")
        except requests.exceptions.ConnectionError as e:
            # Errores de conexión (DNS, red, etc.) - esperados cuando el peer no está disponible
            update_peer_status(peer, False)
            error_msg = str(e)
            if "Failed to resolve" in error_msg or "name resolution" in error_msg.lower():
                print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Peer {peer} no disponible (no se puede resolver DNS)")
            elif "Connection refused" in error_msg or "refused" in error_msg.lower():
                print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Peer {peer} no disponible (conexión rechazada)")
            else:
                print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Peer {peer} no disponible: {type(e).__name__}")
        except requests.exceptions.Timeout:
            update_peer_status(peer, False)
            print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Peer {peer} no responde (timeout)")
        except Exception as e:
            # Otros errores inesperados - solo loguear sin traceback completo
            update_peer_status(peer, False)
            print(f"[NAMENODE] 🗳️  [VOTE] ⚠️  Error solicitando voto a {peer}: {type(e).__name__}: {e}")
    
    total_nodes = successful_contacts  # Solo contar nodos que respondieron
    
    # Si no se pudo contactar a ningún peer, verificar DNS
    if successful_contacts == 1:
        print(f"[NAMENODE] ⚠️  No se pudo contactar a ningún peer de {len(peers)} peers conocidos: {sorted(peers)}")
        # Verificar si hay más peers via DNS que podrían estar iniciándose
        dns_peers = discover_peers_dns()
        if dns_peers:
            print(f"[NAMENODE] ❌ DNS detecta {len(dns_peers)} peers, esperando a que estén listos...")
            return False
        else:
            print(f"[NAMENODE] ✅ DNS confirma que no hay otros peers, autoelegiéndose como líder")
        return True
    
    # Sin quorum: ganar con mayoría simple de los nodos que respondieron
    # Si obtuvimos más de la mitad de los votos de los nodos activos, somos líder
    if votes > total_nodes / 2:
        print(f"[NAMENODE] ✅ Elección ganada: {votes}/{total_nodes} votos de nodos activos")
        print(f"[NAMENODE] 📋 Peers contactados exitosamente: {successful_contacts - 1} de {len(peers)}")
        return True
    
    print(f"[NAMENODE] ❌ Elección perdida: {votes}/{total_nodes} votos de nodos activos")
    print(f"[NAMENODE] 📋 Peers contactados: {successful_contacts - 1} de {len(peers)}")
    return False


def check_existing_leader(peers: List[str]) -> Optional[str]:
    """
    Verifica si hay un líder activo intentando comunicarse directamente con él.
    Si el líder conocido no responde, retorna None para iniciar una votación.
    Solo si no hay líder conocido, consulta a los peers.
    
    Returns:
        ID del líder si se encuentra uno activo y responde, None en caso contrario
    """
    with cluster_lock:
        known_leader_id = cluster_state.get("leader_id")
    
    # PRIMERO: Intentar comunicarse directamente con el líder conocido (si existe)
    if known_leader_id:
        print(f"[NAMENODE] 🔍 Verificando líder conocido directamente: {known_leader_id}")
        try:
            leader_url = get_peer_url(known_leader_id)
            response = requests.get(f"{leader_url}/", timeout=3)
            if response.status_code == 200:
                data = response.json()
                # Verificar que realmente es el líder
                if data.get("is_leader") and (data.get("leader_id") == known_leader_id or data.get("node_id") == known_leader_id):
                    print(f"[NAMENODE] ✅ Líder conocido {known_leader_id} está activo y responde correctamente")
                    # Actualizar estado del líder en gossip
                    update_peer_status(known_leader_id, True)
                    return known_leader_id
                else:
                    print(f"[NAMENODE] ⚠️  Líder conocido {known_leader_id} no es líder activo según su respuesta")
                    # El líder conocido no es realmente el líder, iniciar votación
                    return None
        except Exception as e:
            print(f"[NAMENODE] ❌ Líder conocido {known_leader_id} no responde: {e}")
            print(f"[NAMENODE] 🗳️  Se iniciará votación porque el líder conocido no está disponible")
            # Actualizar estado del líder en gossip como no disponible
            update_peer_status(known_leader_id, False)
            return None
    
    # SEGUNDO: Si no hay líder conocido, consultar a los peers para descubrir uno
    if not peers:
        print(f"[NAMENODE] 🔍 Verificación de líder: No hay peers conocidos para consultar")
        return None
    
    print(f"[NAMENODE] 🔍 No hay líder conocido localmente, consultando {len(peers)} peers conocidos: {sorted(peers)}")
    
    # Consultar cada peer para ver si hay un líder activo
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.get(f"{peer_url}/", timeout=2)
            if response.status_code == 200:
                data = response.json()
                # Si este peer es el líder, intentar comunicarse directamente con él
                if data.get("is_leader"):
                    leader_id = data.get("leader_id") or data.get("node_id")
                    # Verificar que el líder responde directamente
                    try:
                        leader_url = get_peer_url(leader_id)
                        leader_response = requests.get(f"{leader_url}/", timeout=2)
                        if leader_response.status_code == 200:
                            leader_data = leader_response.json()
                            if leader_data.get("is_leader"):
                                print(f"[NAMENODE] ✅ Líder activo encontrado y verificado: {leader_id}")
                                update_peer_status(leader_id, True)
                                return leader_id
                    except Exception as e:
                        print(f"[NAMENODE] ⚠️  Líder {leader_id} reportado por {peer} pero no responde: {e}")
                        continue
                # Si este peer conoce un líder, intentar comunicarse directamente con él
                elif data.get("leader_id"):
                    leader_id = data.get("leader_id")
                    try:
                        leader_url = get_peer_url(leader_id)
                        leader_response = requests.get(f"{leader_url}/", timeout=2)
                        if leader_response.status_code == 200:
                            leader_data = leader_response.json()
                            if leader_data.get("is_leader"):
                                print(f"[NAMENODE] ✅ Líder conocido encontrado y verificado: {leader_id}")
                                update_peer_status(leader_id, True)
                                return leader_id
                    except Exception as e:
                        print(f"[NAMENODE] ⚠️  Líder {leader_id} reportado por {peer} pero no responde: {e}")
                        continue
        except Exception as e:
            # Continuar con el siguiente peer si este no responde
            print(f"[NAMENODE] ⚠️  No se pudo contactar a peer {peer} para verificar líder: {e}")
            continue
    
    print(f"[NAMENODE] ❌ No se encontró líder activo después de consultar {len(peers)} peers")
    print(f"[NAMENODE] 🗳️  Se iniciará votación porque no hay líder disponible")
    return None


def start_election():
    """Inicia una elección de líder entre los nodos disponibles (sin quorum)"""
    current_time = time.time()
    
    with cluster_lock:
        time_since_last_election = current_time - cluster_state.get("last_election_time", 0)
        if time_since_last_election < 5:
            print(f"[NAMENODE] Elección reciente hace {time_since_last_election:.1f}s, esperando cooldown...")
            return False
        
        candidate_id = cluster_state["node_id"]
        peers = cluster_state["peers"].copy()
        current_leader_id = cluster_state.get("leader_id")
    
    # Siempre intentar descubrir peers via DNS antes de la elección
    print(f"[NAMENODE] 🔍 Descubriendo peers via DNS antes de elección...")
    refresh_peers_from_dns()
    with cluster_lock:
        peers = cluster_state["peers"].copy()
    
    # Si no hay peers, es nodo único - auto-elegirse como líder
    if not peers:
        with cluster_lock:
            cluster_state["term"] += 1
            cluster_state["last_election_time"] = current_time
            cluster_state["is_leader"] = True
            cluster_state["leader_id"] = candidate_id
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[NAMENODE] ✅ Nodo único, automáticamente líder (término {cluster_state['term']})")
        return True
    
    # ANTES de iniciar elección, verificar si hay un líder activo
    # Esto previene que nodos nuevos se conviertan en líder incorrectamente
    print(f"[NAMENODE] 🗳️  Preparando elección. Peers conocidos ({len(peers)}): {sorted(peers)}")
    print(f"[NAMENODE] 🗳️  Estado actual: candidate_id={candidate_id}, current_leader_id={current_leader_id}")
    existing_leader = check_existing_leader(peers)
    if existing_leader:
        print(f"[NAMENODE] ⚠️  Líder activo detectado: {existing_leader}. No se iniciará elección.")
        # Actualizar estado con el líder encontrado
        with cluster_lock:
            cluster_state["leader_id"] = existing_leader
            cluster_state["is_leader"] = False
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[NAMENODE] 📋 Estado actualizado: leader_id={existing_leader}, is_leader=False")
        print(f"[NAMENODE] 💡 El seguidor debería intentar registrarse con el líder en el próximo ciclo de follower_heartbeat_check()")
        return False
    
    # Si hay un líder conocido localmente pero no respondió, verificar una vez más
    if current_leader_id:
        try:
            leader_url = get_peer_url(current_leader_id)
            response = requests.get(f"{leader_url}/", timeout=2)
            if response.status_code == 200:
                data = response.json()
                if data.get("is_leader"):
                    print(f"[NAMENODE] Líder conocido {current_leader_id} sigue activo. No se iniciará elección.")
                    with cluster_lock:
                        cluster_state["last_heartbeat_time"] = time.time()
                    return False
        except Exception:
            # El líder conocido no responde, proceder con elección
            pass
    
    # No hay líder activo, proceder con la elección
    with cluster_lock:
        cluster_state["term"] += 1
        cluster_state["last_election_time"] = current_time
        term = cluster_state["term"]
        cluster_state["is_leader"] = False
        cluster_state["leader_id"] = None
    
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
        
        # Enviar heartbeat para mantener el liderazgo
        with cluster_lock:
            term = cluster_state["term"]
            leader_id = cluster_state["node_id"]
            # Obtener la lista de peers conocidos (descubiertos mediante gossip)
            peers = cluster_state["peers"].copy()
            # Incluir al líder en la lista de peers conocidos
            all_known_peers = peers.copy()
            if leader_id not in all_known_peers:
                all_known_peers.append(leader_id)
        
        # Obtener token de servicio para autenticación
        try:
            service_token = generate_service_token(leader_id, "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Detectar qué seguidores están activos
        active_followers = []
        
        # Actualizar estado del líder como vivo en peer_status (el líder también se monitorea a sí mismo)
        update_peer_status(leader_id, True)
        
        print(f"[NAMENODE] 💓 [HEARTBEAT] Enviando heartbeats desde líder {leader_id}")
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Lista completa de peers que se enviará ({len(all_known_peers)}): {sorted(all_known_peers)}")
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Detalle: peers={sorted(peers)}, leader_id={leader_id} (incluido en lista)")
        
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                response = requests.post(
                    f"{peer_url}/internal/heartbeat",
                    json={
                        "term": term, 
                        "leader_id": leader_id,
                        "peers": all_known_peers,  # Incluir al líder en la lista de peers
                        "active_followers": []  # Se actualizará después
                    },
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=2
                )
                if response.status_code == 200:
                    update_peer_status(peer, True)
                    active_followers.append(peer)
                else:
                    update_peer_status(peer, False)
            except Exception as e:
                update_peer_status(peer, False)
                pass  # Silenciar errores de heartbeat
        
        # Enviar segunda pasada con la lista completa de seguidores activos
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                response = requests.post(
                    f"{peer_url}/internal/heartbeat",
                    json={
                        "term": term, 
                        "leader_id": leader_id,
                        "peers": all_known_peers,  # Incluir al líder en la lista de peers
                        "active_followers": active_followers
                    },
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=2
                )
                if response.status_code == 200:
                    update_peer_status(peer, True)
            except Exception:
                update_peer_status(peer, False)
        
        with cluster_lock:
            cluster_state["last_heartbeat_time"] = time.time()
            cluster_state["active_followers"] = active_followers.copy()
        
        # Log del estado de peers
        with cluster_lock:
            current_peers = cluster_state["peers"].copy()
            current_leader = cluster_state["leader_id"]
            # Construir lista completa incluyendo líder
            complete_peers_list = current_peers.copy()
            if current_leader and current_leader not in complete_peers_list:
                complete_peers_list.append(current_leader)
        
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Estado final de peers conocidos ({len(complete_peers_list)}): {sorted(complete_peers_list)}")
        if active_followers:
            print(f"[NAMENODE] ✅ Seguidores activos ({len(active_followers)}/{len(peers)}): {sorted(active_followers)}")
            inactive = [p for p in peers if p not in active_followers]
            if inactive:
                print(f"[NAMENODE] ⚠️  Seguidores inactivos ({len(inactive)}): {sorted(inactive)}")
        else:
            print(f"[NAMENODE] ⚠️  No hay seguidores activos de {len(peers)} peers conocidos")


# Intervalo de sincronización (segundos)
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "30"))


def leader_sync_loop():
    """
    Loop que sincroniza periódicamente el log de operaciones del líder con los seguidores.
    Esto asegura que los seguidores tengan una copia actualizada de los datos.
    """
    # Esperar a que el cluster se estabilice
    time.sleep(15)
    
    while True:
        time.sleep(SYNC_INTERVAL)
        
        if not is_leader():
            continue
        
        with cluster_lock:
            peers = cluster_state["peers"].copy()
            leader_id = cluster_state["node_id"]
            active_followers = cluster_state.get("active_followers", []).copy()
        
        if not active_followers:
            print(f"[NAMENODE] 🔄 [SYNC] No hay seguidores activos para sincronizar")
            continue
        
        print(f"[NAMENODE] 🔄 [SYNC] Iniciando sincronización periódica con {len(active_followers)} seguidores...")
        
        # Cargar el log de operaciones local
        local_operations = load_operation_log(NODE_ID)
        
        if not local_operations:
            print(f"[NAMENODE] 🔄 [SYNC] No hay operaciones para sincronizar")
            continue
        
        # Obtener token de servicio
        try:
            service_token = generate_service_token(leader_id, "service")
        except Exception as e:
            print(f"[NAMENODE] 🔄 [SYNC] Error generando token: {e}")
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        sync_success = 0
        sync_failed = 0
        
        for follower in active_followers:
            try:
                follower_url = get_peer_url(follower)
                
                # Obtener el log del seguidor para comparar
                try:
                    response = requests.get(
                        f"{follower_url}/internal/operation-log",
                        headers={"Authorization": f"Bearer {service_token}"},
                        timeout=5
                    )
                    if response.status_code == 200:
                        follower_operations = response.json()
                        follower_op_count = len(follower_operations)
                    else:
                        follower_op_count = 0
                        follower_operations = []
                except Exception:
                    follower_op_count = 0
                    follower_operations = []
                
                # Identificar operaciones faltantes en el seguidor
                follower_keys = set()
                for op in follower_operations:
                    key = f"{op.get('operation')}_{op.get('timestamp')}_{op.get('term')}"
                    follower_keys.add(key)
                
                missing_operations = []
                for op in local_operations:
                    key = f"{op.operation}_{op.timestamp}_{op.term}"
                    if key not in follower_keys:
                        missing_operations.append(op)
                
                if not missing_operations:
                    print(f"[NAMENODE] 🔄 [SYNC] ✅ {follower} ya está sincronizado ({follower_op_count} operaciones)")
                    sync_success += 1
                    continue
                
                print(f"[NAMENODE] 🔄 [SYNC] Enviando {len(missing_operations)} operaciones faltantes a {follower}...")
                
                # Enviar operaciones faltantes
                ops_sent = 0
                for operation in missing_operations:
                    try:
                        response = requests.post(
                            f"{follower_url}/internal/replicate",
                            json={
                                "operation": operation.operation,
                                "data": operation.data,
                                "term": operation.term,
                                "timestamp": operation.timestamp
                            },
                            headers={"Authorization": f"Bearer {service_token}"},
                            timeout=3
                        )
                        if response.status_code == 200:
                            ops_sent += 1
                    except Exception as e:
                        print(f"[NAMENODE] 🔄 [SYNC] Error enviando operación a {follower}: {e}")
                
                if ops_sent == len(missing_operations):
                    print(f"[NAMENODE] 🔄 [SYNC] ✅ {follower} sincronizado: {ops_sent} operaciones enviadas")
                    sync_success += 1
                else:
                    print(f"[NAMENODE] 🔄 [SYNC] ⚠️ {follower} parcialmente sincronizado: {ops_sent}/{len(missing_operations)} operaciones")
                    sync_failed += 1
                    
            except Exception as e:
                print(f"[NAMENODE] 🔄 [SYNC] ❌ Error sincronizando con {follower}: {e}")
                sync_failed += 1
        
        print(f"[NAMENODE] 🔄 [SYNC] Sincronización completada: {sync_success} exitosos, {sync_failed} fallidos")


def follower_heartbeat_check():
    """Verifica si el líder sigue activo (para seguidores)"""
    while True:
        # Verificar más frecuentemente (cada 5 segundos en lugar de ELECTION_TIMEOUT)
        time.sleep(5)
        
        if is_leader():
            continue
        
        with cluster_lock:
            time_since_heartbeat = time.time() - cluster_state["last_heartbeat_time"]
            leader_id = cluster_state["leader_id"]
            # Verificar también el estado del líder en gossip
            leader_status = None
            if leader_id and leader_id in cluster_state.get("peer_status", {}):
                leader_status = cluster_state["peer_status"][leader_id].get("status")
        
        # Verificar si el líder está marcado como "dead" o "suspected" en gossip
        if leader_id and leader_status in ["dead", "suspected"]:
            print(f"[NAMENODE] ⚠️  Líder {leader_id} detectado como {leader_status} mediante gossip, iniciando elección...")
            with cluster_lock:
                # Marcar líder como dead para excluirlo de la votación
                if leader_id in cluster_state.get("peer_status", {}):
                    cluster_state["peer_status"][leader_id]["status"] = "dead"
                cluster_state["leader_id"] = None
                cluster_state["last_heartbeat_time"] = 0  # Resetear para forzar elección
            start_election()
            continue
        
        if leader_id:
            # Si han pasado más de ELECTION_TIMEOUT sin heartbeat, verificar conectividad
            if time_since_heartbeat > ELECTION_TIMEOUT:
                print(f"[NAMENODE] ⏱️  Sin heartbeat del líder {leader_id} por {time_since_heartbeat:.1f}s, verificando conectividad...")
                try:
                    leader_url = get_peer_url(leader_id)
                    response = requests.get(f"{leader_url}/", timeout=3)
                    if response.status_code == 200:
                        # El líder está vivo, actualizar timestamp
                        with cluster_lock:
                            cluster_state["last_heartbeat_time"] = time.time()
                            # Actualizar estado del líder en gossip
                            if leader_id in cluster_state.get("peer_status", {}):
                                cluster_state["peer_status"][leader_id]["status"] = "alive"
                                cluster_state["peer_status"][leader_id]["last_seen"] = time.time()
                        print(f"[NAMENODE] ✅ Líder {leader_id} responde correctamente")
                        continue
                    else:
                        print(f"[NAMENODE] ❌ Líder {leader_id} no responde correctamente (HTTP {response.status_code}), iniciando elección...")
                        with cluster_lock:
                            # Marcar líder como dead
                            if leader_id in cluster_state.get("peer_status", {}):
                                cluster_state["peer_status"][leader_id]["status"] = "dead"
                            cluster_state["leader_id"] = None
                            cluster_state["last_heartbeat_time"] = 0
                        start_election()
                except Exception as e:
                    # Si el timeout es mayor, definitivamente el líder está desconectado
                    if time_since_heartbeat > (ELECTION_TIMEOUT * 1.5):
                        print(f"[NAMENODE] ❌ Líder {leader_id} inaccesible ({time_since_heartbeat:.1f}s sin contacto): {e}")
                        print(f"[NAMENODE] 🗳️  Iniciando elección...")
                        with cluster_lock:
                            # Marcar líder como dead para excluirlo de la votación
                            if leader_id in cluster_state.get("peer_status", {}):
                                cluster_state["peer_status"][leader_id]["status"] = "dead"
                            cluster_state["leader_id"] = None
                            cluster_state["last_heartbeat_time"] = 0
                        start_election()
                    else:
                        # Aún no ha pasado suficiente tiempo, solo marcar como suspected
                        print(f"[NAMENODE] ⚠️  Líder {leader_id} no responde temporalmente ({time_since_heartbeat:.1f}s), marcando como suspected...")
                        if leader_id in cluster_state.get("peer_status", {}):
                            cluster_state["peer_status"][leader_id]["status"] = "suspected"
        elif time_since_heartbeat > ELECTION_TIMEOUT:
            print(f"[NAMENODE] 🗳️  No hay líder conocido después de {time_since_heartbeat:.1f}s, intentando elección...")
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
    with cluster_lock:
        node_id = cluster_state['node_id']
        peers = cluster_state['peers'].copy()
        active_followers = cluster_state.get('active_followers', []).copy()
    
    print(f"[NAMENODE] 🚀 Nodo iniciado: {node_id}")
    print(f"[NAMENODE] ⏱️  [CONFIG] Tiempos de reconciliación configurados:")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - GOSSIP_INTERVAL: {GOSSIP_INTERVAL}s (intervalo entre rondas de gossip)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - PEER_FAILURE_TIMEOUT: 30s (tiempo para marcar como suspected)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - PEER_DEAD_TIMEOUT: 60s (tiempo para marcar como dead)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - Tiempo mínimo para detectar reunificación: ~5s (próximo gossip)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - Tiempo para marcar peer como dead: 60s sin contacto")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - Tiempo esperado para reconciliación completa: 10-30s (depende de operaciones)")
    
    with cluster_lock:
        leader_id_startup = cluster_state.get("leader_id")
        # Construir lista completa incluyendo líder
        complete_peers_startup = peers.copy()
        if leader_id_startup and leader_id_startup not in complete_peers_startup:
            complete_peers_startup.append(leader_id_startup)
    
    print(f"[NAMENODE] 📋 [STARTUP] Estado inicial del clúster:")
    print(f"[NAMENODE] 📋 [STARTUP] Node ID: {node_id}")
    print(f"[NAMENODE] 📋 [STARTUP] Peers conocidos (sin líder) ({len(peers)}): {sorted(peers) if peers else '[]'}")
    print(f"[NAMENODE] 📋 [STARTUP] Lista completa de peers conocidos ({len(complete_peers_startup)}): {sorted(complete_peers_startup)}")
    print(f"[NAMENODE] 📋 [STARTUP] Líder conocido: {leader_id_startup}")
    if active_followers:
        print(f"[NAMENODE] 👥 Seguidores activos conocidos ({len(active_followers)}): {sorted(active_followers)}")
    
    # Inicializar base de datos
    # Inicializar base de datos (incluye tabla de usuarios)
    init_db(node_id=cluster_state["node_id"])
    
    # Asegurar que el usuario admin existe
    from security.auth import init_users_db
    init_users_db(node_id=cluster_state["node_id"])
    
    # Fase 2: Cargar log de operaciones persistente al iniciar
    print(f"[NAMENODE] Cargando log de operaciones persistente...")
    loaded_operations = load_operation_log(cluster_state["node_id"])
    with log_lock:
        operation_log.extend(loaded_operations)
    print(f"[NAMENODE] Cargadas {len(loaded_operations)} operaciones del log persistente")
    
    # Verificar si este nodo necesita reconciliación al iniciar
    # Esperar un poco para que los peers estén disponibles
    def check_reconciliation_on_startup():
        """Verifica si este nodo necesita reconciliación al iniciar"""
        time.sleep(10)  # Esperar a que los peers estén disponibles
        
        with cluster_lock:
            current_term = cluster_state["term"]
            current_node_id = cluster_state["node_id"]
            peers = cluster_state["peers"].copy()
        
        if not peers:
            print(f"[NAMENODE] [STARTUP] No hay peers conocidos, saltando verificación de reconciliación")
            return
        
        print(f"[NAMENODE] [STARTUP] Verificando si este nodo necesita reconciliación...")
        print(f"[NAMENODE] [STARTUP] Term local: {current_term}")
        
        # Consultar a los peers para ver si tienen términos más altos o más operaciones
        peers_to_reconcile = []
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                response = requests.get(f"{peer_url}/", timeout=3)
                if response.status_code == 200:
                    peer_data = response.json()
                    peer_term = peer_data.get("term", 0)
                    peer_total_files = peer_data.get("total_files", 0)
                    
                    # Obtener log del peer para comparar
                    peer_log = get_peer_operation_log(peer)
                    peer_log_count = len(peer_log) if peer_log else 0
                    
                    local_log = load_operation_log(NODE_ID)
                    local_log_count = len(local_log)
                    
                    print(f"[NAMENODE] [STARTUP] Peer {peer}: term={peer_term}, files={peer_total_files}, log_ops={peer_log_count}")
                    print(f"[NAMENODE] [STARTUP] Local: term={current_term}, log_ops={local_log_count}")
                    
                    # Si el peer tiene term mayor o más operaciones, necesitamos reconciliación
                    if peer_term > current_term:
                        print(f"[NAMENODE] [STARTUP] ⚠️  Peer {peer} tiene term mayor ({peer_term} > {current_term}), necesitamos reconciliación")
                        peers_to_reconcile.append(peer)
                    elif peer_log_count > local_log_count:
                        print(f"[NAMENODE] [STARTUP] ⚠️  Peer {peer} tiene más operaciones ({peer_log_count} > {local_log_count}), necesitamos reconciliación")
                        peers_to_reconcile.append(peer)
                    else:
                        print(f"[NAMENODE] [STARTUP] ✅ Peer {peer} está sincronizado o más atrás")
            except Exception as e:
                print(f"[NAMENODE] [STARTUP] ⚠️  Error verificando peer {peer}: {e}")
        
        if peers_to_reconcile:
            print(f"[NAMENODE] [STARTUP] 🔄 Este nodo necesita reconciliación con: {peers_to_reconcile}")
            print(f"[NAMENODE] [STARTUP] 🔄 Iniciando reconciliación desde startup...")
            trigger_reconciliation(peers_to_reconcile)
        else:
            print(f"[NAMENODE] [STARTUP] ✅ Este nodo está sincronizado, no se necesita reconciliación")
    
    # Iniciar verificación de reconciliación en un hilo separado
    reconciliation_check_thread = threading.Thread(target=check_reconciliation_on_startup, daemon=True)
    reconciliation_check_thread.start()
    
    # Iniciar hilos
    heartbeat_thread = threading.Thread(target=leader_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    
    follower_thread = threading.Thread(target=follower_heartbeat_check, daemon=True)
    follower_thread.start()
    
    election_thread = threading.Thread(target=election_retry_loop, daemon=True)
    election_thread.start()
    
    # Iniciar loop de gossip para descubrimiento de peers
    gossip_thread = threading.Thread(target=gossip_loop, daemon=True)
    gossip_thread.start()
    print(f"[NAMENODE] [GOSSIP] Loop de gossip iniciado (interval={GOSSIP_INTERVAL}s, fanout={GOSSIP_FANOUT})")
    
    # Iniciar loop de sincronización periódica (líder -> seguidores)
    sync_thread = threading.Thread(target=leader_sync_loop, daemon=True)
    sync_thread.start()
    print(f"[NAMENODE] [SYNC] Loop de sincronización iniciado (interval={SYNC_INTERVAL}s)")
    
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
                        # Las réplicas ya fueron eliminadas en detect_inactive_datanodes()
                        # Solo necesitamos re-replicar archivos que quedaron undereplicated
                        from namenode.datanode_manager import trigger_rereplication_for_undereplicated
                        trigger_rereplication_for_undereplicated(node_id_db=NODE_ID)
                    
                    # Limpiar archivos con más de 3 réplicas activas (por si acaso)
                    from namenode.datanode_manager import cleanup_overreplicated_files
                    cleanup_overreplicated_files(node_id_db=NODE_ID)
                            
                except Exception as e:
                    print(f"[NAMENODE] Error en monitoreo de DataNodes: {e}")
                    import traceback
                    traceback.print_exc()
    
    datanode_monitor_thread = threading.Thread(target=datanode_monitor_loop, daemon=True)
    datanode_monitor_thread.start()
    
    # Intentar elección inicial después de un delay breve
    print(f"[NAMENODE] ⏳ Esperando 5 segundos antes de la elección inicial...")
    time.sleep(5)
    
    start_election()
    
    yield
    
    # Shutdown
    print(f"[NAMENODE] Nodo deteniéndose...")


app = FastAPI(title="TBFS MetaNameNode (Distributed)", lifespan=lifespan)


@app.get("/")
def root():
    """Endpoint de estado del MetaNameNode (público, sin autenticación)"""
    with cluster_lock:
        cluster_data = {
            "node_id": cluster_state["node_id"],
            "is_leader": cluster_state["is_leader"],
            "leader_id": cluster_state["leader_id"],
            "term": cluster_state["term"]
        }
        leader_id = cluster_state["leader_id"]
        is_leader_flag = cluster_state["is_leader"]
        peers = cluster_state["peers"].copy()
        active_followers = cluster_state.get("active_followers", []).copy()
    
    # Construir lista completa de peers conocidos (incluyendo líder)
    complete_peers_list = peers.copy()
    if leader_id and leader_id not in complete_peers_list:
        complete_peers_list.append(leader_id)
    
    # Log del estado de peers cuando se consulta el endpoint
    print(f"[NAMENODE] 📋 [ENDPOINT /] Estado actual del clúster:")
    print(f"[NAMENODE] 📋 [ENDPOINT /] Node ID: {cluster_data['node_id']}, Is Leader: {is_leader_flag}, Leader ID: {leader_id}")
    print(f"[NAMENODE] 📋 [ENDPOINT /] Peers conocidos (sin líder) ({len(peers)}): {sorted(peers) if peers else '[]'}")
    print(f"[NAMENODE] 📋 [ENDPOINT /] Lista completa de peers conocidos ({len(complete_peers_list)}): {sorted(complete_peers_list)}")
    print(f"[NAMENODE] 📋 [ENDPOINT /] Líder incluido en lista completa: {leader_id in complete_peers_list if leader_id else 'N/A'}")
    
    if active_followers:
        print(f"[NAMENODE] 👥 Estado actual - Seguidores activos ({len(active_followers)}): {sorted(active_followers)}")
    
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
        # Verificar si node_id ya tiene el prefijo "tbfs-"
        node_id = cluster_state['node_id']
        if not node_id.startswith("tbfs-"):
            node_service_name = f"tbfs-{node_id}"
        else:
            node_service_name = node_id
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


# ========== ENDPOINTS DE AUTENTICACIÓN ==========

@app.post("/auth/login", response_model=TokenResponse)
def login(credentials: UserLogin):
    """Endpoint de login para obtener token JWT - verifica usuario y contraseña en la base de datos"""
    # Redirigir al líder si no somos el líder
    leader_url = get_leader_url()
    if leader_url:
        print(f"[NAMENODE] Redirigiendo login a líder: {leader_url}")
        try:
            response = requests.post(
                f"{leader_url}/auth/login",
                json=credentials.dict(),
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[NAMENODE] Error redirigiendo login a líder: {e}")
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        print(f"[NAMENODE] Este nodo no es líder, pero no hay líder disponible")
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Procesar login en el líder
    user = authenticate_user(credentials.username, credentials.password, node_id=_get_node_id())
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Credenciales inválidas"
        )
    
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role.value}
    )
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=3600,  # 1 hora
        user={
            "username": user.username,
            "role": user.role.value,
            "is_active": user.is_active
        }
    )


@app.post("/auth/register")
def register(user_data: UserCreate, current_user: User = Depends(require_role([Role.ADMIN]))):
    """Endpoint para registrar nuevos usuarios (solo admin) - crea usuario en la base de datos del namenode"""
    # Solo el líder puede crear usuarios (para evitar inconsistencias)
    leader_url = get_leader_url()
    if leader_url:
        try:
            headers = {}
            # Redirigir al líder con el token de autenticación del admin
            response = requests.post(
                f"{leader_url}/auth/register",
                json=user_data.dict(),
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    try:
        new_user = create_user(user_data, node_id=_get_node_id())
        
        # Replicar creación de usuario a otros namenodes
        operation = OperationLog(
            operation="create_user",
            data={
                "username": new_user.username,
                "password_hash": new_user.password_hash,
                "role": new_user.role.value,
                "is_active": new_user.is_active
            },
            term=cluster_state["term"],
            timestamp=time.time()
        )
        
        with log_lock:
            operation_log.append(operation)
        
        # Guardar en log persistente (Fase 2)
        save_operation_to_log(operation, NODE_ID)
        
        replicate_to_peers(operation)
        
        return {
            "success": True,
            "message": f"Usuario {new_user.username} creado exitosamente",
            "user": {
                "username": new_user.username,
                "role": new_user.role.value
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/signup", response_model=TokenResponse)
def signup(user_data: UserSignup):
    """
    Registro de usuario sin autenticación.
    - Fuerza rol USER.
    - Rechaza si el usuario ya existe.
    - Devuelve token JWT para inicio de sesión inmediato.
    """
    # Redirigir al líder si no somos el líder
    leader_url = get_leader_url()
    if leader_url:
        print(f"[NAMENODE] Redirigiendo signup a líder: {leader_url}")
        try:
            response = requests.post(
                f"{leader_url}/auth/signup",
                json=user_data.dict(),
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[NAMENODE] Error redirigiendo signup a líder: {e}")
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        print(f"[NAMENODE] Este nodo no es líder, pero no hay líder disponible")
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Procesar en el líder
    try:
        # Forzar rol USER
        user_create = UserCreate(username=user_data.username, password=user_data.password, role=Role.USER)
        new_user = create_user(user_create, node_id=_get_node_id())
        
        # Replicar creación de usuario a otros namenodes
        operation = OperationLog(
            operation="create_user",
            data={
                "username": new_user.username,
                "password_hash": new_user.password_hash,
                "role": new_user.role.value,
                "is_active": new_user.is_active
            },
            term=cluster_state["term"],
            timestamp=time.time()
        )
        
        with log_lock:
            operation_log.append(operation)
        
        # Guardar en log persistente (Fase 2)
        save_operation_to_log(operation, NODE_ID)
        
        replicate_to_peers(operation)
        
        # Emitir token
        access_token = create_access_token(
            data={"sub": new_user.username, "role": new_user.role.value}
        )
        return TokenResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=3600,
            user={
                "username": new_user.username,
                "role": new_user.role.value,
                "is_active": new_user.is_active
            }
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/auth/me")
def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Obtiene información del usuario actual"""
    return {
        "username": current_user.username,
        "role": current_user.role.value,
        "is_active": current_user.is_active,
        "created_at": current_user.created_at
    }


@app.post("/auth/change-password")
def change_user_password(
    password_data: PasswordChange,
    current_user: User = Depends(get_current_user),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Cambia la contraseña del usuario actual.
    Requiere autenticación y verifica la contraseña antigua.
    Si este nodo es el líder, replica el cambio a otros namenodes.
    """
    node_id = _get_node_id()
    
    # Solo el líder puede cambiar contraseñas (para evitar inconsistencias)
    leader_url = get_leader_url()
    if leader_url:
        # Redirigir al líder con el token de autenticación
        try:
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            response = requests.post(
                f"{leader_url}/auth/change-password",
                json=password_data.dict(),
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Cambiar contraseña en el líder
    success = change_password(
        username=current_user.username,
        old_password=password_data.old_password,
        new_password=password_data.new_password,
        node_id=node_id
    )
    
    if not success:
        raise HTTPException(
            status_code=400,
            detail="La contraseña antigua es incorrecta"
        )
    
    # Obtener el nuevo hash de contraseña después del cambio
    updated_user = get_user(current_user.username, node_id)
    if not updated_user:
        raise HTTPException(status_code=500, detail="Error al verificar el cambio de contraseña")
    
    # Replicar cambio de contraseña a otros namenodes
    operation = OperationLog(
        operation="change_password",
        data={
            "username": current_user.username,
            "new_password_hash": updated_user.password_hash
        },
        term=cluster_state["term"],
        timestamp=time.time()
    )
    
    with log_lock:
        operation_log.append(operation)
    
    # Guardar en log persistente (Fase 2)
    save_operation_to_log(operation, NODE_ID)
    
    replicate_to_peers(operation)
    
    return {
        "success": True,
        "message": "Contraseña cambiada exitosamente"
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
def register_datanode_endpoint(
    registration: DataNodeRegistration,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Registra un nuevo DataNode o actualiza uno existente.
    Requiere token de servicio para autenticación.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    if not validate_service_request(registration.node_id, token):
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
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
def datanode_heartbeat_endpoint(
    node_id: str,
    heartbeat: DataNodeHeartbeat,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Recibe un heartbeat de un DataNode.
    Requiere token de servicio para autenticación.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    
    # Logging para diagnóstico
    from security.service_auth import verify_service_token
    payload = verify_service_token(token)
    if payload:
        token_service_id = payload.get("service_id") or payload.get("sub")
        print(f"[NAMENODE] Heartbeat auth: node_id={node_id}, token_service_id={token_service_id}, match={token_service_id == node_id}")
    else:
        print(f"[NAMENODE] Heartbeat auth: node_id={node_id}, token JWT inválido, intentando token pre-compartido")
    
    if not validate_service_request(node_id, token):
        print(f"[NAMENODE] ❌ Heartbeat rechazado: node_id={node_id}, token no válido")
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    print(f"[NAMENODE] ✓ Heartbeat autenticado correctamente: node_id={node_id}")
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
def drain_datanode_endpoint(
    node_id: str,
    current_user: User = Depends(require_permission(Permission.MANAGE_DATANODES))
):
    """
    Inicia el drenaje de un DataNode: re-replica todos sus archivos y evita nuevas asignaciones.
    Requiere permiso de administración de DataNodes.
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
def undrain_datanode_endpoint(
    node_id: str,
    current_user: User = Depends(require_permission(Permission.MANAGE_DATANODES))
):
    """
    Desmarca un DataNode del proceso de drenaje, permitiendo nuevas asignaciones.
    Requiere permiso de administración de DataNodes.
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


@app.post("/add")
async def add_file_compat(
    file: UploadFile,
    tags: str = Form(...),
    current_user: User = Depends(require_permission(Permission.WRITE_FILES))
):
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
    
    # Agregar metadatos primero (asociado al usuario actual)
    # Obtener term actual para versionado
    with cluster_lock:
        current_term = cluster_state["term"]
    
    file_id = add_file_metadata(
        name=file.filename,
        tags=tag_list,
        size=file_size,
        hash_value=hash_value,
        node_id=NODE_ID,
        user_id=current_user.username,
        term=current_term
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
    
    # Determinar número mínimo de réplicas requeridas
    # Idealmente 2, pero aceptar 1 si solo hay 1 DataNode disponible
    min_required_replicas = min(2, len(datanode_ids)) if datanode_ids else 1
    
    for attempt in range(max_attempts):
        if success_count >= min_required_replicas:
            # Ya tenemos suficientes réplicas, salir
            break
        
        if attempt > 0:
            print(f"[NAMENODE] Reintento {attempt + 1}/{max_attempts} para almacenar archivo...")
            # Reasignar réplicas excluyendo los DataNodes que ya fallaron
            remaining_datanodes = [dn_id for dn_id in final_datanode_ids if dn_id not in failed_datanodes]
            if len(remaining_datanodes) < len(datanode_ids):
                # Necesitamos más DataNodes, reasignar completamente
                new_datanode_ids = assign_replicas(file_hash, file_size, node_id_db=NODE_ID, exclude_datanodes=failed_datanodes)
                if new_datanode_ids:
                    # Excluir los que ya fallaron
                    available_datanodes = [dn_id for dn_id in new_datanode_ids if dn_id not in failed_datanodes]
                    if len(available_datanodes) >= 1:
                        final_datanode_ids = available_datanodes[:3] if len(available_datanodes) >= 3 else available_datanodes
                        # Actualizar mínimo requerido basado en los disponibles
                        min_required_replicas = min(2, len(final_datanode_ids))
                    else:
                        final_datanode_ids = new_datanode_ids
                        min_required_replicas = min(2, len(final_datanode_ids))
                else:
                    print(f"[NAMENODE] No hay más DataNodes disponibles para reasignar")
                    break
            else:
                # Usar los DataNodes restantes
                final_datanode_ids = remaining_datanodes[:3] if len(remaining_datanodes) >= 3 else remaining_datanodes
                min_required_replicas = min(2, len(final_datanode_ids))
        
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
                # Obtener token de servicio para autenticación con DataNode
                node_id = cluster_state["node_id"]
                try:
                    service_token = generate_service_token(node_id, "service")
                    print(f"[NAMENODE] Token generado para node_id={node_id}")
                except Exception as token_error:
                    print(f"[NAMENODE] Error generando token para node_id={node_id}: {token_error}, usando token pre-compartido")
                    service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                
                files = {"file": (file.filename, file_content)}
                data = {"file_id": file_hash}
                
                print(f"[NAMENODE] Enviando archivo a {dn_id} ({dn_url}/store) con node_id={node_id}")
                response = requests.post(
                    f"{dn_url}/store",
                    files=files,
                    data=data,
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=30
                )
                response.raise_for_status()
                print(f"[NAMENODE] ✓ Archivo almacenado exitosamente en {dn_id} ({dn_url})")
                success_count += 1
                successful_datanodes.append(dn_id)
            except requests.HTTPError as e:
                if e.response.status_code == 403:
                    print(f"[NAMENODE] ❌ Error 403 Forbidden almacenando en {dn_id} ({dn_url}): {e}")
                    print(f"[NAMENODE] Detalles de respuesta: {e.response.text if hasattr(e, 'response') else 'N/A'}")
                else:
                    print(f"[NAMENODE] Error HTTP {e.response.status_code} almacenando en {dn_id} ({dn_url}): {e}")
                if dn_id not in failed_datanodes:
                    failed_datanodes.append(dn_id)
            except Exception as e:
                print(f"[NAMENODE] Error almacenando en {dn_id} ({dn_url}): {e}")
                if dn_id not in failed_datanodes:
                    failed_datanodes.append(dn_id)
                if dn_id not in failed_datanodes:
                    failed_datanodes.append(dn_id)
    
    # Verificar que se guardó al menos 1 réplica (o 2 si hay múltiples DataNodes disponibles)
    # Si solo hay 1 DataNode disponible, aceptar 1 réplica; si hay más, preferir al menos 2
    expected_replicas = len(datanode_ids) if datanode_ids else 1
    min_acceptable = min_required_replicas
    
    if success_count < min_acceptable:
        print(f"[NAMENODE] Error: Solo {success_count}/{expected_replicas} réplicas se guardaron después de {max_attempts} intentos (mínimo requerido: {min_acceptable}). Eliminando metadatos...")
        # Intentar eliminar de los DataNodes que sí recibieron el archivo
        for dn_id in successful_datanodes:
            dn_info = get_datanode(dn_id, node_id_db=NODE_ID)
            if dn_info:
                url = dn_info["url"]
                if not url.startswith("http"):
                    url = f"http://{url}:{dn_info['port']}"
                try:
                    # Obtener token de servicio para autenticación con DataNode
                    try:
                        service_token = generate_service_token(cluster_state["node_id"], "service")
                    except Exception:
                        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                    
                    requests.delete(
                        f"{url}/delete/{file_hash}",
                        headers={"Authorization": f"Bearer {service_token}"},
                        timeout=10
                    )
                except:
                    pass
        delete_file_metadata(file_id, node_id=NODE_ID)
        raise HTTPException(
            status_code=507,
            detail=f"No se pudo almacenar el archivo en suficientes DataNodes después de {max_attempts} intentos ({success_count}/{expected_replicas}, mínimo requerido: {min_acceptable})"
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
            "datanode_ids": datanode_ids,
            "user_id": current_user.username  # Incluir user_id en replicación
        },
        term=cluster_state["term"],
        timestamp=time.time()
    )
    
    with log_lock:
        operation_log.append(operation)
    
    # Guardar en log persistente (Fase 2)
    save_operation_to_log(operation, NODE_ID)
    
    replicate_to_peers(operation)
    
    print(f"[NAMENODE] Operación replicada a peers")
    
    return {
        "success": True,
        "message": f"Archivo '{file.filename}' agregado correctamente",
        "file_id": file_id,
        "replicas": datanode_ids,
        "replicas_stored": success_count
    }


# ========== CHUNKED UPLOAD ENDPOINTS ==========

@app.post("/upload/init")
def init_chunked_upload(
    filename: str = Form(...),
    file_hash: str = Form(...),
    file_size: int = Form(...),
    tags: str = Form(...),
    chunk_size: int = Form(...),
    current_user: User = Depends(require_permission(Permission.WRITE_FILES))
):
    """
    Inicia un upload por chunks.
    Crea metadatos del archivo, asigna un DataNode y devuelve token para subir directamente.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/upload/init",
                data={
                    "filename": filename,
                    "file_hash": file_hash,
                    "file_size": file_size,
                    "tags": tags,
                    "chunk_size": chunk_size
                },
                headers={"Authorization": f"Bearer {current_user.username}"},
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Iniciando upload: {filename} ({file_size} bytes)")
    
    # Parsear file_hash (puede venir con o sin prefijo "sha256:")
    if file_hash.startswith("sha256:"):
        file_hash_clean = file_hash[7:]
    else:
        file_hash_clean = file_hash
    hash_value = f"sha256:{file_hash_clean}"
    
    # Parsear tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if not tag_list:
        tag_list = ["general"]
    
    # Calcular total de chunks
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    
    # Asignar DataNode primario para el upload
    datanode_ids = assign_replicas(file_hash_clean, file_size, node_id_db=NODE_ID)
    if not datanode_ids or len(datanode_ids) == 0:
        raise HTTPException(status_code=507, detail="No hay DataNodes disponibles")
    
    primary_datanode_id = datanode_ids[0]
    primary_datanode = get_datanode(primary_datanode_id, node_id_db=NODE_ID)
    if not primary_datanode:
        raise HTTPException(status_code=507, detail=f"DataNode {primary_datanode_id} no encontrado")
    
    # Construir URL del DataNode
    datanode_url = primary_datanode["url"]
    if not datanode_url.startswith("http"):
        datanode_url = f"http://{datanode_url}:{primary_datanode['port']}"
    
    # Generar token temporal para el cliente (válido por 15 minutos)
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Generando token con file_hash_clean={file_hash_clean[:32]}... (longitud: {len(file_hash_clean)})")
    client_token = generate_client_upload_token(
        user_id=current_user.username,
        file_hash=file_hash_clean,
        datanode_id=primary_datanode_id,
        expires_minutes=15
    )
    
    # NO crear metadatos aquí - se crearán SOLO después de verificar que el archivo existe en el DataNode
    # Verificar si ya existe un upload en progreso para el mismo hash y usuario
    existing_upload_id = None
    existing_session_id = None
    received_chunks = []
    missing_chunks = []
    
    with uploads_lock:
        # Buscar upload existente para el mismo hash y usuario
        for uid, upload_info in active_uploads.items():
            if (upload_info.get("file_hash") == file_hash_clean and 
                upload_info.get("user_id") == current_user.username):
                existing_upload_id = uid
                print(f"[NAMENODE] [CHUNKED_UPLOAD] Upload previo encontrado: upload_id={existing_upload_id}")
                break
    
    # Si hay un upload previo, consultar progreso en el DataNode
    # Nota: Generamos el token primero para poder consultar el progreso
    if existing_upload_id:
        try:
            # Consultar progreso en DataNode usando el token del cliente
            progress_response = requests.get(
                f"{datanode_url}/client/upload/progress/{file_hash_clean}",
                headers={"Authorization": f"Bearer {client_token}"},
                timeout=10
            )
            
            if progress_response.status_code == 200:
                progress_data = progress_response.json()
                existing_session_id = progress_data.get("session_id")
                received_chunks = progress_data.get("received_chunks", [])
                missing_chunks = progress_data.get("missing_chunks", [])
                print(f"[NAMENODE] [CHUNKED_UPLOAD] Progreso encontrado: {len(received_chunks)}/{total_chunks} chunks recibidos")
            else:
                print(f"[NAMENODE] [CHUNKED_UPLOAD] No se pudo consultar progreso, creando nueva sesión")
                existing_upload_id = None
        except Exception as e:
            print(f"[NAMENODE] [CHUNKED_UPLOAD] Error consultando progreso: {e}, creando nueva sesión")
            existing_upload_id = None
    
    # Generar upload_id (reutilizar si hay upload previo, o crear nuevo)
    if existing_upload_id:
        upload_id = existing_upload_id
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Reanudando upload existente: upload_id={upload_id}")
    else:
        upload_id = str(uuid.uuid4())
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Creando nuevo upload: upload_id={upload_id}")
    
    # Almacenar información del upload en progreso (SIN file_id aún - se creará al finalizar)
    with uploads_lock:
        active_uploads[upload_id] = {
            "upload_id": upload_id,
            "file_id": None,  # Se creará SOLO después de verificar que el archivo existe en DataNode
            "filename": filename,
            "file_hash": file_hash_clean,
            "hash_value": hash_value,  # Guardar hash con prefijo para crear metadatos
            "file_size": file_size,
            "tags": tag_list,
            "user_id": current_user.username,
            "datanode_id": primary_datanode_id,
            "datanode_url": datanode_url,
            "datanode_ids": datanode_ids,  # Para replicación después
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "started_at": time.time(),
            "session_id": existing_session_id  # Guardar session_id si se reanudó
        }
    
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Upload iniciado: upload_id={upload_id}, datanode={primary_datanode_id} (metadatos NO creados aún)")
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Devolviendo file_hash={file_hash_clean[:32]}... (longitud: {len(file_hash_clean)})")
    
    response_data = {
        "upload_id": upload_id,
        "file_id": None,  # No hay file_id aún - se creará al finalizar
        "file_hash": file_hash_clean,  # Hash del archivo (sin prefijo) para usar en DataNode
        "datanode_url": datanode_url,
        "datanode_id": primary_datanode_id,
        "client_token": client_token,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "resumed": existing_upload_id is not None,
        "received_chunks": received_chunks,
        "missing_chunks": missing_chunks
    }
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Respuesta completa: {list(response_data.keys())}")
    return response_data


@app.post("/upload/{upload_id}/finalize")
def finalize_chunked_upload(
    upload_id: str,
    current_user: User = Depends(require_permission(Permission.WRITE_FILES))
):
    """
    Finaliza un upload por chunks.
    Verifica que el archivo esté completo en el DataNode y completa los metadatos.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/upload/{upload_id}/finalize",
                headers={"Authorization": f"Bearer {current_user.username}"},
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Obtener información del upload
    with uploads_lock:
        upload_info = active_uploads.get(upload_id)
        if not upload_info:
            raise HTTPException(status_code=404, detail="Upload no encontrado")
        
        # Verificar que el usuario es el propietario
        if upload_info["user_id"] != current_user.username:
            raise HTTPException(status_code=403, detail="No tienes permiso para finalizar este upload")
        
        file_id = upload_info.get("file_id")  # Puede ser None si aún no se han creado metadatos
        datanode_id = upload_info["datanode_id"]
        datanode_url = upload_info["datanode_url"]
        datanode_ids = upload_info["datanode_ids"]
        file_hash = upload_info["file_hash"]
        hash_value = upload_info.get("hash_value", f"sha256:{file_hash}")  # Hash con prefijo para metadatos
        filename = upload_info["filename"]
        tag_list = upload_info["tags"]
        file_size = upload_info["file_size"]
    
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Finalizando upload: upload_id={upload_id}")
    
    # ========== VERIFICAR QUE EL ARCHIVO EXISTE EN EL DATANODE ==========
    # Verificar ESTRICTAMENTE que el archivo está completo en el DataNode antes de crear metadatos
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Verificando que el archivo existe en DataNode {datanode_id}...")
    
    try:
        # Obtener token de servicio para verificar en DataNode
        with cluster_lock:
            node_id = cluster_state["node_id"]
        try:
            service_token = generate_service_token(node_id, "service")
        except Exception as token_error:
            print(f"[NAMENODE] [CHUNKED_UPLOAD] Error generando token: {token_error}, usando token pre-compartido")
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Verificar que el archivo existe haciendo una petición HEAD o GET pequeño
        # Usamos /retrieve con Range para solo obtener los primeros bytes (más eficiente)
        verify_response = requests.get(
            f"{datanode_url}/retrieve/{file_hash}",
            headers={"Authorization": f"Bearer {service_token}", "Range": "bytes=0-0"},
            timeout=10
        )
        
        if verify_response.status_code == 404:
            raise HTTPException(
                status_code=400,
                detail=f"El archivo no existe en el DataNode {datanode_id}. La subida no se completó correctamente."
            )
        elif verify_response.status_code not in [200, 206]:
            raise HTTPException(
                status_code=503,
                detail=f"Error verificando archivo en DataNode {datanode_id}: HTTP {verify_response.status_code}"
            )
        
        print(f"[NAMENODE] [CHUNKED_UPLOAD] ✅ Archivo verificado en DataNode {datanode_id}")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMENODE] [CHUNKED_UPLOAD] ❌ Error verificando archivo en DataNode: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Error verificando que el archivo existe en el DataNode: {str(e)}"
        )
    
    # ========== CREAR METADATOS SOLO AHORA ==========
    # Ahora que hemos verificado que el archivo existe, crear los metadatos
    # Si file_id ya existe (reanudación), verificamos que pertenece a este usuario
    # Si no existe, lo creamos
    if file_id:
        # Verificar que el file_id existe y pertenece al usuario
        from namenode.manager import get_file_by_id
        existing_file = get_file_by_id(file_id, node_id=NODE_ID, user_id=current_user.username)
        if not existing_file:
            # file_id existe pero no pertenece a este usuario o no existe - crear nuevo
            print(f"[NAMENODE] [CHUNKED_UPLOAD] file_id {file_id} no válido o no pertenece al usuario, creando nuevo...")
            file_id = None
    
    if not file_id:
        # Crear metadatos SOLO ahora que verificamos que el archivo existe en DataNode
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Creando metadatos del archivo (archivo verificado en DataNode)...")
        
        with cluster_lock:
            current_term = cluster_state["term"]
        
        file_id = add_file_metadata(
            name=filename,
            tags=tag_list,
            size=file_size,
            hash_value=hash_value,
            node_id=NODE_ID,
            user_id=current_user.username,
            term=current_term
        )
        
        if not file_id:
            raise HTTPException(status_code=500, detail="No se pudieron crear los metadatos del archivo")
        
        print(f"[NAMENODE] [CHUNKED_UPLOAD] ✅ Metadatos creados: file_id={file_id}")
        
        # Actualizar upload_info con el file_id creado
        with uploads_lock:
            if upload_id in active_uploads:
                active_uploads[upload_id]["file_id"] = file_id
    else:
        print(f"[NAMENODE] [CHUNKED_UPLOAD] ✅ Usando metadatos existentes: file_id={file_id}")
    
    # ========== REPLICACIÓN A DATANODES SECUNDARIOS ==========
    # El archivo ya está en el datanode primario, ahora replicarlo a los otros datanodes
    primary_datanode_id = datanode_ids[0]
    primary_datanode_url = datanode_url  # Ya está en formato http://...
    
    successful_replicas = [primary_datanode_id]  # El primario ya tiene el archivo
    failed_replicas = []
    
    # Obtener token de servicio para autenticación con DataNodes
    with cluster_lock:
        node_id = cluster_state["node_id"]
    try:
        service_token = generate_service_token(node_id, "service")
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Token generado para node_id={node_id}")
    except Exception as token_error:
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Error generando token para node_id={node_id}: {token_error}, usando token pre-compartido")
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    # Replicar a datanodes secundarios (excluyendo el primario)
    if len(datanode_ids) > 1:
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Iniciando replicación a {len(datanode_ids) - 1} datanodes adicionales...")
        
        for i in range(1, len(datanode_ids)):
            target_datanode_id = datanode_ids[i]
            
            try:
                # Obtener información del datanode destino
                target_datanode = get_datanode(target_datanode_id, node_id_db=NODE_ID)
                if not target_datanode:
                    print(f"[NAMENODE] [CHUNKED_UPLOAD] ⚠️  DataNode {target_datanode_id} no encontrado, saltando...")
                    failed_replicas.append(target_datanode_id)
                    continue
                
                target_datanode_url = target_datanode["url"]
                if not target_datanode_url.startswith("http"):
                    target_datanode_url = f"http://{target_datanode_url}:{target_datanode['port']}"
                
                print(f"[NAMENODE] [CHUNKED_UPLOAD] Replicando a {target_datanode_id} ({target_datanode_url})...")
                
                # Usar función que transfiere chunks directamente desde el primario al destino
                # Esto es eficiente porque no ensambla el archivo completo en memoria
                success, message = send_chunks_from_datanode(
                    source_datanode_url=primary_datanode_url,
                    target_datanode_url=target_datanode_url,
                    file_id=file_hash,
                    service_token=service_token
                )
                
                if success:
                    successful_replicas.append(target_datanode_id)
                    print(f"[NAMENODE] [CHUNKED_UPLOAD] ✓ Réplica exitosa en {target_datanode_id}")
                else:
                    failed_replicas.append(target_datanode_id)
                    print(f"[NAMENODE] [CHUNKED_UPLOAD] ✗ Error replicando a {target_datanode_id}: {message}")
                    
            except Exception as e:
                failed_replicas.append(target_datanode_id)
                print(f"[NAMENODE] [CHUNKED_UPLOAD] ✗ Excepción replicando a {target_datanode_id}: {e}")
                import traceback
                traceback.print_exc()
        
        # Verificar mínimo de réplicas requeridas
        # Idealmente 2, pero aceptar 1 si solo hay 1 DataNode disponible
        min_required_replicas = min(2, len(datanode_ids)) if datanode_ids else 1
        
        if len(successful_replicas) < min_required_replicas:
            error_msg = f"No se pudo replicar el archivo a suficientes DataNodes después de finalizar el upload ({len(successful_replicas)}/{len(datanode_ids)} exitosas, mínimo requerido: {min_required_replicas})"
            print(f"[NAMENODE] [CHUNKED_UPLOAD] ❌ {error_msg}")
            
            # No lanzar excepción, solo reportar el problema
            # El archivo al menos está en el primario y está disponible
            print(f"[NAMENODE] [CHUNKED_UPLOAD] ⚠️  Advertencia: {error_msg}. El archivo está disponible en {len(successful_replicas)} datanode(s)")
        else:
            print(f"[NAMENODE] [CHUNKED_UPLOAD] ✅ Replicación completada: {len(successful_replicas)}/{len(datanode_ids)} réplicas exitosas")
    else:
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Solo hay 1 datanode asignado, no se requiere replicación adicional")
    
    # Guardar asignación de réplicas (solo las exitosas)
    # IMPORTANTE: file_id ya fue creado arriba, así que podemos guardar las réplicas
    save_file_replicas(file_id, successful_replicas, node_id_db=NODE_ID)
    
    # Replicar operación a otros MetaNameNodes
    with cluster_lock:
        current_term = cluster_state["term"]
    
    operation = OperationLog(
        operation="add_file",
        data={
            "name": filename,
            "tags": tag_list,
            "size": file_size,
            "hash": f"sha256:{file_hash}",
            "datanode_ids": datanode_ids,
            "user_id": current_user.username
        },
        term=current_term,
        timestamp=time.time()
    )
    
    with log_lock:
        operation_log.append(operation)
    
    save_operation_to_log(operation, NODE_ID)
    replicate_to_peers(operation)
    
    # Limpiar upload de la lista activa
    with uploads_lock:
        if upload_id in active_uploads:
            del active_uploads[upload_id]
    
    print(f"[NAMENODE] [CHUNKED_UPLOAD] Upload finalizado: upload_id={upload_id}, file_id={file_id}, réplicas={successful_replicas} (exitosas: {len(successful_replicas)}/{len(datanode_ids)})")
    
    return {
        "success": True,
        "file_id": file_id,
        "message": f"Archivo '{filename}' subido correctamente",
        "replicas": successful_replicas,
        "replicas_stored": len(successful_replicas),
        "replicas_assigned": len(datanode_ids),
        "replicas_failed": len(failed_replicas) if len(datanode_ids) > 1 else 0
    }


@app.get("/list")
def list_files_compat(
    tags: Optional[List[str]] = Query(None),
    current_user: User = Depends(require_permission(Permission.READ_FILES)),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint de compatibilidad: lista archivos del usuario actual (requiere autenticación)"""
    # Redirigir al líder si no somos el líder
    leader_url = get_leader_url()
    if leader_url:
        print(f"[NAMENODE] Redirigiendo list a líder: {leader_url}")
        try:
            # Construir parámetros de query
            params = {}
            if tags:
                params["tags"] = tags
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            response = requests.get(
                f"{leader_url}/list",
                params=params,
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[NAMENODE] Error redirigiendo list a líder: {e}")
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        print(f"[NAMENODE] Este nodo no es líder, pero no hay líder disponible")
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Procesar en el líder
    files = query_files(query_tags=tags, node_id=NODE_ID, user_id=current_user.username)
    formatted = [
        {"id": fid, "name": name, "tags": tags, "path": ""}
        for fid, name, tags in files
    ]
    return {"files": formatted}


@app.delete("/delete")
def delete_files_compat(
    tags: str = Query(...),
    current_user: User = Depends(require_delete_permission()),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint de compatibilidad: elimina archivos por tags (requiere autenticación y permiso)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            response = requests.delete(
                f"{leader_url}/delete",
                params={"tags": tags},
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    
    # Obtener term actual para versionado
    with cluster_lock:
        current_term = cluster_state["term"]
    
    deleted = delete_files_by_tags(tag_list, node_id=NODE_ID, user_id=current_user.username, term=current_term)
    
    if deleted:
        operation = OperationLog(
            operation="delete_files_by_tags",
            data={"tags": tag_list, "user_id": current_user.username},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        # Guardar en log persistente (Fase 2)
        save_operation_to_log(operation, NODE_ID)
        replicate_to_peers(operation)
    
    return {
        "success": deleted,
        "message": "Archivos eliminados" if deleted else "No se encontró coincidencia"
    }


@app.delete("/delete-by-id/{file_id}")
def delete_file_by_id(
    file_id: int,
    current_user: User = Depends(require_delete_permission()),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Elimina un archivo por su ID"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            response = requests.delete(
                f"{leader_url}/delete-by-id/{file_id}",
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Verificar que el archivo existe y pertenece al usuario
    file_info = get_file_by_id(file_id, node_id=NODE_ID, user_id=current_user.username)
    if not file_info:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    
    with cluster_lock:
        current_term = cluster_state["term"]
    
    deleted = delete_file_metadata(file_id, node_id=NODE_ID, user_id=current_user.username, term=current_term)
    
    if deleted:
        operation = OperationLog(
            operation="delete_file",
            data={"file_id": file_id, "user_id": current_user.username},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with cluster_lock:
            operation_log.append(operation)
        save_operation_to_log(operation, NODE_ID)
        replicate_to_peers(operation)
    
    return {
        "success": deleted,
        "message": "Archivo eliminado" if deleted else "No se pudo eliminar el archivo"
    }


@app.delete("/delete-exact")
def delete_files_exact_tags(
    tags: str = Query(...),
    current_user: User = Depends(require_delete_permission()),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Elimina archivos que tienen EXACTAMENTE las etiquetas especificadas (ni más, ni menos)"""
    leader_url = get_leader_url()
    if leader_url:
        try:
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            response = requests.delete(
                f"{leader_url}/delete-exact",
                params={"tags": tags},
                headers=headers,
                timeout=5
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    
    with cluster_lock:
        current_term = cluster_state["term"]
    
    deleted_count = delete_files_by_exact_tags(tag_list, node_id=NODE_ID, user_id=current_user.username, term=current_term)
    
    if deleted_count > 0:
        operation = OperationLog(
            operation="delete_files_by_exact_tags",
            data={"tags": tag_list, "user_id": current_user.username, "deleted": deleted_count},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with cluster_lock:
            operation_log.append(operation)
        save_operation_to_log(operation, NODE_ID)
        replicate_to_peers(operation)
    
    return {
        "success": deleted_count > 0,
        "deleted": deleted_count,
        "message": f"{deleted_count} archivo(s) eliminado(s)" if deleted_count > 0 else "No se encontraron archivos con esas etiquetas exactas"
    }


@app.post("/add-tags")
def add_tags_compat(
    query: str = Query(...),
    new_tags: str = Query(...),
    current_user: User = Depends(require_permission(Permission.MANAGE_TAGS))
):
    """Endpoint de compatibilidad: agrega etiquetas (requiere autenticación y permiso)"""
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
    
    # Obtener term actual para versionado
    with cluster_lock:
        current_term = cluster_state["term"]
    
    ok = add_tags_to_files(query_tags, new_tags_list, node_id=NODE_ID, user_id=current_user.username, term=current_term)
    
    if ok:
        operation = OperationLog(
            operation="add_tags",
            data={"query_tags": query_tags, "new_tags": new_tags_list, "user_id": current_user.username},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        # Guardar en log persistente (Fase 2)
        save_operation_to_log(operation, NODE_ID)
        replicate_to_peers(operation)
    
    return {"success": ok}


@app.post("/delete-tags")
def delete_tags_compat(
    query: str = Query(...),
    del_tags: str = Query(...),
    current_user: User = Depends(require_permission(Permission.MANAGE_TAGS))
):
    """Endpoint de compatibilidad: elimina etiquetas (requiere autenticación y permiso)"""
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
    
    # Obtener term actual para versionado
    with cluster_lock:
        current_term = cluster_state["term"]
    
    ok = delete_tags_from_files(query_tags, del_tags_list, node_id=NODE_ID, user_id=current_user.username, term=current_term)
    
    if ok:
        operation = OperationLog(
            operation="delete_tags",
            data={"query_tags": query_tags, "del_tags": del_tags_list, "user_id": current_user.username},
            term=cluster_state["term"],
            timestamp=time.time()
        )
        with log_lock:
            operation_log.append(operation)
        # Guardar en log persistente (Fase 2)
        save_operation_to_log(operation, NODE_ID)
        replicate_to_peers(operation)
    
    return {"success": ok}


@app.get("/download/{file_name}")
def download_file_compat(
    file_name: str,
    current_user: User = Depends(require_permission(Permission.READ_FILES)),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
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
            # Pasar el token de autenticación al líder
            headers = {}
            if authorization:
                headers["Authorization"] = authorization
            try:
                response = requests.get(
                    f"{leader_url}/download/{file_name}",
                    headers=headers,
                    timeout=30,
                    allow_redirects=True  # Seguir redirecciones automáticamente
                )
                response.raise_for_status()
                # Retornar el archivo directamente
                from fastapi.responses import Response
                return Response(
                    content=response.content,
                    media_type=response.headers.get("Content-Type", "application/octet-stream"),
                    headers={
                        "Content-Disposition": response.headers.get("Content-Disposition", f'attachment; filename="{file_name}"'),
                        "Content-Length": response.headers.get("Content-Length", str(len(response.content)))
                    }
                )
            except requests.RequestException as e:
                print(f"[NAMENODE] Error redirigiendo descarga a líder: {e}")
                raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
        else:
            # No hay líder disponible
            print(f"[NAMENODE] ERROR: No hay líder disponible para procesar descarga")
            raise HTTPException(status_code=503, detail="No hay líder disponible. Intenta de nuevo en unos momentos.")
    
    # Buscar archivo por nombre en metadatos (solo del usuario actual)
    files = query_files(query_tags=None, node_id=NODE_ID, user_id=current_user.username)
    file_data = None
    file_id = None
    file_hash = None
    
    for fid, name, _ in files:
        if name == file_name:
            file_data = get_file_by_id(fid, node_id=NODE_ID, user_id=current_user.username)
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
            # Obtener token de servicio para autenticación con DataNode
            try:
                service_token = generate_service_token(cluster_state["node_id"], "service")
            except Exception:
                service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
            
            print(f"[NAMENODE] Intentando leer desde {replica['datanode_id']} ({replica_type})...")
            response = requests.get(
                f"{datanode_url}/retrieve/{file_hash}",
                headers={"Authorization": f"Bearer {service_token}"},
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


@app.post("/download/init")
def init_chunked_download(
    filename: str = Form(...),
    current_user: User = Depends(require_permission(Permission.READ_FILES))
):
    """
    Inicia una descarga por chunks.
    Obtiene información del archivo y genera token para descargar directamente desde DataNode.
    """
    leader_url = get_leader_url()
    if leader_url:
        try:
            response = requests.post(
                f"{leader_url}/download/init",
                data={"filename": filename},
                headers={"Authorization": f"Bearer {current_user.username}"},
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    print(f"[NAMENODE] [CHUNKED_DOWNLOAD] Iniciando descarga: {filename}")
    
    # Buscar archivo por nombre en metadatos (solo del usuario actual)
    files = query_files(query_tags=None, node_id=NODE_ID, user_id=current_user.username)
    file_data = None
    file_id = None
    file_hash = None
    file_size = None
    
    for fid, name, _ in files:
        if name == filename:
            file_data = get_file_by_id(fid, node_id=NODE_ID, user_id=current_user.username)
            if file_data:
                file_id = fid
                file_hash_value = file_data.get("hash", "")
                file_size = file_data.get("size", 0)
                # Extraer hash sin prefijo "sha256:"
                file_hash = file_hash_value[7:] if file_hash_value.startswith("sha256:") else file_hash_value
                break
    
    if not file_data or not file_hash:
        raise HTTPException(status_code=404, detail=f"Archivo '{filename}' no encontrado")
    
    # Obtener réplicas del archivo
    replicas = get_file_replicas(file_id, node_id_db=NODE_ID)
    if not replicas:
        raise HTTPException(status_code=503, detail="No hay réplicas disponibles para este archivo")
    
    # Seleccionar mejor DataNode para lectura
    primary_datanode = get_best_datanode_for_read(file_id, node_id_db=NODE_ID)
    if not primary_datanode:
        # Si no hay mejor opción, usar la primera réplica
        primary_datanode = replicas[0]
    
    primary_datanode_id = primary_datanode["datanode_id"]
    datanode_info = get_datanode(primary_datanode_id, node_id_db=NODE_ID)
    if not datanode_info:
        raise HTTPException(status_code=503, detail=f"DataNode {primary_datanode_id} no encontrado")
    
    # Construir URL del DataNode
    datanode_url = datanode_info["url"]
    if not datanode_url.startswith("http"):
        datanode_url = f"http://{datanode_url}:{datanode_info['port']}"
    
    # Calcular tamaño de chunk (usar el mismo que para uploads: 5MB)
    CHUNK_SIZE = 5 * 1024 * 1024
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE if file_size > 0 else 1
    
    # Generar token temporal para el cliente (válido por 15 minutos)
    from security.service_auth import generate_client_download_token
    client_token = generate_client_download_token(
        user_id=current_user.username,
        file_hash=file_hash,
        datanode_id=primary_datanode_id,
        expires_minutes=15
    )
    
    print(f"[NAMENODE] [CHUNKED_DOWNLOAD] Descarga iniciada: file_id={file_id}, datanode={primary_datanode_id}, chunks={total_chunks}")
    
    return {
        "download_id": str(uuid.uuid4()),
        "file_id": file_id,
        "filename": filename,
        "file_hash": file_hash,
        "file_size": file_size,
        "datanode_url": datanode_url,
        "datanode_id": primary_datanode_id,
        "client_token": client_token,
        "total_chunks": total_chunks,
        "chunk_size": CHUNK_SIZE
    }


# ========== ENDPOINTS INTERNOS PARA REPLICACIÓN ==========

@app.post("/internal/fix-primary-replicas")
def internal_fix_primary_replicas(
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Endpoint interno para corregir archivos que no tienen réplica primary activa.
    Requiere token de servicio.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Corregir réplicas sin primary
    from namenode.datanode_manager import fix_missing_primary_replicas
    fixed_count = fix_missing_primary_replicas(node_id_db=NODE_ID)
    
    return {
        "status": "success",
        "files_fixed": fixed_count,
        "message": f"Se corrigieron {fixed_count} archivos sin réplica primary"
    }


@app.post("/internal/replicate")
def internal_replicate(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint interno para recibir replicación del líder (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    
    # 🔍 LOG 1: Ver si el token se puede decodificar
    print(f"[NAMENODE] [REPLICATE] 🔍 Token recibido (primeros 20 chars): {token[:20]}...")
    
    payload = verify_service_token(token)
    
    # 🔍 LOG 2: Ver si verify_service_token retorna None
    if not payload:
        print(f"[NAMENODE] [REPLICATE] ❌ ERROR: verify_service_token retornó None (token inválido)")
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # 🔍 LOG 3: Ver qué contiene el payload
    print(f"[NAMENODE] [REPLICATE] ✅ Token válido. Payload completo: {payload}")
    
    # Verificar que viene de otro namenode
    service_id = payload.get("service_id") or payload.get("sub", "")
    
    # 🔍 LOG 4: Ver qué service_id se extrajo
    print(f"[NAMENODE] [REPLICATE] 🔍 Service ID extraído: '{service_id}'")
    print(f"[NAMENODE] [REPLICATE] 🔍 Service ID empieza con 'namenode-': {service_id.startswith('namenode-')}")
    print(f"[NAMENODE] [REPLICATE] 🔍 Service ID empieza con 'tbfs-namenode-': {service_id.startswith('tbfs-namenode-')}")
    
    # Aceptar tanto 'namenode-' como 'tbfs-namenode-'
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        print(f"[NAMENODE] [REPLICATE] ❌ ERROR: Service ID '{service_id}' NO empieza con 'namenode-' ni 'tbfs-namenode-'")
        raise HTTPException(status_code=403, detail="Solo namenodes pueden replicar")
    
    print(f"[NAMENODE] [REPLICATE] ✅ Service ID válido: '{service_id}'")
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
                node_id=NODE_ID,
                user_id=operation_data.get("user_id", "system"),  # Para replicación
                term=term  # Usar el term de la operación replicada
            )
            # También guardar las réplicas si están en los datos de la operación
            if file_id and "datanode_ids" in operation_data:
                from namenode.datanode_manager import save_file_replicas
                datanode_ids = operation_data["datanode_ids"]
                if datanode_ids:
                    save_file_replicas(file_id, datanode_ids, node_id_db=NODE_ID)
                    print(f"[NAMENODE] Réplicas replicadas para file_id={file_id}: {datanode_ids}")
        elif operation == "delete_file":
            delete_file_metadata(operation_data["file_id"], node_id=NODE_ID, term=term)
        elif operation == "delete_files_by_tags":
            delete_files_by_tags(operation_data["tags"], node_id=NODE_ID, 
                                user_id=operation_data.get("user_id", "system"), term=term)
        elif operation == "add_tags":
            add_tags_to_files(
                operation_data["query_tags"],
                operation_data["new_tags"],
                node_id=NODE_ID,
                user_id=operation_data.get("user_id", "system"),
                term=term
            )
        elif operation == "delete_tags":
            delete_tags_from_files(
                operation_data["query_tags"],
                operation_data["del_tags"],
                node_id=NODE_ID,
                user_id=operation_data.get("user_id", "system"),
                term=term
            )
        elif operation == "create_user":
            # Replicar creación de usuario
            from security.auth import get_users_db_path, get_password_hash
            db_path = get_users_db_path(NODE_ID)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            try:
                # Verificar si el usuario ya existe
                cursor.execute("SELECT username FROM users WHERE username = ?", (operation_data["username"],))
                if cursor.fetchone():
                    print(f"[NAMENODE] Usuario {operation_data['username']} ya existe, saltando replicación")
                else:
                    cursor.execute("""
                        INSERT INTO users (username, password_hash, role, is_active)
                        VALUES (?, ?, ?, ?)
                    """, (
                        operation_data["username"],
                        operation_data["password_hash"],
                        operation_data["role"],
                        1 if operation_data.get("is_active", True) else 0
                    ))
                    conn.commit()
                    print(f"[NAMENODE] Usuario {operation_data['username']} replicado")
            except Exception as e:
                conn.rollback()
                print(f"[NAMENODE] Error replicando usuario: {e}")
            finally:
                conn.close()
        elif operation == "change_password":
            # Replicar cambio de contraseña
            from security.auth import get_users_db_path
            db_path = get_users_db_path(NODE_ID)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    UPDATE users 
                    SET password_hash = ?
                    WHERE username = ?
                """, (operation_data["new_password_hash"], operation_data["username"]))
                conn.commit()
                print(f"[NAMENODE] Contraseña replicada para usuario {operation_data['username']}")
            except Exception as e:
                print(f"[NAMENODE] Error replicando cambio de contraseña: {e}")
                conn.rollback()
            finally:
                conn.close()
        
        # IMPORTANTE: Guardar la operación en el log local del seguidor
        # Esto permite que si este nodo se convierte en líder, pueda replicar a nuevos seguidores
        op_log = OperationLog(
            operation=operation,
            data=operation_data,
            term=term,
            timestamp=timestamp
        )
        save_operation_to_log(op_log, NODE_ID)
        with cluster_lock:
            operation_log.append(op_log)
        print(f"[NAMENODE] [REPLICATE] ✅ Operación '{operation}' guardada en log local")
        
        return {"success": True}
    except Exception as e:
        print(f"[NAMENODE] Error en internal_replicate: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


@app.post("/internal/replicate-sql")
def internal_replicate_sql(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint interno para recibir replicación de escrituras SQL del líder"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro namenode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        raise HTTPException(status_code=403, detail="Solo namenodes pueden replicar")
    
    try:
        sql_writes = data.get("sql_writes", [])
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
        
        # Asegurar que las bases de datos estén inicializadas
        from namenode.database import init_metadata_db, init_operations_db
        init_metadata_db(node_id=NODE_ID)
        init_operations_db(node_id=NODE_ID)
        
        # Aplicar escrituras SQL localmente
        from namenode.database import get_connection, close_connection, metadata_db_lock, operations_db_lock
        import sqlite3
        
        applied_count = 0
        skipped_count = 0
        for write in sql_writes:
            sql = write.get("sql")
            params = write.get("params")
            db_type = write.get("db_type", "metadata")
            
            if not sql:
                continue
            
            # Seleccionar lock según tipo de BD
            db_lock_to_use = operations_db_lock if db_type == "operations" else metadata_db_lock
            
            with db_lock_to_use:
                conn, cursor = get_connection(db_type=db_type, node_id=NODE_ID)
                try:
                    # Marcar que estamos replicando para evitar loop infinito
                    # La conexión es un ReplicatingConnection, así que podemos setear el flag
                    conn._is_replicating = True
                    
                    # Deserializar parámetros si es necesario
                    if params:
                        # Si params es una lista, ejecutar con parámetros
                        if isinstance(params, list):
                            # Deserializar bytes si es necesario
                            deserialized_params = []
                            for p in params:
                                if isinstance(p, dict) and p.get('__type__') == 'bytes':
                                    import base64
                                    deserialized_params.append(base64.b64decode(p['__value__']))
                                else:
                                    deserialized_params.append(p)
                            cursor.execute(sql, deserialized_params)
                        else:
                            # Parámetro único
                            if isinstance(params, dict) and params.get('__type__') == 'bytes':
                                import base64
                                params = base64.b64decode(params['__value__'])
                            cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    
                    conn.commit()
                    applied_count += 1
                except sqlite3.IntegrityError as e:
                    # Error de UNIQUE constraint - el dato ya existe, esto es normal en replicación
                    conn.rollback()
                    error_msg = str(e)
                    # Solo loguear si no es un error esperado de duplicado
                    if "UNIQUE constraint" in error_msg:
                        skipped_count += 1
                        # No loguear estos errores para reducir ruido en logs
                        # print(f"[NAMENODE] [SQL_REPLICATE] Dato ya existe (esperado): {sql[:50]}...")
                    else:
                        print(f"[NAMENODE] [SQL_REPLICATE] Error de integridad: {sql[:50]}... - {e}")
                except sqlite3.OperationalError as e:
                    error_msg = str(e)
                    conn.rollback()
                    
                    # Si la tabla no existe, intentar crearla
                    if "no such table" in error_msg.lower():
                        print(f"[NAMENODE] [SQL_REPLICATE] Tabla faltante detectada, inicializando BD...")
                        try:
                            # Re-inicializar la base de datos correspondiente
                            if db_type == "operations":
                                init_operations_db(node_id=NODE_ID)
                            else:
                                init_metadata_db(node_id=NODE_ID)
                            
                            # Reintentar la operación
                            conn, cursor = get_connection(db_type=db_type, node_id=NODE_ID)
                            conn._is_replicating = True
                            
                            if params:
                                if isinstance(params, list):
                                    deserialized_params = []
                                    for p in params:
                                        if isinstance(p, dict) and p.get('__type__') == 'bytes':
                                            import base64
                                            deserialized_params.append(base64.b64decode(p['__value__']))
                                        else:
                                            deserialized_params.append(p)
                                    cursor.execute(sql, deserialized_params)
                                else:
                                    if isinstance(params, dict) and params.get('__type__') == 'bytes':
                                        import base64
                                        params = base64.b64decode(params['__value__'])
                                    cursor.execute(sql, params)
                            else:
                                cursor.execute(sql)
                            
                            conn.commit()
                            applied_count += 1
                            print(f"[NAMENODE] [SQL_REPLICATE] ✅ Tabla creada y operación aplicada: {sql[:50]}...")
                        except Exception as retry_e:
                            print(f"[NAMENODE] [SQL_REPLICATE] ❌ Error después de crear tabla: {retry_e}")
                            conn.rollback()
                    else:
                        print(f"[NAMENODE] [SQL_REPLICATE] Error operacional: {sql[:50]}... - {e}")
                        import traceback
                        traceback.print_exc()
                except Exception as e:
                    conn.rollback()
                    print(f"[NAMENODE] [SQL_REPLICATE] Error aplicando SQL: {sql[:50]}... - {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    conn._is_replicating = False
                    # Cerrar la conexión real (el wrapper delega close() a _conn)
                    close_connection(conn)
        
        print(f"[NAMENODE] [SQL_REPLICATE] ✅ {applied_count}/{len(sql_writes)} escrituras SQL aplicadas ({skipped_count} duplicados ignorados)")
        return {"success": True, "applied": applied_count, "skipped": skipped_count}
        
    except Exception as e:
        print(f"[NAMENODE] [SQL_REPLICATE] Error en internal_replicate_sql: {e}")
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
    candidate_id = request.candidate_id
    print(f"[NAMENODE] [VOTE] 📥 Recibida solicitud de voto de candidato: {candidate_id}, term: {request.term}")
    
    # Usar validate_service_request que maneja tanto JWT como tokens pre-compartidos
    if not validate_service_request(candidate_id, token):
        print(f"[NAMENODE] [VOTE] ❌ Token de servicio inválido para candidato {candidate_id}")
        # Intentar verificar el token para obtener más información de diagnóstico
        payload = verify_service_token(token)
        if payload:
            service_id_from_token = payload.get("service_id") or payload.get("sub", "")
            print(f"[NAMENODE] [VOTE] 📋 Token decodificado pero no coincide: service_id={service_id_from_token}, candidate_id={candidate_id}")
        else:
            print(f"[NAMENODE] [VOTE] 📋 Token no se puede decodificar como JWT")
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Si validate_service_request pasó, obtener el service_id del token para verificación adicional
    payload = verify_service_token(token)
    if payload:
        service_id = payload.get("service_id") or payload.get("sub", "")
        # Verificar que viene de otro namenode
        if "namenode" not in service_id.lower():
            print(f"[NAMENODE] [VOTE] ❌ Error: service_id '{service_id}' no es un namenode")
            raise HTTPException(status_code=403, detail="Solo namenodes pueden votar")
        print(f"[NAMENODE] [VOTE] ✅ Solicitud de voto autenticada de {service_id} (candidato: {candidate_id}, term: {request.term})")
    else:
        # Si no es JWT, es token pre-compartido (ya validado por validate_service_request)
        print(f"[NAMENODE] [VOTE] ✅ Solicitud de voto autenticada con token pre-compartido (candidato: {candidate_id}, term: {request.term})")
    
    # Si este nodo es el líder, detectar si el candidato es un nuevo seguidor
    candidate_id = request.candidate_id
    if is_leader():
        with cluster_lock:
            if candidate_id not in cluster_state["peers"] and candidate_id != cluster_state["node_id"]:
                print(f"[NAMENODE] 🆕 Nuevo seguidor detectado durante votación: {candidate_id}")
                cluster_state["peers"].append(candidate_id)
                # Inicializar tracking de conectividad
                if candidate_id not in cluster_state["peer_status"]:
                    cluster_state["peer_status"][candidate_id] = {
                        "last_seen": time.time(),
                        "status": "alive"
                    }
                print(f"[NAMENODE] 📋 Nuevo seguidor agregado a lista de peers: {candidate_id}")
                print(f"[NAMENODE] 📋 Lista actualizada de peers: {sorted(cluster_state['peers'])}")
    
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
def internal_heartbeat(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """Endpoint interno para recibir heartbeats del líder (requiere token de servicio)"""
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro namenode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        raise HTTPException(status_code=403, detail="Solo namenodes pueden enviar heartbeats")
    term = data.get("term")
    leader_id = data.get("leader_id")
    peers_from_leader = data.get("peers", [])
    active_followers_from_leader = data.get("active_followers", [])
    
    print(f"[NAMENODE] 💓 [HEARTBEAT] 📥 Heartbeat recibido del líder {leader_id}")
    print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Peers recibidos del líder ({len(peers_from_leader)}): {sorted(peers_from_leader)}")
    print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Líder: {leader_id}, Term: {term}")
    
    with cluster_lock:
        peers_before_update = cluster_state["peers"].copy()
        current_node_id = cluster_state["node_id"]
        current_is_leader = cluster_state["is_leader"]
        current_term = cluster_state["term"]
        
        # Si este nodo es el líder actual, ignorar heartbeats de otros nodos
        # (a menos que tengan un término mayor)
        if current_is_leader and leader_id != current_node_id:
            if term <= current_term:
                print(f"[NAMENODE] 💓 [HEARTBEAT] ⚠️ Ignorando heartbeat de {leader_id} (term={term}) - este nodo es el líder (term={current_term})")
                return {"success": False, "reason": "Este nodo es el líder actual"}
        
        if term >= current_term:
            # Solo ceder liderazgo si el término es estrictamente mayor,
            # o si no somos el líder
            if term > current_term or not current_is_leader:
                cluster_state["term"] = term
                cluster_state["leader_id"] = leader_id
                cluster_state["is_leader"] = False
                cluster_state["last_heartbeat_time"] = time.time()
            elif current_is_leader and term == current_term:
                # Somos el líder con el mismo término, mantener nuestro estado
                print(f"[NAMENODE] 💓 [HEARTBEAT] ⚠️ Conflicto de líderes detectado: este nodo es líder con term={current_term}, recibido de {leader_id}")
                return {"success": False, "reason": "Conflicto de líderes"}
        
        # Actualizar lista de peers desde el líder
        # El líder conoce todos los peers del clúster, así que actualizamos nuestra lista
        if peers_from_leader:
            # Excluir este nodo de la lista de peers (no somos nuestro propio peer)
            updated_peers = [p for p in peers_from_leader if p != current_node_id]
            
            # Asegurar que el líder esté incluido en la lista de peers conocidos
            if leader_id and leader_id != current_node_id:
                if leader_id not in updated_peers:
                    updated_peers.append(leader_id)
                    print(f"[NAMENODE] 📥 Líder {leader_id} agregado a la lista de peers conocidos")
            
            # Comparar con la lista actual
            old_peers = set(cluster_state["peers"])
            new_peers = set(updated_peers)
            
            if old_peers != new_peers:
                added_peers = new_peers - old_peers
                removed_peers = old_peers - new_peers
                
                if added_peers:
                    print(f"[NAMENODE] 📥 Nuevos peers recibidos del líder: {sorted(added_peers)}")
                if removed_peers:
                    print(f"[NAMENODE] 📥 Peers removidos (según líder): {sorted(removed_peers)}")
                
                # Actualizar lista de peers
                cluster_state["peers"] = updated_peers
                
                # Inicializar tracking de conectividad para nuevos peers
                for peer in added_peers:
                    if peer not in cluster_state["peer_status"]:
                        cluster_state["peer_status"][peer] = {
                            "last_seen": 0.0,
                            "status": "unknown"
                        }
                        print(f"[NAMENODE] 📥 Inicializado estado de peer para nuevo peer: {peer}")
                
                # Construir lista completa incluyendo líder
                complete_peers_after = updated_peers.copy()
                if leader_id and leader_id not in complete_peers_after:
                    complete_peers_after.append(leader_id)
                
                print(f"[NAMENODE] ✅ Lista de peers actualizada desde líder: {len(updated_peers)} peers conocidos")
                print(f"[NAMENODE] 📋 Peers actuales (sin líder): {sorted(updated_peers)}")
                print(f"[NAMENODE] 📋 Lista completa de peers conocidos ({len(complete_peers_after)}): {sorted(complete_peers_after)}")
                print(f"[NAMENODE] 📋 Líder incluido en lista: {leader_id in complete_peers_after}")
        
        # Actualizar lista de seguidores activos desde el líder
        if active_followers_from_leader is not None:
            # Excluir este nodo de la lista de seguidores activos (no somos nuestro propio seguidor)
            current_node_id = cluster_state["node_id"]
            updated_active_followers = [f for f in active_followers_from_leader if f != current_node_id]
            
            # Comparar con la lista actual
            old_active_followers = set(cluster_state.get("active_followers", []))
            new_active_followers = set(updated_active_followers)
            
            if old_active_followers != new_active_followers:
                added_followers = new_active_followers - old_active_followers
                removed_followers = old_active_followers - new_active_followers
                
                if added_followers:
                    print(f"[NAMENODE] 📥 Nuevos seguidores activos recibidos del líder: {sorted(added_followers)}")
                if removed_followers:
                    print(f"[NAMENODE] 📥 Seguidores inactivos (según líder): {sorted(removed_followers)}")
                
                # Actualizar lista de seguidores activos
                cluster_state["active_followers"] = updated_active_followers
                
                print(f"[NAMENODE] ✅ Lista de seguidores activos actualizada desde líder: {len(updated_active_followers)} seguidores activos")
                if updated_active_followers:
                    print(f"[NAMENODE] 👥 Seguidores activos: {sorted(updated_active_followers)}")
                else:
                    print(f"[NAMENODE] 👥 No hay seguidores activos según el líder")
    
    # Responder al líder con el node_id de este seguidor para que lo almacene y distribuya
    with cluster_lock:
        follower_node_id = cluster_state["node_id"]
    
    return {
        "success": True,
        "node_id": follower_node_id  # Enviar node_id al líder para que lo almacene y distribuya
    }


@app.post("/internal/gossip")
def internal_gossip(
    exchange: GossipExchange,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Endpoint interno para intercambio de estado en protocolo Gossip entre namenodes.
    Recibe estado de un peer y retorna el estado local.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        print(f"[NAMENODE] [GOSSIP] Error: Se requiere token de servicio")
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        print(f"[NAMENODE] [GOSSIP] Error: Token de servicio inválido o expirado")
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro namenode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        print(f"[NAMENODE] [GOSSIP] Error: service_id '{service_id}' no es un namenode")
        raise HTTPException(status_code=403, detail="Solo namenodes pueden hacer gossip")
    
    sender_id = exchange.sender_id
    
    print(f"[NAMENODE] [GOSSIP] 📥 Intercambio recibido de {sender_id}")
    print(f"[NAMENODE] [GOSSIP] 📋 Peers conocidos recibidos ({len(exchange.known_peers)}): {sorted(exchange.known_peers)}")
    print(f"[NAMENODE] [GOSSIP] 📋 Detalle remoto: is_leader={exchange.is_leader}, leader_id={exchange.leader_id}, version={exchange.sender_version}")
    
    # Actualizar estado del peer como vivo
    update_peer_status(sender_id, True)
    
    # Actualizar versión y peers conocidos
    peers_changed = False
    with cluster_lock:
        peers_before = cluster_state["peers"].copy()
        leader_before = cluster_state["leader_id"]
        local_version = cluster_state["version"]
        # Actualizar versión al máximo entre local y remota
        max_version = max(local_version, exchange.sender_version)
        if max_version > cluster_state["version"]:
            old_version = cluster_state["version"]
            cluster_state["version"] = max_version
            print(f"[NAMENODE] [GOSSIP] Versión actualizada de {old_version} a {max_version} (de {sender_id})")
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
                    print(f"[NAMENODE] [GOSSIP] Nuevo peer descubierto: {remote_peer}")
                # Inicializar estado del nuevo peer si no existe
                if remote_peer not in cluster_state["peer_status"]:
                    cluster_state["peer_status"][remote_peer] = {
                        "last_seen": 0.0,
                        "status": "unknown"
                    }
                peers_changed = True
        
        # Asegurar que no hay duplicados ni el node_id después de agregar
        cluster_state["peers"] = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
        cluster_state["peers"] = list(set(cluster_state["peers"]))
        
        # Actualizar información del líder si el peer remoto es líder o conoce un líder
        # IMPORTANTE: No cambiar is_leader si este nodo ya es el líder
        if exchange.is_leader and exchange.leader_id == sender_id:
            # El peer remoto dice ser el líder
            if cluster_state["is_leader"]:
                # Este nodo es el líder, ignorar la información del peer
                print(f"[NAMENODE] [GOSSIP] ⚠️ Ignorando info de líder de {sender_id} - este nodo es el líder")
            elif cluster_state["leader_id"] != sender_id:
                # No somos líder, actualizar leader_id
                print(f"[NAMENODE] [GOSSIP] Líder actualizado desde gossip: {sender_id}")
                cluster_state["leader_id"] = sender_id
        elif exchange.leader_id and exchange.leader_id != cluster_state["leader_id"]:
            # El peer remoto conoce un líder diferente
            if not cluster_state["is_leader"]:
                print(f"[NAMENODE] [GOSSIP] Líder conocido actualizado desde gossip: {exchange.leader_id}")
                cluster_state["leader_id"] = exchange.leader_id
        
        # Preparar lista de peers conocidos para retornar (sin duplicados, incluyendo este nodo y el líder si existe)
        known_peers_list = [p for p in cluster_state["peers"] if p != cluster_state["node_id"]]
        known_peers_list.append(cluster_state["node_id"])  # Incluir este nodo
        # Incluir el líder si existe y es diferente del node_id
        if cluster_state["leader_id"] and cluster_state["leader_id"] != cluster_state["node_id"]:
            if cluster_state["leader_id"] not in known_peers_list:
                known_peers_list.append(cluster_state["leader_id"])
        known_peers = list(set(known_peers_list))
        
        peers_after = cluster_state["peers"].copy()
        leader_after = cluster_state["leader_id"]
    
    print(f"[NAMENODE] [GOSSIP] 📊 Resumen del intercambio:")
    print(f"[NAMENODE] [GOSSIP] 📋 Peers antes: {len(peers_before)} -> después: {len(peers_after)}, Cambios: {peers_changed}")
    print(f"[NAMENODE] [GOSSIP] 📋 Líder antes: {leader_before} -> después: {leader_after}")
    print(f"[NAMENODE] [GOSSIP] 📋 Lista completa de peers conocidos que se retornará ({len(known_peers)}): {sorted(known_peers)}")
    print(f"[NAMENODE] [GOSSIP] 📋 Líder incluido en lista retornada: {leader_after in known_peers if leader_after else 'N/A'}")
    
    # Retornar nuestro estado actualizado
    with cluster_lock:
        return {
            "success": True,
            "node_id": cluster_state["node_id"],
            "version": local_version,
            "known_peers": known_peers,
            "is_leader": cluster_state["is_leader"],
            "leader_id": cluster_state["leader_id"],
            "timestamp": time.time()
        }


@app.get("/internal/operation-log")
def get_operation_log_endpoint(
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Fase 4: Endpoint interno para obtener el log de operaciones.
    Usado para reconciliación después de particionamiento.
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene de otro namenode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        raise HTTPException(status_code=403, detail="Solo namenodes pueden obtener logs")
    
    # Cargar log desde la base de datos
    operations = load_operation_log(NODE_ID)
    
    # Convertir a formato JSON serializable
    operations_data = []
    for op in operations:
        operations_data.append({
            "operation": op.operation,
            "data": op.data,
            "term": op.term,
            "timestamp": op.timestamp
        })
    
    return {
        "node_id": NODE_ID,
        "term": cluster_state["term"],
        "operations": operations_data,
        "total": len(operations_data)
    }