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
    "peers": [],  # Lista de peers conocidos (descubiertos mediante DNS de Docker)
    "peer_status": {},  # {peer_id: {"last_seen": float, "status": "alive"/"suspected"/"dead"}}
    "version": 0,  # Versión del estado local (incrementa en cada cambio de peers)
    "reconciliation_in_progress": False,  # Flag para evitar reconciliaciones simultáneas
    "active_followers": [],  # Lista de seguidores activos según el líder
    "last_file_replicas_snapshot": None,  # Snapshot de file_replicas para detectar cambios
    "last_datanodes_snapshot": None  # Snapshot de datanodes para detectar cambios
}
cluster_lock = threading.Lock()

# Configuración
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT", "30"))
LEADER_HEARTBEAT_INTERVAL = int(os.getenv("LEADER_HEARTBEAT_INTERVAL", "5"))
ELECTION_TIMEOUT = int(os.getenv("ELECTION_TIMEOUT", "15"))
FOLLOWER_LEADER_CHECK_INTERVAL = int(os.getenv("FOLLOWER_LEADER_CHECK_INTERVAL", "12"))  # Verificar líder cada 12 segundos
LEADER_TIMEOUT = int(os.getenv("LEADER_TIMEOUT", "15"))  # Tiempo sin respuesta antes de considerar líder muerto (10-15s)
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
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
    
    # 🔍 DEBUG: Log antes de guardar
    op_type = operation.operation
    op_name = operation.data.get('name', 'N/A') if operation.operation in ['add_file', 'delete_file'] else 'N/A'
    print(f"[DEBUG] 💾 Guardando en log persistente: {op_type} | archivo='{op_name}' | term={operation.term} | timestamp={operation.timestamp:.2f}")
    
    with operations_db_lock:
        # IMPORTANTE: Especificar db_type="operations" explícitamente para que la replicación SQL funcione correctamente
        conn, cursor = get_connection(db_path=db_path, node_id=node_id, db_type="operations")
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
            
            # 🔍 DEBUG: Confirmar guardado
            print(f"[DEBUG] ✅ Operación guardada en BD: {op_type} | archivo='{op_name}'")
            
        except Exception as e:
            print(f"[ERROR] ❌ Error guardando operación en log persistente: {e}")
            print(f"[DEBUG] Operación que falló: {op_type} | archivo='{op_name}' | term={operation.term}")
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
    
    # 🔍 DEBUG: Log antes de cargar
    print(f"[DEBUG] 📂 Cargando log de operaciones desde: {db_path}")
    
    with operations_db_lock:
        # IMPORTANTE: Especificar db_type="operations" explícitamente
        conn, cursor = get_connection(db_path=db_path, node_id=node_id, db_type="operations")
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
    
    # 🔍 DEBUG: Resumen de operaciones cargadas
    op_counts = {}
    for op in operations:
        op_counts[op.operation] = op_counts.get(op.operation, 0) + 1
    
    print(f"[DEBUG] 📂 Log cargado: {len(operations)} operaciones | Desglose: {op_counts}")
    
    # 🔍 DEBUG: Mostrar operaciones de archivos (últimas 10)
    file_ops = [op for op in operations if op.operation in ['add_file', 'delete_file']]
    if file_ops:
        print(f"[DEBUG] 📂 Operaciones de archivos en log ({len(file_ops)} total, mostrando últimas 10):")
        for op in file_ops[-10:]:
            op_name = op.data.get('name', 'N/A')
            print(f"[DEBUG] 📂   - {op.operation}: '{op_name}' | term={op.term} | t={op.timestamp:.2f}")
    
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
        Lista de IPs de peers descubiertos (SOLO IPs, excluyendo la IP local de este nodo)
    """
    discovered_ips = []
    
    try:
        # Resolver DNS para obtener todas las IPs de los namenodes con alias 'namenode'
        addr_info = socket.getaddrinfo(
            NAMENODE_SERVICE, 
            NAMENODE_PORT,
            proto=socket.IPPROTO_TCP
        )
        
        # Obtener la IP local de este nodo para excluirla
        local_ip = None
        try:
            # Método principal: IP de la interfaz de salida
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            # Método alternativo: IP del hostname
            try:
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
            except Exception:
                print(f"[NAMENODE] ⚠️  No se pudo detectar la IP local")
        
        # Extraer SOLO IPs únicas del DNS, excluyendo la IP local
        all_resolved_ips = set()
        for info in addr_info:
            ip = info[4][0]  # info[4] es (ip, port) - solo queremos la IP
            all_resolved_ips.add(ip)
        
        # Filtrar: incluir todas las IPs excepto la propia
        for ip in all_resolved_ips:
            if ip != local_ip:
                discovered_ips.append(ip)
        
        if discovered_ips:
            print(f"[NAMENODE] 🔍 DNS descubrió {len(discovered_ips)} peers (IP local: {local_ip}, peers: {sorted(discovered_ips)})")
        elif local_ip:
            print(f"[NAMENODE] 🔍 DNS solo encontró la IP local {local_ip}, no hay otros peers")
        else:
            print(f"[NAMENODE] 🔍 DNS no descubrió peers")
        
    except socket.gaierror as e:
        print(f"[NAMENODE] ⚠️  Error DNS resolviendo '{NAMENODE_SERVICE}': {e}")
    except Exception as e:
        print(f"[NAMENODE] ⚠️  Error inesperado en descubrimiento DNS: {e}")
    
    return discovered_ips


def refresh_peers_from_dns():
    """
    Actualiza la lista de peers usando DNS de Docker.
    Reemplaza la lista actual con los peers descubiertos via DNS (solo IPs).
    """
    global cluster_state
    
    # 🔍 DEBUG: Inicio de descubrimiento
    print(f"[DEBUG] 🔍 ========== REFRESH PEERS FROM DNS ==========")
    
    # Obtener IP local para filtrar
    local_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"[DEBUG] 🔍 IP local detectada: {local_ip}")
    except Exception as e:
        print(f"[DEBUG] ⚠️  Error detectando IP local (método 1): {e}")
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            print(f"[DEBUG] 🔍 IP local detectada (método 2): {local_ip}")
        except Exception as e2:
            print(f"[DEBUG] ⚠️  Error detectando IP local (método 2): {e2}")
            pass
    
    # Descubrir peers via DNS (ya viene filtrado, solo IPs)
    print(f"[DEBUG] 🔍 Llamando discover_peers_dns()...")
    dns_peers = discover_peers_dns()
    print(f"[DEBUG] 🔍 DNS retornó {len(dns_peers)} peers: {dns_peers}")
    
    with cluster_lock:
        # Obtener identificadores locales para filtrado completo
        current_node_id = cluster_state["node_id"]
        
        # Crear set con todas las variaciones del nodo local para filtrar
        local_identifiers = {local_ip} if local_ip else set()
        
        # Agregar hostname y sus variaciones
        try:
            hostname = socket.gethostname()
            local_identifiers.add(hostname)
            local_identifiers.add(socket.gethostbyname(hostname))
        except Exception:
            pass
        
        # Agregar todas las variaciones del node_id
        local_identifiers.add(current_node_id)
        local_identifiers.add(f"tbfs-{current_node_id}")
        local_identifiers.add(current_node_id.replace("tbfs-", ""))
        if "namenode" in current_node_id:
            local_identifiers.add(current_node_id.replace("namenode-", ""))
            local_identifiers.add(f"namenode-{current_node_id.replace('namenode-', '')}")
        
        # Limpiar la lista actual: remover todo lo que no sea una IP válida de peers DNS
        # O mejor: reemplazar completamente con los peers descubiertos via DNS
        old_peers = cluster_state["peers"].copy()
        
        # Filtrar peers DNS para asegurar que NO incluimos NINGUNA variación del nodo local
        valid_dns_peers = [ip for ip in dns_peers if ip not in local_identifiers]
        
        # DOBLE VERIFICACIÓN: Asegurar que el node_id actual no esté en la lista final
        valid_dns_peers = [p for p in valid_dns_peers if p not in local_identifiers]
        
        # Reemplazar lista de peers con los descubiertos via DNS (solo IPs, sin este nodo)
        cluster_state["peers"] = valid_dns_peers.copy()
        
        # Inicializar estado de los nuevos peers
        for peer_ip in valid_dns_peers:
            if peer_ip not in cluster_state["peer_status"]:
                cluster_state["peer_status"][peer_ip] = {
                    "last_seen": 0.0,
                    "status": "unknown"
                }
        
        # Remover duplicados (por si acaso)
        cluster_state["peers"] = list(set(cluster_state["peers"]))
        
        # Log de cambios
        added = set(valid_dns_peers) - set(old_peers)
        removed = set(old_peers) - set(valid_dns_peers)
        
        # 🔍 DEBUG: Mostrar cambios detallados
        print(f"[DEBUG] 🔍 Peers ANTES: {old_peers}")
        print(f"[DEBUG] 🔍 Peers DESPUÉS: {valid_dns_peers}")
        print(f"[DEBUG] 🔍 Agregados: {added}")
        print(f"[DEBUG] 🔍 Removidos: {removed}")
        
        if added:
            print(f"[NAMENODE] 🆕 Peers agregados via DNS: {sorted(added)}")
        if removed:
            print(f"[NAMENODE] 🗑️  Peers removidos (ya no en DNS o filtrados): {sorted(removed)}")
        
        print(f"[NAMENODE] 📋 Lista final de peers (solo IPs): {sorted(cluster_state['peers'])}")
        print(f"[DEBUG] 🔍 ============================================================")


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
    Actualiza el estado de un peer basado en si está vivo o no.
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
            # 🔍 DEBUG: Nuevo peer detectado
            print(f"[DEBUG] 🆕 Nuevo peer detectado: {peer_id} (status=unknown)")
        
        peer_info = cluster_state["peer_status"][peer_id]
        previous_status = peer_info["status"]
        
        if alive:
            peer_info["last_seen"] = time.time()
            # 🔍 DEBUG: Actualización de estado
            print(f"[DEBUG] ✅ Peer {peer_id}: {previous_status} -> alive")
            
            # Detectar reunificación: si el peer estaba "dead" o "suspected" y ahora está "alive"
            if previous_status in ["dead", "suspected"]:
                print(f"[NAMENODE] 🔄 Peer {peer_id} reunificado: {previous_status} -> alive")
                reunited_peers.append(peer_id)
            peer_info["status"] = "alive"
        else:
            # Si ha pasado mucho tiempo sin ver al peer, marcarlo como suspected o dead
            time_since_seen = time.time() - peer_info["last_seen"]
            PEER_FAILURE_TIMEOUT = 30  # 30 segundos
            if time_since_seen > PEER_FAILURE_TIMEOUT:
                if peer_info["status"] == "alive":
                    peer_info["status"] = "suspected"
                    print(f"[NAMENODE] Peer {peer_id} marcado como suspected (sin contacto por {time_since_seen:.1f}s)")
                elif peer_info["status"] == "suspected" and time_since_seen > (PEER_FAILURE_TIMEOUT * 2):
                    peer_info["status"] = "dead"
                    print(f"[NAMENODE] Peer {peer_id} marcado como dead (sin contacto por {time_since_seen:.1f}s)")
    
    # Disparar reconciliación si se detectó reunificación
    if reunited_peers:
        print(f"[NAMENODE] 🔄 ========== DETECCIÓN DE REUNIFICACIÓN ==========")
        print(f"[NAMENODE] 🔄 Peers reunificados detectados: {reunited_peers}")
        print(f"[NAMENODE] 🔄 Timestamp: {datetime.now().isoformat()}")
        print(f"[NAMENODE] 🔄 Disparando reconciliación en hilo separado...")
        threading.Thread(target=trigger_reconciliation, args=(reunited_peers,), daemon=True).start()


def trigger_reconciliation(reunited_peers: List[str]):
    """
    Fase 3: Dispara el proceso de reconciliación cuando se detecta reunificación.
    
    Esta función será implementada en la Fase 4, por ahora solo registra el evento.
    
    Args:
        reunited_peers: Lista de peer IDs que han vuelto a estar disponibles
    """
    # 🔍 DEBUG: Confirmar que se llamó a trigger_reconciliation
    print(f"[DEBUG] 🔔 trigger_reconciliation() LLAMADA con peers: {reunited_peers}")
    
    if not reunited_peers:
        print(f"[DEBUG] ⚠️  trigger_reconciliation() - Lista de peers vacía, abortando")
        return
    
    with cluster_lock:
        if cluster_state["reconciliation_in_progress"]:
            print(f"[NAMENODE] FASE 3: Reconciliación ya en progreso, ignorando nueva detección")
            print(f"[DEBUG] ⚠️  Reconciliación ya en progreso, abortando")
            return
        
        cluster_state["reconciliation_in_progress"] = True
        print(f"[DEBUG] ✅ Flag reconciliation_in_progress activado")
    
    try:
        start_time = time.time()
        print(f"[NAMENODE] 🔄 ========== INICIANDO RECONCILIACIÓN ==========")
        print(f"[DEBUG] 🚀 RECONCILIACIÓN INICIADA - Este log confirma que perform_full_reconciliation() se ejecutará")
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
    elif operation.operation == "sync_file_replicas":
        # Para sincronización de réplicas, usar timestamp para identificar la versión
        return f"sync_replicas:{operation.timestamp}"
    elif operation.operation == "sync_datanodes":
        # Para sincronización de datanodes, usar timestamp para identificar la versión
        return f"sync_datanodes:{operation.timestamp}"
    else:
        # Clave genérica: operación + datos serializados
        return f"{operation.operation}:{str(operation.data)}"


def get_operation_sort_key(operation: OperationLog, node_id: str = None) -> tuple:
    """
    Genera una clave de ordenamiento determinística para operaciones.
    Usa (term, timestamp, node_id_hash) para garantizar orden consistente entre todos los nodos.
    
    Args:
        operation: Operación a ordenar
        node_id: ID del nodo que generó la operación (opcional, se obtiene de operation.data si está disponible)
    
    Returns:
        Tupla (term, timestamp, node_id_hash) para ordenamiento
    """
    if node_id is None:
        # Intentar obtener node_id de los datos de la operación o usar un valor por defecto
        node_id = operation.data.get("node_id", operation.data.get("user_id", "unknown"))
    
    # Crear hash del node_id para ordenamiento determinístico
    import hashlib
    node_id_hash = int(hashlib.md5(node_id.encode()).hexdigest()[:8], 16)
    
    # Ordenar por: term (primero), timestamp (segundo), node_id_hash (tercero para determinismo)
    return (operation.term, operation.timestamp, node_id_hash)


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
            file_hash = operation_data.get("hash")  # NUEVO: Hash como identificador único
            file_name = operation_data.get("name", "N/A")
            file_id_to_delete = operation_data.get("file_id")
            
            # 🔍 DEBUG: Información de la operación de borrado
            print(f"[DEBUG] 🗑️  Operación delete_file recibida: hash={file_hash[:16] if file_hash else 'N/A'}..., name='{file_name}', file_id={file_id_to_delete}")
            
            # ESTRATEGIA DE BORRADO (para evitar colisiones de file_id):
            # 1. Si hay hash, buscar y borrar por hash (identificador único global)
            # 2. Si no hay hash pero hay nombre, buscar por nombre
            # 3. Como último recurso, usar file_id (puede causar inconsistencias entre particiones)
            
            deleted = False
            actual_file_id = None
            
            if file_hash:
                # PRIORIDAD 1: Buscar por hash (identificador único)
                print(f"[DEBUG] 🔍 Buscando archivo por hash: {file_hash[:16]}...")
                
                try:
                    from namenode.database import get_db_path, get_connection, close_connection
                    from namenode.rw_lock import ReadLock
                    from namenode.database import metadata_rw_lock
                    
                    db_path = get_db_path(node_id)
                    
                    with ReadLock(metadata_rw_lock):
                        conn, cursor = get_connection(db_path=db_path, node_id=node_id)
                        try:
                            cursor.execute("SELECT id, name FROM files WHERE hash = ?", (file_hash,))
                            row = cursor.fetchone()
                            if row:
                                actual_file_id = row[0]
                                actual_name = row[1]
                                print(f"[DEBUG] ✅ Archivo encontrado por hash: ID={actual_file_id}, nombre='{actual_name}'")
                            else:
                                print(f"[DEBUG] ⚠️  Archivo con hash {file_hash[:16]}... NO encontrado")
                        finally:
                            close_connection(conn)
                
                except Exception as e:
                    print(f"[DEBUG] ⚠️  Error buscando por hash: {e}")
            
            elif file_name != "N/A":
                # PRIORIDAD 2: Buscar por nombre (si no hay hash)
                print(f"[DEBUG] 🔍 Buscando archivo por nombre: '{file_name}'...")
                
                try:
                    from namenode.manager import query_files
                    user_id = operation_data.get("user_id", "system")
                    
                    # Buscar archivos con cualquier tag del mismo usuario
                    all_files = query_files(query_tags=[], node_id=node_id, user_id=user_id)
                    
                    for fid, fname, tags in all_files:
                        if fname == file_name:
                            actual_file_id = fid
                            print(f"[DEBUG] ✅ Archivo encontrado por nombre: ID={actual_file_id}")
                            break
                    
                    if not actual_file_id:
                        print(f"[DEBUG] ⚠️  Archivo '{file_name}' NO encontrado por nombre")
                
                except Exception as e:
                    print(f"[DEBUG] ⚠️  Error buscando por nombre: {e}")
            
            # PRIORIDAD 3: Usar file_id (fallback, puede ser incorrecto entre particiones)
            if not actual_file_id:
                actual_file_id = file_id_to_delete
                print(f"[DEBUG] ⚠️  Usando file_id como fallback: {actual_file_id} (puede causar inconsistencias)")
            
            # Borrar usando el file_id correcto
            if actual_file_id:
                # Verificar antes de borrar
                try:
                    from namenode.manager import get_file_by_id
                    file_info = get_file_by_id(actual_file_id, node_id=node_id)
                    if file_info:
                        print(f"[DEBUG] 🗑️  Confirmado: Borrando archivo ID={actual_file_id}, nombre='{file_info.get('name')}', hash={file_info.get('hash', 'N/A')[:16]}...")
                    else:
                        print(f"[DEBUG] ⚠️  Archivo ID={actual_file_id} NO existe, saltando borrado")
                        return
                except Exception as e:
                    print(f"[DEBUG] ⚠️  Error verificando archivo: {e}")
                
                deleted = delete_file_metadata(
                    actual_file_id,
                    node_id=node_id,
                    term=operation.term
                )
                
                # 🔍 DEBUG: Resultado del borrado
                if deleted:
                    print(f"[DEBUG] ✅ Archivo ID={actual_file_id} borrado exitosamente")
                else:
                    print(f"[DEBUG] ⚠️  Archivo ID={actual_file_id} NO se pudo borrar (no existía)")
            else:
                print(f"[DEBUG] ❌ No se pudo determinar el archivo a borrar (sin hash, nombre ni file_id válido)")
        
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


def detect_multiple_leaders(peers: List[str]) -> List[Dict]:
    """
    Detecta si hay múltiples líderes activos en el clúster.
    Deduplica líderes basándose en el node_id reportado por el endpoint.
    
    Returns:
        Lista de diccionarios con información de líderes detectados:
        [{"node_id": str, "term": int, "timestamp": float}, ...]
    """
    leaders = []
    leaders_by_id = {}  # Deduplicar por node_id reportado
    
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.get(f"{peer_url}/", timeout=3)
            if response.status_code == 200:
                peer_data = response.json()
                if peer_data.get("is_leader", False):
                    # Usar el node_id que el peer reporta, no el identificador usado para contactarlo
                    reported_node_id = peer_data.get("node_id", peer)
                    
                    # 🔍 DEBUG: Mostrar qué peer y qué reporta
                    print(f"[DEBUG] 🔍 Peer {peer} reporta: node_id='{reported_node_id}', is_leader={peer_data.get('is_leader')}, term={peer_data.get('term')}")
                    
                    # Deduplicar por node_id reportado
                    if reported_node_id not in leaders_by_id:
                        leaders_by_id[reported_node_id] = {
                            "node_id": reported_node_id,  # Usar el ID reportado, no el peer
                            "term": peer_data.get("term", 0),
                            "timestamp": time.time()
                        }
                    else:
                        print(f"[DEBUG] ⏭️  Líder duplicado detectado y filtrado: {reported_node_id}")
        except Exception as e:
            # Peer no responde, no es líder
            pass
    
    # También verificar si este nodo es líder
    with cluster_lock:
        current_node_id = cluster_state["node_id"]
        if cluster_state.get("is_leader", False):
            # Solo agregar si no está ya en la lista (deduplicación)
            if current_node_id not in leaders_by_id:
                leaders_by_id[current_node_id] = {
                    "node_id": current_node_id,
                    "term": cluster_state.get("term", 0),
                    "timestamp": time.time()
                }
                print(f"[DEBUG] ✅ Este nodo agregado como líder: {current_node_id}")
            else:
                print(f"[DEBUG] ⏭️  Este nodo ya está en lista de líderes, no duplicar")
    
    # Convertir dict a lista
    leaders = list(leaders_by_id.values())
    
    # 🔍 DEBUG: Mostrar líderes finales después de deduplicación
    print(f"[DEBUG] 🔍 Líderes únicos detectados: {len(leaders)}")
    for leader in leaders:
        print(f"[DEBUG] 🔍   - {leader['node_id']}: term={leader['term']}")
    
    return leaders


def calculate_operations_checksum(operations: List[OperationLog]) -> str:
    """
    Calcula un checksum del orden y contenido de operaciones para verificar consenso.
    
    Args:
        operations: Lista ordenada de operaciones
    
    Returns:
        Hash SHA256 del orden de operaciones
    """
    import hashlib
    import json
    
    # Crear representación serializable de operaciones
    ops_data = []
    for op in operations:
        ops_data.append({
            "operation": op.operation,
            "term": op.term,
            "timestamp": op.timestamp,
            "data_key": get_operation_key(op)
        })
    
    # Calcular hash
    ops_json = json.dumps(ops_data, sort_keys=True)
    return hashlib.sha256(ops_json.encode()).hexdigest()


def propose_operation_order(operations: List[OperationLog], peers: List[str], 
                            coordinator_id: str, term: int) -> Dict:
    """
    Fase 1: PROPOSE - El coordinador propone un orden de operaciones a los peers.
    
    Args:
        operations: Lista ordenada de operaciones propuestas
        peers: Lista de peers a quienes proponer
        coordinator_id: ID del coordinador que propone
        term: Término de consenso
    
    Returns:
        Dict con resultados de la propuesta {peer_id: {"accepted": bool, "checksum": str}}
    """
    print(f"[NAMENODE] 🤝 [CONSENSUS] Fase 1: PROPOSE - Proponiendo orden de {len(operations)} operaciones")
    print(f"[DEBUG] 🔍 PROPOSE: Coordinador={coordinator_id}, Term={term}, Peers={peers}")
    
    # 🔍 DEBUG: Mostrar operaciones a proponer
    print(f"[DEBUG] 📋 Operaciones a proponer ({len(operations)}):")
    for idx, op in enumerate(operations[:20], 1):  # Primeras 20
        op_name = op.data.get('name', 'N/A') if op.operation in ['add_file', 'delete_file'] else 'N/A'
        print(f"[DEBUG] 📋   [{idx}] {op.operation} | '{op_name}' | term={op.term} | t={op.timestamp:.2f}")
    if len(operations) > 20:
        print(f"[DEBUG] 📋   ... y {len(operations) - 20} operaciones más")
    
    # Calcular checksum del orden propuesto
    proposed_checksum = calculate_operations_checksum(operations)
    print(f"[NAMENODE] 🤝 [CONSENSUS] Checksum propuesto: {proposed_checksum[:16]}...")
    print(f"[DEBUG] 🔍 Checksum completo: {proposed_checksum}")
    
    # Serializar operaciones para envío
    operations_data = []
    for op in operations:
        operations_data.append({
            "operation": op.operation,
            "data": op.data,
            "term": op.term,
            "timestamp": op.timestamp
        })
    
    # Obtener token de servicio
    try:
        service_token = generate_service_token(coordinator_id, "service")
    except Exception:
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    # Proponer a cada peer
    results = {}
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/consensus-propose",
                json={
                    "coordinator_id": coordinator_id,
                    "term": term,
                    "operations": operations_data,
                    "checksum": proposed_checksum,
                    "operation_count": len(operations)
                },
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                results[peer] = {
                    "accepted": result.get("accepted", False),
                    "checksum": result.get("checksum", ""),
                    "message": result.get("message", "")
                }
                status = "✅" if result.get("accepted") else "❌"
                print(f"[NAMENODE] 🤝 [CONSENSUS] {status} Respuesta de {peer}: {result.get('message')}")
            else:
                results[peer] = {"accepted": False, "error": f"HTTP {response.status_code}"}
                print(f"[NAMENODE] 🤝 [CONSENSUS] ❌ Error de {peer}: HTTP {response.status_code}")
        
        except Exception as e:
            results[peer] = {"accepted": False, "error": str(e)}
            print(f"[NAMENODE] 🤝 [CONSENSUS] ❌ Error contactando {peer}: {e}")
    
    # Calcular resultado del consenso
    accepted_count = sum(1 for r in results.values() if r.get("accepted", False))
    total = len(peers) + 1  # +1 por el coordinador
    quorum = (total // 2) + 1
    
    consensus_reached = (accepted_count + 1) >= quorum  # +1 por el coordinador
    
    print(f"[NAMENODE] 🤝 [CONSENSUS] Resultado PROPOSE: {accepted_count + 1}/{total} aceptaron (quorum: {quorum})")
    print(f"[NAMENODE] 🤝 [CONSENSUS] Consenso alcanzado: {consensus_reached}")
    
    return {
        "consensus_reached": consensus_reached,
        "accepted_count": accepted_count + 1,
        "total_nodes": total,
        "quorum": quorum,
        "proposed_checksum": proposed_checksum,
        "peer_results": results
    }


def commit_consensus_order(peers: List[str], coordinator_id: str, 
                           term: int, checksum: str) -> bool:
    """
    Fase 3: COMMIT - Notifica a los peers que el consenso fue alcanzado y deben aplicar el orden.
    
    Args:
        peers: Lista de peers a notificar
        coordinator_id: ID del coordinador
        term: Término de consenso
        checksum: Checksum del orden acordado
    
    Returns:
        True si la mayoría confirmó la aplicación
    """
    print(f"[NAMENODE] 🤝 [CONSENSUS] Fase 3: COMMIT - Notificando consenso alcanzado")
    
    # Obtener token de servicio
    try:
        service_token = generate_service_token(coordinator_id, "service")
    except Exception:
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    committed_count = 0
    for peer in peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.post(
                f"{peer_url}/internal/consensus-commit",
                json={
                    "coordinator_id": coordinator_id,
                    "term": term,
                    "checksum": checksum
                },
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("committed", False):
                    committed_count += 1
                    print(f"[NAMENODE] 🤝 [CONSENSUS] ✅ {peer} confirmó commit")
                else:
                    print(f"[NAMENODE] 🤝 [CONSENSUS] ⚠️  {peer} no confirmó commit: {result.get('message')}")
            else:
                print(f"[NAMENODE] 🤝 [CONSENSUS] ❌ Error commit en {peer}: HTTP {response.status_code}")
        
        except Exception as e:
            print(f"[NAMENODE] 🤝 [CONSENSUS] ❌ Error contactando {peer} para commit: {e}")
    
    total = len(peers) + 1
    quorum = (total // 2) + 1
    consensus_committed = (committed_count + 1) >= quorum
    
    print(f"[NAMENODE] 🤝 [CONSENSUS] Resultado COMMIT: {committed_count + 1}/{total} confirmaron (quorum: {quorum})")
    
    return consensus_committed


def merge_and_order_operations(all_logs: Dict[str, List[OperationLog]]) -> List[OperationLog]:
    """
    Fusiona logs de múltiples nodos y los ordena de forma determinística.
    Usa un ordenamiento causal robusto: (term, timestamp, node_id_hash).
    
    Args:
        all_logs: Diccionario {node_id: [OperationLog, ...]}
    
    Returns:
        Lista de operaciones ordenadas de forma determinística
    """
    # Recopilar todas las operaciones con su origen
    all_operations = []
    seen_operations = set()  # Para evitar duplicados
    
    for node_id, log in all_logs.items():
        for op in log:
            # Crear clave única para detectar duplicados
            op_key = (op.operation, op.term, op.timestamp, get_operation_key(op))
            
            if op_key not in seen_operations:
                seen_operations.add(op_key)
                all_operations.append((op, node_id))
    
    # Ordenar usando la clave determinística
    sorted_operations = sorted(
        all_operations,
        key=lambda x: get_operation_sort_key(x[0], x[1])
    )
    
    # Extraer solo las operaciones (sin node_id)
    return [op for op, _ in sorted_operations]


def perform_full_reconciliation(reunited_peers: List[str]):
    """
    Realiza reconciliación completa después de particionamiento con consenso entre líderes.
    
    Proceso mejorado:
    1. Obtener logs de operaciones de todos los peers reunificados
    2. Detectar múltiples líderes activos
    3. Fusionar y ordenar todas las operaciones de forma determinística
    4. Aplicar operaciones en orden causal (term + timestamp + node_id)
    5. Resolver conflictos con orden causal robusto
    6. Verificar integridad de datos físicos
    7. Re-replicar archivos faltantes
    
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
    
    # Paso 0: Detectar múltiples líderes activos
    print(f"[NAMENODE] 🔄 [PASO 0] Detectando múltiples líderes activos...")
    # 🔍 DEBUG: Mostrar peers a verificar
    print(f"[DEBUG] 🔍 [PASO 0] Verificando líderes en peers reunificados: {reunited_peers}")
    print(f"[DEBUG] 🔍 [PASO 0] Este nodo: {current_node_id}")
    
    # NO incluir current_node_id en la lista - la función detect_multiple_leaders ya lo verifica internamente
    detected_leaders = detect_multiple_leaders(reunited_peers)
    
    if len(detected_leaders) > 1:
        print(f"[NAMENODE] 🔄 [PASO 0] ⚠️  MÚLTIPLES LÍDERES DETECTADOS: {len(detected_leaders)}")
        for leader in detected_leaders:
            print(f"[NAMENODE] 🔄 [PASO 0]   - Líder: {leader['node_id']}, Term: {leader['term']}")
        print(f"[NAMENODE] 🔄 [PASO 0] Se requerirá consenso para ordenar operaciones")
    else:
        print(f"[NAMENODE] 🔄 [PASO 0] ✅ Un solo líder detectado o ninguno")
    
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
    
    # 🔍 DEBUG: Mostrar operaciones de archivos en log local ANTES de reconciliar
    print(f"[DEBUG] 📝 ========== LOG LOCAL ANTES DE RECONCILIACIÓN ==========")
    file_ops_local = [op for op in local_log if op.operation in ['add_file', 'delete_file']]
    print(f"[DEBUG] 📝 Operaciones de archivos en log local: {len(file_ops_local)}")
    for op in file_ops_local:
        op_name = op.data.get('name', 'N/A')
        print(f"[DEBUG] 📝   - {op.operation}: '{op_name}' | term={op.term} | t={op.timestamp:.2f}")
    print(f"[DEBUG] 📝 ==========================================================")
    
    # Paso 2.5: Incluir log local en la colección de logs
    print(f"[NAMENODE] 🔄 [PASO 2.5] Incluyendo log local en fusión...")
    all_logs = peer_logs.copy()
    all_logs[current_node_id] = local_log
    print(f"[NAMENODE] 🔄 [PASO 2.5] Total de logs a fusionar: {len(all_logs)}")
    
    # Paso 3: Fusionar y ordenar todas las operaciones de forma determinística
    print(f"[NAMENODE] 🔄 [PASO 3] Fusionando y ordenando operaciones de forma determinística...")
    merged_operations = merge_and_order_operations(all_logs)
    print(f"[NAMENODE] 🔄 [PASO 3] ✅ Total de operaciones únicas después de fusión: {len(merged_operations)}")
    
    # 🔍 DEBUG: Mostrar TODAS las operaciones fusionadas
    print(f"[DEBUG] 📋 ========== OPERACIONES FUSIONADAS ==========")
    for idx, op in enumerate(merged_operations, 1):
        op_name = op.data.get('name', 'N/A') if op.operation in ['add_file', 'delete_file'] else str(op.data)[:50]
        op_hash = op.data.get('hash', 'N/A')[:16] if op.operation in ['add_file', 'delete_file'] else 'N/A'
        print(f"[DEBUG] 📋 [{idx}/{len(merged_operations)}] {op.operation} | archivo='{op_name}' | hash={op_hash}... | term={op.term} | t={op.timestamp:.2f}")
    print(f"[DEBUG] 📋 ==================================================")
    
    # 🔍 ANÁLISIS: Detectar secuencias add→delete del mismo archivo
    print(f"[DEBUG] 🔍 ========== ANÁLISIS DE SECUENCIAS ADD→DELETE ==========")
    file_operations = {}  # hash → [operaciones]
    
    for op in merged_operations:
        if op.operation in ['add_file', 'delete_file']:
            op_hash = op.data.get('hash', '')
            op_name = op.data.get('name', 'N/A')
            
            # Usar hash si está disponible, sino nombre
            key = op_hash if op_hash else op_name
            
            if key and key != 'N/A':
                if key not in file_operations:
                    file_operations[key] = []
                file_operations[key].append(op)
    
    # Detectar archivos con múltiples operaciones (add y delete)
    for file_key, ops in file_operations.items():
        if len(ops) > 1:
            add_ops = [o for o in ops if o.operation == 'add_file']
            delete_ops = [o for o in ops if o.operation == 'delete_file']
            
            if add_ops and delete_ops:
                print(f"[DEBUG] 🔍 Secuencia ADD→DELETE detectada para: {file_key[:30]}...")
                print(f"[DEBUG] 🔍   - {len(add_ops)} add_file: terms={[o.term for o in add_ops]}, timestamps={[f'{o.timestamp:.2f}' for o in add_ops]}")
                print(f"[DEBUG] 🔍   - {len(delete_ops)} delete_file: terms={[o.term for o in delete_ops]}, timestamps={[f'{o.timestamp:.2f}' for o in delete_ops]}")
                
                # Verificar si el delete es posterior al último add
                last_add = max(add_ops, key=lambda x: (x.term, x.timestamp))
                last_delete = max(delete_ops, key=lambda x: (x.term, x.timestamp))
                
                if (last_delete.term, last_delete.timestamp) > (last_add.term, last_add.timestamp):
                    print(f"[DEBUG] ✅ Delete es posterior al último add → Archivo será borrado finalmente")
                else:
                    print(f"[DEBUG] ⚠️  Add es posterior al delete → Archivo sobrevivirá (posible resurrección)")
    
    print(f"[DEBUG] 🔍 ============================================================")
    
    # Paso 3.5: CONSENSO EXPLÍCITO si hay múltiples líderes
    if len(detected_leaders) > 1:
        print(f"[NAMENODE] 🤝 [PASO 3.5] ========== INICIANDO PROTOCOLO DE CONSENSO ==========")
        print(f"[NAMENODE] 🤝 [PASO 3.5] Múltiples líderes detectados, requiere consenso explícito")
        
        # 🔍 DEBUG: Mostrar líderes detectados
        print(f"[DEBUG] 🔍 Líderes detectados:")
        for leader in detected_leaders:
            print(f"[DEBUG] 🔍   - {leader['node_id']}: term={leader['term']}, is_leader={leader.get('is_leader', 'N/A')}")
        
        # Determinar coordinador (líder con mayor term, o menor node_id si empate)
        coordinator = max(detected_leaders, key=lambda x: (x['term'], -int(hashlib.md5(x['node_id'].encode()).hexdigest()[:8], 16)))
        coordinator_id = coordinator['node_id']
        consensus_term = coordinator['term']
        
        print(f"[NAMENODE] 🤝 [PASO 3.5] Coordinador seleccionado: {coordinator_id} (term: {consensus_term})")
        print(f"[DEBUG] 🔍 Este nodo: {current_node_id}, Es coordinador: {coordinator_id == current_node_id}")
        
        is_coordinator = (coordinator_id == current_node_id)
        
        if is_coordinator:
            print(f"[NAMENODE] 🤝 [PASO 3.5] Este nodo ES el coordinador, iniciando protocolo 3PC...")
            
            # Calcular checksum del orden propuesto
            proposed_checksum = calculate_operations_checksum(merged_operations)
            print(f"[NAMENODE] 🤝 [PASO 3.5] Checksum del orden propuesto: {proposed_checksum[:16]}...")
            
            # Obtener otros líderes (excluir este nodo)
            other_leaders = [l['node_id'] for l in detected_leaders if l['node_id'] != current_node_id]
            
            # FASE 1: PROPOSE
            propose_result = propose_operation_order(
                operations=merged_operations,
                peers=other_leaders + [p for p in reunited_peers if p not in other_leaders],  # Incluir todos los peers
                coordinator_id=coordinator_id,
                term=consensus_term
            )
            
            if propose_result["consensus_reached"]:
                print(f"[NAMENODE] 🤝 [PASO 3.5] ✅ CONSENSO ALCANZADO en fase PROPOSE")
                print(f"[NAMENODE] 🤝 [PASO 3.5] Aplicando orden acordado...")
                
                # Guardar checksum del consenso para verificación
                with cluster_lock:
                    cluster_state["last_consensus_checksum"] = proposed_checksum
                    cluster_state["last_consensus_term"] = consensus_term
                
                # FASE 3: COMMIT - Notificar a todos que apliquen el orden
                commit_success = commit_consensus_order(
                    peers=other_leaders + [p for p in reunited_peers if p not in other_leaders],
                    coordinator_id=coordinator_id,
                    term=consensus_term,
                    checksum=proposed_checksum
                )
                
                if commit_success:
                    print(f"[NAMENODE] 🤝 [PASO 3.5] ✅ COMMIT confirmado por la mayoría")
                else:
                    print(f"[NAMENODE] 🤝 [PASO 3.5] ⚠️  WARNING: COMMIT no confirmado por todos")
                
            else:
                print(f"[NAMENODE] 🤝 [PASO 3.5] ❌ CONSENSO NO ALCANZADO")
                print(f"[NAMENODE] 🤝 [PASO 3.5] Solo {propose_result['accepted_count']}/{propose_result['total_nodes']} nodos aceptaron")
                print(f"[NAMENODE] 🤝 [PASO 3.5] Continuando con aplicación local (sin garantía de consenso)")
        
        else:
            print(f"[NAMENODE] 🤝 [PASO 3.5] Este nodo NO es el coordinador, esperando propuesta de {coordinator_id}...")
            # Los followers esperarán la propuesta vía el endpoint /internal/consensus-propose
            # y aplicarán las operaciones cuando reciban el COMMIT
            
            # IMPORTANTE: Esperar a recibir la propuesta y el commit del coordinador
            # Timeout de 30 segundos para recibir el consenso
            print(f"[NAMENODE] 🤝 [PASO 3.5] ⏳ Esperando PROPOSE y COMMIT del coordinador (timeout: 30s)...")
            print(f"[DEBUG] 🔍 Coordinador: {coordinator_id}, Este nodo: {current_node_id}")
            
            # Calcular checksum local para comparar
            local_checksum = calculate_operations_checksum(merged_operations)
            print(f"[DEBUG] 🔍 Checksum local de operaciones fusionadas: {local_checksum[:16]}...")
            
            # Esperar hasta 30 segundos a que llegue el COMMIT
            wait_start = time.time()
            consensus_applied = False
            checks_count = 0
            
            while (time.time() - wait_start) < 30:
                checks_count += 1
                elapsed = time.time() - wait_start
                
                # Verificar si ya se aplicó el consenso
                with _consensus_lock:
                    proposals_found = len(_consensus_proposals)
                    
                    # 🔍 DEBUG: Mostrar propuestas pendientes cada 5 segundos
                    if checks_count % 10 == 1:  # Cada 5 segundos (0.5s * 10)
                        print(f"[DEBUG] ⏳ Esperando consenso... ({elapsed:.1f}s) | Propuestas pendientes: {proposals_found}")
                        if proposals_found > 0:
                            for prop_checksum, proposal in _consensus_proposals.items():
                                print(f"[DEBUG]    - Checksum: {prop_checksum[:16]}... | Coordinador: {proposal.get('coordinator_id')} | Aplicado: {proposal.get('applied', False)}")
                    
                    # Buscar si hay una propuesta con nuestro checksum local o cualquier propuesta del coordinador
                    for prop_checksum, proposal in _consensus_proposals.items():
                        if (proposal.get("coordinator_id") == coordinator_id and 
                            proposal.get("term") == consensus_term and
                            proposal.get("applied", False)):
                            consensus_applied = True
                            print(f"[NAMENODE] 🤝 [PASO 3.5] ✅ Consenso recibido y aplicado vía endpoint")
                            print(f"[DEBUG] ✅ Propuesta aplicada: checksum={prop_checksum[:16]}... | ops={len(proposal.get('operations', []))}")
                            break
                
                if consensus_applied:
                    break
                
                # Esperar 0.5 segundos antes de verificar de nuevo
                time.sleep(0.5)
            
            if not consensus_applied:
                print(f"[NAMENODE] 🤝 [PASO 3.5] ⚠️  TIMEOUT esperando consenso del coordinador ({elapsed:.1f}s)")
                print(f"[DEBUG] ❌ TIMEOUT: Propuestas en memoria: {len(_consensus_proposals)}")
                with _consensus_lock:
                    for prop_checksum, proposal in _consensus_proposals.items():
                        print(f"[DEBUG]    - {prop_checksum[:16]}...: aplicado={proposal.get('applied', False)}")
                print(f"[NAMENODE] 🤝 [PASO 3.5] Continuando con aplicación local (fallback)...")
            else:
                print(f"[NAMENODE] 🤝 [PASO 3.5] ✅ Consenso aplicado exitosamente por el coordinador")
                print(f"[DEBUG] ✅ Recargando log para incluir operaciones del consenso...")
                # Saltar el PASO 5 ya que las operaciones ya fueron aplicadas vía consenso
                # Actualizar índice local para marcar todas como aplicadas
                local_log = load_operation_log(NODE_ID)
                print(f"[DEBUG] 📋 Log recargado: {len(local_log)} operaciones")
                local_op_index = {}
                for op in local_log:
                    op_key = (op.operation, op.term, op.timestamp, get_operation_key(op))
                    local_op_index[op_key] = op
    
    else:
        print(f"[NAMENODE] 🔄 [PASO 3.5] No hay múltiples líderes, consenso no necesario")
    
    # Paso 4: Determinar líder válido (mayor term) y actualizar term si es necesario
    print(f"[NAMENODE] 🔄 [PASO 4] Determinando líder válido (mayor term)...")
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
                print(f"[NAMENODE] 🔄 [PASO 4] ⚠️  Error obteniendo term actual de {peer}: {e}")
            
            print(f"[NAMENODE] 🔄 [PASO 4] Peer {peer}: term máximo = {peer_max_term}")
            if peer_max_term > max_term:
                max_term = peer_max_term
                leader_peer = peer
                print(f"[NAMENODE] 🔄 [PASO 4] ✅ Nuevo líder candidato: {peer} (term {max_term})")
    
    # También verificar term del log local
    if local_log:
        local_max_term = max((op.term for op in local_log), default=current_term)
        if local_max_term > max_term:
            max_term = local_max_term
            leader_peer = current_node_id
    
    print(f"[NAMENODE] 🔄 [PASO 4] ✅ Term máximo encontrado: {max_term}")
    print(f"[NAMENODE] 🔄 [PASO 4] ✅ Líder identificado: {leader_peer or current_node_id}")
    
    # Actualizar term local si es necesario
    with cluster_lock:
        if max_term > cluster_state["term"]:
            old_term = cluster_state["term"]
            cluster_state["term"] = max_term
            if leader_peer and leader_peer != current_node_id:
                cluster_state["leader_id"] = leader_peer
                cluster_state["is_leader"] = False
            print(f"[NAMENODE] 🔄 [PASO 4] ✅ Term actualizado: {old_term} -> {max_term}")
    
    # Paso 5: Aplicar operaciones fusionadas en orden determinístico
    print(f"[NAMENODE] 🔄 [PASO 5] Aplicando operaciones fusionadas en orden causal...")
    
    # Verificar si el consenso ya fue aplicado (evitar duplicación)
    consensus_already_applied = False
    if len(detected_leaders) > 1:
        with cluster_lock:
            last_consensus_term = cluster_state.get("last_consensus_term", -1)
        
        # Si ya se aplicó un consenso con el mismo term, verificar si ya tenemos las operaciones
        if last_consensus_term == consensus_term if 'consensus_term' in locals() else False:
            print(f"[NAMENODE] 🔄 [PASO 5] ℹ️  Consenso ya aplicado previamente (term={last_consensus_term})")
            consensus_already_applied = True
    
    # Crear índice de operaciones locales para verificación rápida
    if 'local_op_index' not in locals():
        local_op_index = {}
        for op in local_log:
            op_key = (op.operation, op.term, op.timestamp, get_operation_key(op))
            local_op_index[op_key] = op
    
    applied_count = 0
    skipped_count = 0
    op_type_counts = {}
    
    for idx, operation in enumerate(merged_operations, 1):
        # 🔍 DEBUG: Log de cada operación procesada
        op_name = operation.data.get('name', 'N/A') if operation.operation in ['add_file', 'delete_file'] else 'N/A'
        
        # Verificar si la operación ya está en local
        op_key = (operation.operation, operation.term, operation.timestamp, get_operation_key(operation))
        
        if op_key in local_op_index:
            # Verificar si es realmente la misma operación
            local_op = local_op_index[op_key]
            timestamp_match = abs(local_op.timestamp - operation.timestamp) < 0.1  # Tolerancia muy pequeña
            term_match = local_op.term == operation.term
            operation_match = local_op.operation == operation.operation
            
            if term_match and timestamp_match and operation_match:
                skipped_count += 1
                # 🔍 DEBUG: Operación saltada
                print(f"[DEBUG] ⏭️  [{idx}/{len(merged_operations)}] SKIP {operation.operation} | archivo='{op_name}' | term={operation.term} | ya existe en local")
                continue
        
        # Aplicar operación
        print(f"[NAMENODE] 🔄 [PASO 5] [{idx}/{len(merged_operations)}] Aplicando operación: {operation.operation} (term {operation.term}, timestamp {operation.timestamp:.2f})")
        print(f"[DEBUG] ✨ [{idx}/{len(merged_operations)}] APLICAR {operation.operation} | archivo='{op_name}' | term={operation.term} | t={operation.timestamp:.2f}")
        
        apply_operation_safely(operation, NODE_ID)
        
        # Guardar operación aplicada en el log persistente
        save_operation_to_log(operation, NODE_ID)
        
        # Agregar al log en memoria
        with log_lock:
            operation_log.append(operation)
        
        applied_count += 1
        op_type_counts[operation.operation] = op_type_counts.get(operation.operation, 0) + 1
        
        # Actualizar índice local
        local_op_index[op_key] = operation
        
        # 🔍 DEBUG: Confirmación
        print(f"[DEBUG] ✅ [{idx}/{len(merged_operations)}] Aplicada y guardada: {operation.operation} | archivo='{op_name}'")
    
    print(f"[NAMENODE] 🔄 [PASO 5] ✅ Resumen aplicación de operaciones:")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Operaciones aplicadas: {applied_count}")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Operaciones saltadas (ya existían): {skipped_count}")
    print(f"[NAMENODE] 🔄 [PASO 5]   - Desglose aplicadas: {op_type_counts}")
    
    # Recargar log local después de aplicar operaciones
    local_log = load_operation_log(NODE_ID)
    print(f"[NAMENODE] 🔄 [PASO 5] ✅ Log local actualizado: {len(local_log)} operaciones")
    
    # Paso 6: Resolver conflictos restantes con orden causal robusto
    print(f"[NAMENODE] 🔄 [PASO 6] Resolviendo conflictos con orden causal robusto...")
    total_conflicts_resolved = 0
    
    # Comparar log local final con cada peer para detectar conflictos
    for peer, peer_log in peer_logs.items():
        comparison = compare_operation_logs(local_log, peer_log)
        conflicts_count = len(comparison["conflicts"])
        
        if conflicts_count > 0:
            print(f"[NAMENODE] 🔄 [PASO 6] Detectados {conflicts_count} conflictos con {peer}")
            
            for conflict in comparison["conflicts"]:
                local_op = conflict.get("local")
                peer_op = conflict.get("peer")
                
                if local_op and peer_op:
                    # Usar orden causal robusto: (term, timestamp, node_id_hash)
                    local_key = get_operation_sort_key(local_op, current_node_id)
                    peer_key = get_operation_sort_key(peer_op, peer)
                    
                    # Si la operación del peer tiene orden causal mayor, aplicarla
                    if peer_key > local_key:
                        print(f"[NAMENODE] 🔄 [PASO 6]   Resolviendo conflicto: {peer_op.operation}")
                        print(f"[NAMENODE] 🔄 [PASO 6]     - Local: term={local_op.term}, ts={local_op.timestamp:.2f}")
                        print(f"[NAMENODE] 🔄 [PASO 6]     - Peer:  term={peer_op.term}, ts={peer_op.timestamp:.2f}")
                        print(f"[NAMENODE] 🔄 [PASO 6]     - Aplicando versión del peer (orden causal mayor)")
                        apply_operation_safely(peer_op, NODE_ID)
                        save_operation_to_log(peer_op, NODE_ID)
                        with log_lock:
                            operation_log.append(peer_op)
                        total_conflicts_resolved += 1
                    else:
                        print(f"[NAMENODE] 🔄 [PASO 6]   Manteniendo versión local (orden causal mayor o igual)")
    
    if total_conflicts_resolved > 0:
        print(f"[NAMENODE] 🔄 [PASO 6] ✅ Total conflictos resueltos: {total_conflicts_resolved}")
    else:
        print(f"[NAMENODE] 🔄 [PASO 6] ✅ No se detectaron conflictos adicionales")
    
    # Paso 7: Verificar integridad de datos físicos y re-replicar si es necesario
    print(f"[NAMENODE] 🔄 [PASO 7] Verificando integridad de réplicas...")
    verify_and_rereplicate_files(NODE_ID)
    
    # Paso 8: Reconciliar tablas operation_states y operation_state_log
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
    
    # Paso 9: Sincronizar term final con todos los namenodes y verificar versiones
    print(f"[NAMENODE] 🔄 [PASO 9] Sincronizando term final con todos los namenodes...")
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
                        print(f"[NAMENODE] 🔄 [PASO 9] ⚠️  Peer {peer} tiene term {peer_term} < {final_term}, necesita actualización")
                        failed_count += 1
                    elif peer_term == final_term:
                        print(f"[NAMENODE] 🔄 [PASO 9] ✅ Peer {peer} ya tiene el term correcto ({final_term})")
                        synced_count += 1
                    else:
                        print(f"[NAMENODE] 🔄 [PASO 9] ⚠️  Peer {peer} tiene term mayor ({peer_term} > {final_term}), debería ejecutar su propia reconciliación")
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
        print(f"[NAMENODE] 🔄 [PASO 9] Como líder, enviando heartbeats a {len(peers_needing_update)} peers para sincronizar term...")
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
                        print(f"[NAMENODE] 🔄 [PASO 9] ✅ Heartbeat enviado a {peer}, term debería actualizarse")
                        synced_count += 1
                        failed_count -= 1
                    else:
                        print(f"[NAMENODE] 🔄 [PASO 9] ⚠️  Error enviando heartbeat a {peer}: HTTP {response.status_code}")
                except Exception as e:
                    print(f"[NAMENODE] 🔄 [PASO 9] ⚠️  Error enviando heartbeat a {peer}: {e}")
            except Exception as e:
                print(f"[NAMENODE] 🔄 [PASO 9] ❌ Error procesando peer {peer}: {e}")
    
    # Verificar versiones en la tabla files
    print(f"[NAMENODE] 🔄 [PASO 9] Verificando campos de versión en tabla files...")
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
                
                print(f"[NAMENODE] 🔄 [PASO 9] Estado de versiones en tabla files:")
                print(f"[NAMENODE] 🔄 [PASO 9]   - Total archivos: {stats['total']}")
                print(f"[NAMENODE] 🔄 [PASO 9]   - Archivos sin version: {stats['null_version']}")
                print(f"[NAMENODE] 🔄 [PASO 9]   - Archivos sin last_modified_term: {stats['null_term']}")
                print(f"[NAMENODE] 🔄 [PASO 9]   - Archivos sin last_modified_timestamp: {stats['null_timestamp']}")
                
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
                        print(f"[NAMENODE] 🔄 [PASO 9] ✅ Corregidos {fixed_count} archivos con campos de versión inválidos")
        finally:
            close_connection(conn)
    except Exception as e:
        print(f"[NAMENODE] 🔄 [PASO 9] ⚠️  Error verificando versiones: {e}")
    
    print(f"[NAMENODE] 🔄 [PASO 9] ✅ Sincronización de term completada: {synced_count} exitosos, {failed_count} con problemas")
    print(f"[NAMENODE] 🔄 [PASO 9] Term final: {final_term} (debe ser el mismo en todos los namenodes después de heartbeats)")
    
    # PASO 10: Fusionar y sincronizar tabla datanodes desde todos los peers
    print(f"[NAMENODE] 🔄 [PASO 10] Fusionando y sincronizando tabla datanodes desde todos los peers...")
    
    with cluster_lock:
        is_leader_now = cluster_state["is_leader"]
    
    # PRIMERO: Recolectar tablas de datanodes de TODOS los peers (incluyendo este nodo)
    print(f"[NAMENODE] 🔄 [PASO 10.1] Recolectando tablas de datanodes de todos los peers...")
    
    all_datanodes_by_peer = {}
    
    # Obtener datanodes locales
    try:
        db_path = get_db_path(NODE_ID)
        from namenode.rw_lock import ReadLock
        from namenode.database import metadata_rw_lock
        
        local_datanodes = []
        with ReadLock(metadata_rw_lock):
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            try:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes
                """)
                for row in cursor.fetchall():
                    local_datanodes.append({
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
            finally:
                close_connection(conn)
        
        all_datanodes_by_peer[current_node_id] = local_datanodes
        print(f"[NAMENODE] 🔄 [PASO 10.1] Datanodes locales: {len(local_datanodes)}")
    except Exception as e:
        print(f"[NAMENODE] ⚠️  [PASO 10.1] Error obteniendo datanodes locales: {e}")
        all_datanodes_by_peer[current_node_id] = []
    
    # Obtener datanodes de cada peer reunificado
    try:
        service_token = generate_service_token(current_node_id, "service")
    except Exception:
        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
    
    for peer in reunited_peers:
        try:
            peer_url = get_peer_url(peer)
            response = requests.get(
                f"{peer_url}/internal/get-datanodes",
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=10
            )
            
            if response.status_code == 200:
                peer_datanodes = response.json().get("datanodes", [])
                all_datanodes_by_peer[peer] = peer_datanodes
                print(f"[NAMENODE] 🔄 [PASO 10.1] Datanodes de {peer}: {len(peer_datanodes)}")
            else:
                print(f"[NAMENODE] ⚠️  [PASO 10.1] Error obteniendo datanodes de {peer}: HTTP {response.status_code}")
                all_datanodes_by_peer[peer] = []
        except Exception as e:
            print(f"[NAMENODE] ⚠️  [PASO 10.1] Error obteniendo datanodes de {peer}: {e}")
            all_datanodes_by_peer[peer] = []
    
    # SEGUNDO: Fusionar tablas de datanodes (tomar la versión más reciente de cada datanode)
    print(f"[NAMENODE] 🔄 [PASO 10.2] Fusionando tablas de datanodes...")
    
    merged_datanodes = {}
    for peer_id, peer_datanodes in all_datanodes_by_peer.items():
        for datanode in peer_datanodes:
            dn_id = datanode["node_id"]
            dn_last_heartbeat = datanode.get("last_heartbeat", 0)
            
            # Si el datanode no existe en merged o tiene heartbeat más reciente, actualizar
            if dn_id not in merged_datanodes:
                merged_datanodes[dn_id] = datanode
                print(f"[NAMENODE] 🔄 [PASO 10.2] Agregado datanode {dn_id} desde {peer_id} (heartbeat={dn_last_heartbeat})")
            else:
                existing_heartbeat = merged_datanodes[dn_id].get("last_heartbeat", 0)
                if dn_last_heartbeat > existing_heartbeat:
                    old_status = merged_datanodes[dn_id].get("status", "unknown")
                    new_status = datanode.get("status", "unknown")
                    merged_datanodes[dn_id] = datanode
                    print(f"[NAMENODE] 🔄 [PASO 10.2] Actualizado datanode {dn_id} desde {peer_id} (heartbeat {existing_heartbeat} -> {dn_last_heartbeat}, status {old_status} -> {new_status})")
    
    final_datanodes = list(merged_datanodes.values())
    print(f"[NAMENODE] 🔄 [PASO 10.2] ✅ Tabla fusionada: {len(final_datanodes)} datanodes")
    
    # Log de datanodes por estado
    active_count = sum(1 for dn in final_datanodes if dn.get("status") == "active")
    inactive_count = sum(1 for dn in final_datanodes if dn.get("status") == "inactive")
    print(f"[NAMENODE] 🔄 [PASO 10.2]   - Activos: {active_count}, Inactivos: {inactive_count}")
    
    # TERCERO: Aplicar tabla fusionada localmente
    print(f"[NAMENODE] 🔄 [PASO 10.3] Aplicando tabla fusionada localmente...")
    
    try:
        db_path = get_db_path(NODE_ID)
        
        with db_lock:
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            
            try:
                conn._is_replicating = True
                
                # Limpiar y reemplazar con tabla fusionada
                cursor.execute("DELETE FROM datanodes")
                
                if final_datanodes:
                    insert_values = []
                    for datanode in final_datanodes:
                        insert_values.append((
                            datanode["node_id"],
                            datanode["url"],
                            datanode["port"],
                            datanode.get("ip"),
                            datanode["total_space"],
                            datanode["free_space"],
                            datanode.get("last_heartbeat"),
                            datanode["status"],
                            1 if datanode.get("draining") else 0,
                            datanode.get("registered_at")
                        ))
                    
                    cursor.executemany("""
                        INSERT INTO datanodes 
                        (node_id, url, port, ip, total_space, free_space, 
                         last_heartbeat, status, draining, registered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, insert_values)
                
                conn.commit()
                print(f"[NAMENODE] 🔄 [PASO 10.3] ✅ Tabla fusionada aplicada localmente: {len(final_datanodes)} datanodes")
            except Exception as e:
                conn.rollback()
                print(f"[NAMENODE] ❌ [PASO 10.3] Error aplicando tabla fusionada: {e}")
            finally:
                close_connection(conn)
        
        # Invalidar cache
        try:
            from namenode.datanode_cache import get_datanode_cache
            cache = get_datanode_cache(node_id_db=NODE_ID)
            cache.invalidate()
            print(f"[NAMENODE] 🔄 [PASO 10.3] ✅ Cache de datanodes invalidado")
        except Exception as cache_error:
            print(f"[NAMENODE] ⚠️  [PASO 10.3] Error invalidando cache: {cache_error}")
    
    except Exception as e:
        print(f"[NAMENODE] ❌ [PASO 10.3] Error en aplicación local: {e}")
    
    # CUARTO: Si somos el líder, sincronizar tabla fusionada a todos los peers
    print(f"[NAMENODE] 🔄 [PASO 10.4] Sincronizando tabla fusionada a todos los peers...")
    
    if is_leader_now:
        try:
            # Ejecutar sincronización en background para no bloquear la reconciliación
            def sync_merged_table():
                try:
                    # Esperar 2 segundos para que los peers terminen su reconciliación
                    time.sleep(2)
                    
                    # Enviar tabla fusionada a cada peer reunificado
                    with cluster_lock:
                        term = cluster_state["term"]
                        leader_id = cluster_state["node_id"]
                    
                    try:
                        service_token = generate_service_token(leader_id, "service")
                    except Exception:
                        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                    
                    sync_count = 0
                    for peer in reunited_peers:
                        try:
                            peer_url = get_peer_url(peer)
                            response = requests.post(
                                f"{peer_url}/internal/sync-datanodes",
                                json={
                                    "datanodes": final_datanodes,  # Usar tabla fusionada
                                    "term": term,
                                    "leader_id": leader_id,
                                    "timestamp": time.time()
                                },
                                headers={"Authorization": f"Bearer {service_token}"},
                                timeout=10
                            )
                            
                            if response.status_code == 200:
                                sync_count += 1
                                print(f"[NAMENODE] ✅ [PASO 10.4] Tabla fusionada sincronizada a {peer}")
                            else:
                                print(f"[NAMENODE] ⚠️  [PASO 10.4] Error sincronizando a {peer}: HTTP {response.status_code}")
                        except Exception as e:
                            print(f"[NAMENODE] ⚠️  [PASO 10.4] Error sincronizando a {peer}: {e}")
                    
                    print(f"[NAMENODE] 🔄 [PASO 10.4] ✅ Sincronización completada: {sync_count}/{len(reunited_peers)} peers")
                
                except Exception as e:
                    print(f"[NAMENODE] ❌ [PASO 10.4] Error en sincronización: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Ejecutar en background
            sync_thread = threading.Thread(target=sync_merged_table, daemon=True, name="merged-table-sync")
            sync_thread.start()
            print(f"[NAMENODE] 🔄 [PASO 10.4] Líder sincronizando tabla fusionada en background...")
            
        except Exception as e:
            print(f"[NAMENODE] ⚠️  [PASO 10.4] Error iniciando sincronización: {e}")
    else:
        print(f"[NAMENODE] 🔄 [PASO 10.4] Este nodo NO es líder, tabla fusionada ya aplicada localmente")
    
    # Estadísticas finales
    final_log = load_operation_log(NODE_ID)
    final_op_counts = {}
    for op in final_log:
        final_op_counts[op.operation] = final_op_counts.get(op.operation, 0) + 1
    
    # 🔍 DEBUG: Listar todos los archivos en la BD después de reconciliación
    print(f"[DEBUG] 📊 ========== ESTADO FINAL DE ARCHIVOS EN BD ==========")
    try:
        from namenode.database import get_db_path, get_connection, close_connection, db_lock
        from namenode.rw_lock import ReadLock
        from namenode.database import metadata_rw_lock
        
        db_path = get_db_path(NODE_ID)
        
        with ReadLock(metadata_rw_lock):
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            try:
                cursor.execute("SELECT id, name, hash, size, user_id FROM files ORDER BY id")
                files = cursor.fetchall()
                
                print(f"[DEBUG] 📊 Total archivos en BD: {len(files)}")
                for file_row in files:
                    file_id, name, hash_val, size, user_id = file_row
                    print(f"[DEBUG] 📊   - [ID={file_id}] '{name}' | hash={hash_val[:16] if hash_val else 'N/A'}... | size={size} | user={user_id}")
                
                if len(files) == 0:
                    print(f"[DEBUG] 📊 ⚠️  ¡TABLA DE ARCHIVOS VACÍA!")
            finally:
                close_connection(conn)
    except Exception as e:
        print(f"[DEBUG] 📊 ❌ Error listando archivos: {e}")
    print(f"[DEBUG] 📊 ====================================================")
    
    # 🔍 DEBUG: Mostrar operaciones de archivos en el log final
    file_ops_final = [op for op in final_log if op.operation in ['add_file', 'delete_file']]
    print(f"[DEBUG] 📝 ========== OPERACIONES DE ARCHIVOS EN LOG FINAL ==========")
    print(f"[DEBUG] 📝 Total operaciones de archivos en log: {len(file_ops_final)}")
    for op in file_ops_final:
        op_name = op.data.get('name', 'N/A')
        print(f"[DEBUG] 📝   - {op.operation}: '{op_name}' | term={op.term} | t={op.timestamp:.2f}")
    print(f"[DEBUG] 📝 ============================================================")
    
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
    
    # Identificar si hay escrituras en datanodes para logging especial
    datanodes_writes = [w for w in sql_writes if 'datanodes' in w.get('sql', '').lower()]
    if datanodes_writes:
        print(f"[NAMENODE] [SQL_REPLICATE] 📤 Replicando {len(datanodes_writes)} escrituras en tabla 'datanodes' a {len(peers)} peers...")
    
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
                if datanodes_writes:
                    print(f"[NAMENODE] [SQL_REPLICATE] ✅ Escrituras en 'datanodes' replicadas exitosamente a {peer}")
            else:
                update_peer_status(peer, False)
                error_msg = f"[NAMENODE] [SQL_REPLICATE] ❌ Error replicando a {peer}: HTTP {response.status_code}"
                if datanodes_writes:
                    error_msg += f" (incluye {len(datanodes_writes)} escrituras en 'datanodes')"
                print(error_msg)
        except Exception as e:
            update_peer_status(peer, False)
            error_msg = f"[NAMENODE] [SQL_REPLICATE] ❌ Error replicando a {peer}: {e}"
            if datanodes_writes:
                error_msg += f" (incluye {len(datanodes_writes)} escrituras en 'datanodes')"
            print(error_msg)
    
    # Se necesita mayoría (quorum): al menos 2 de 3 nodos
    total_nodes = len(peers) + 1
    quorum = (total_nodes // 2) + 1
    replicated = success_count + 1 >= quorum
    
    if not replicated:
        warning_msg = f"[NAMENODE] [SQL_REPLICATE] ⚠️ WARNING: Solo se replicó a {success_count + 1}/{total_nodes} nodos (quorum: {quorum})"
        if datanodes_writes:
            warning_msg += f" - {len(datanodes_writes)} escrituras en 'datanodes' pueden no haberse replicado"
        print(warning_msg)
    else:
        success_msg = f"[NAMENODE] [SQL_REPLICATE] ✅ {len(sql_writes)} escrituras SQL replicadas a {success_count + 1} peers"
        if datanodes_writes:
            success_msg += f" (incluye {len(datanodes_writes)} escrituras en 'datanodes')"
        print(success_msg)
    
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
    Verifica si hay un líder activo consultando peers descubiertos vía DNS de Docker.
    Solo usa DNS para descubrimiento, no gossip.
    
    Returns:
        ID del líder si se encuentra uno activo y responde, None en caso contrario
    """
    if not peers:
        print(f"[NAMENODE] 🔍 [LEADER_CHECK] No hay peers conocidos vía DNS para consultar")
        return None
    
    print(f"[NAMENODE] 🔍 [LEADER_CHECK] Consultando {len(peers)} peers vía DNS para encontrar líder: {sorted(peers)}")
    
    # Consultar cada peer descubierto vía DNS para ver si hay un líder activo
    for peer_ip in peers:
        try:
            peer_url = get_peer_url(peer_ip)
            print(f"[NAMENODE] 🔍 [LEADER_CHECK] Consultando peer {peer_ip} ({peer_url})...")
            response = requests.get(f"{peer_url}/", timeout=3)
            if response.status_code == 200:
                data = response.json()
                # Si este peer es el líder, verificar que responde directamente
                if data.get("is_leader"):
                    leader_id = data.get("leader_id") or data.get("node_id")
                    # Verificar que el líder responde directamente
                    try:
                        leader_url = get_peer_url(leader_id)
                        leader_response = requests.get(f"{leader_url}/", timeout=3)
                        if leader_response.status_code == 200:
                            leader_data = leader_response.json()
                            if leader_data.get("is_leader"):
                                print(f"[NAMENODE] ✅ [LEADER_CHECK] Líder activo encontrado vía DNS: {leader_id}")
                                return leader_id
                    except Exception as e:
                        print(f"[NAMENODE] ⚠️  [LEADER_CHECK] Líder {leader_id} reportado por {peer_ip} pero no responde: {e}")
                        continue
                # Si este peer conoce un líder, verificar que responde directamente
                elif data.get("leader_id"):
                    leader_id = data.get("leader_id")
                    try:
                        leader_url = get_peer_url(leader_id)
                        leader_response = requests.get(f"{leader_url}/", timeout=3)
                        if leader_response.status_code == 200:
                            leader_data = leader_response.json()
                            if leader_data.get("is_leader"):
                                print(f"[NAMENODE] ✅ [LEADER_CHECK] Líder conocido encontrado vía DNS: {leader_id} (reportado por {peer_ip})")
                                return leader_id
                    except Exception as e:
                        print(f"[NAMENODE] ⚠️  [LEADER_CHECK] Líder {leader_id} reportado por {peer_ip} pero no responde: {e}")
                        continue
        except Exception as e:
            # Continuar con el siguiente peer si este no responde
            print(f"[NAMENODE] ⚠️  [LEADER_CHECK] No se pudo contactar peer {peer_ip}: {e}")
            continue
    
    print(f"[NAMENODE] ❌ [LEADER_CHECK] No se encontró líder activo después de consultar {len(peers)} peers vía DNS")
    print(f"[NAMENODE] 🗳️  [LEADER_CHECK] Se iniciará votación porque no hay líder disponible")
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


def update_active_followers_from_dns():
    """
    Actualiza la lista de seguidores activos consultando directamente todos los namenodes vía DNS.
    Solo se ejecuta en el líder.
    """
    if not is_leader():
        return
    
    with cluster_lock:
        leader_id = cluster_state["node_id"]
        peers = cluster_state["peers"].copy()
    
    # Refrescar lista de peers vía DNS primero
    refresh_peers_from_dns()
    
    with cluster_lock:
        # Obtener peers actualizados después del refresh
        peers = [p for p in cluster_state["peers"] if p != leader_id]
    
    print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] Verificando seguidores activos vía DNS...")
    verified_active_followers = []
    
    # Consultar cada peer vía DNS para verificar si es un seguidor activo
    for peer_ip in peers:
        try:
            peer_url = get_peer_url(peer_ip)
            response = requests.get(f"{peer_url}/", timeout=3)
            if response.status_code == 200:
                data = response.json()
                # Si este peer no es líder (es seguidor) y responde, está activo
                if not data.get("is_leader"):
                    # Verificar que reconoce al líder actual
                    peer_leader_id = data.get("leader_id")
                    if peer_leader_id == leader_id:
                        verified_active_followers.append(peer_ip)
                        update_peer_status(peer_ip, True)
                        print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] ✅ Seguidor activo verificado: {peer_ip}")
                    else:
                        print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] ⚠️  Peer {peer_ip} no reconoce al líder actual (conoce: {peer_leader_id})")
                        update_peer_status(peer_ip, False)
                else:
                    # Este peer es líder (no es seguidor)
                    print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] ⚠️  Peer {peer_ip} es líder, no es seguidor")
                    update_peer_status(peer_ip, False)
        except Exception as e:
            # Peer no responde, marcarlo como inactivo
            update_peer_status(peer_ip, False)
            print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] ❌ Peer {peer_ip} no responde: {e}")
    
    # Actualizar lista de seguidores activos
    with cluster_lock:
        old_active_followers = set(cluster_state.get("active_followers", []))
        new_active_followers = set(verified_active_followers)
        
        if old_active_followers != new_active_followers:
            added = new_active_followers - old_active_followers
            removed = old_active_followers - new_active_followers
            if added:
                print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] 🆕 Seguidores activos agregados: {sorted(added)}")
            if removed:
                print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] 🗑️  Seguidores activos removidos: {sorted(removed)}")
        
        cluster_state["active_followers"] = verified_active_followers.copy()
        print(f"[NAMENODE] 🔍 [FOLLOWER_TRACKING] 📋 Lista actualizada: {len(verified_active_followers)}/{len(peers)} seguidores activos: {sorted(verified_active_followers)}")


def sync_datanodes_to_followers():
    """
    Sincroniza la tabla completa de datanodes del líder con los seguidores.
    Se ejecuta periódicamente desde el líder para asegurar consistencia.
    """
    if not is_leader():
        return
    
    try:
        # Obtener todos los datanodes del líder (solo lectura, rápido)
        db_path = get_db_path(NODE_ID)
        
        # Usar solo read lock para lectura, liberar rápidamente
        from namenode.rw_lock import ReadLock
        from namenode.database import metadata_rw_lock
        
        datanodes = []
        with ReadLock(metadata_rw_lock):
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            
            try:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes
                """)
                
                for row in cursor.fetchall():
                    datanodes.append({
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
            finally:
                close_connection(conn)
        
        if not datanodes:
            print(f"[NAMENODE] 🔄 [SYNC_DATANODES] No hay datanodes para sincronizar")
            return
        
        print(f"[NAMENODE] 🔄 [SYNC_DATANODES] Sincronizando {len(datanodes)} datanodes a seguidores")
        
        # Obtener lista de seguidores activos
        with cluster_lock:
            leader_id = cluster_state["node_id"]
            active_followers = cluster_state.get("active_followers", [])
            term = cluster_state["term"]
        
        if not active_followers:
            print(f"[NAMENODE] 🔄 [SYNC_DATANODES] No hay seguidores activos para sincronizar")
            return
        
        # Obtener token de servicio
        try:
            service_token = generate_service_token(leader_id, "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Enviar snapshot a cada seguidor activo
        success_count = 0
        for follower in active_followers:
            try:
                follower_url = get_peer_url(follower)
                response = requests.post(
                    f"{follower_url}/internal/sync-datanodes",
                    json={
                        "datanodes": datanodes,
                        "term": term,
                        "leader_id": leader_id,
                        "timestamp": time.time()
                    },
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=10
                )
                
                if response.status_code == 200:
                    success_count += 1
                    print(f"[NAMENODE] ✅ [SYNC_DATANODES] Tabla datanodes sincronizada a {follower}")
                else:
                    print(f"[NAMENODE] ⚠️  [SYNC_DATANODES] Error sincronizando a {follower}: HTTP {response.status_code}")
            except Exception as e:
                print(f"[NAMENODE] ⚠️  [SYNC_DATANODES] Error sincronizando a {follower}: {e}")
        
        print(f"[NAMENODE] 🔄 [SYNC_DATANODES] Sincronización completada: {success_count}/{len(active_followers)} seguidores actualizados")
        
    except Exception as e:
        print(f"[NAMENODE] ❌ [SYNC_DATANODES] Error en sincronización: {e}")
        import traceback
        traceback.print_exc()


def leader_heartbeat_loop():
    """
    Loop que envía heartbeats a los seguidores y mantiene actualizada la lista de seguidores activos.
    También verifica periódicamente los seguidores vía DNS para asegurar que la lista esté actualizada.
    Detecta y reconcilia múltiples líderes automáticamente.
    """
    follower_check_counter = 0
    datanode_sync_counter = 0
    multi_leader_check_counter = 0
    FOLLOWER_CHECK_INTERVAL = 6  # Verificar seguidores vía DNS cada 6 ciclos (~30 segundos)
    DATANODE_SYNC_INTERVAL = 12  # Sincronizar datanodes cada 12 ciclos (~60 segundos)
    MULTI_LEADER_CHECK_INTERVAL = 6  # Verificar múltiples líderes cada 6 ciclos (~30 segundos)
    
    while True:
        time.sleep(LEADER_HEARTBEAT_INTERVAL)
        
        if not is_leader():
            continue
        
        # 🔍 DEBUG: Verificar periódicamente si hay múltiples líderes activos
        multi_leader_check_counter += 1
        if multi_leader_check_counter >= MULTI_LEADER_CHECK_INTERVAL:
            multi_leader_check_counter = 0
            
            # Obtener todos los peers conocidos
            with cluster_lock:
                all_peers = cluster_state["peers"].copy()
                current_node_id = cluster_state["node_id"]
                current_is_leader = cluster_state["is_leader"]
            
            # 🔍 DEBUG: Mostrar estado actual del líder
            print(f"[DEBUG] 👑 ========== VERIFICACIÓN DE MÚLTIPLES LÍDERES ==========")
            print(f"[DEBUG] 👑 Este nodo: {current_node_id}, Es líder: {current_is_leader}")
            print(f"[DEBUG] 👑 Peers conocidos ({len(all_peers)}): {all_peers}")
            print(f"[DEBUG] 👑 ============================================================")
            
            if not all_peers:
                print(f"[DEBUG] ⚠️  No hay peers conocidos, saltando verificación de múltiples líderes")
                print(f"[DEBUG] 💡 Ejecutando discover_peers_dns() para actualizar lista...")
                refresh_peers_from_dns()
                with cluster_lock:
                    all_peers = cluster_state["peers"].copy()
                print(f"[DEBUG] 📋 Peers después de DNS: {all_peers}")
            
            # Detectar múltiples líderes (solo verificar peers, no incluir este nodo en la lista)
            # La función detect_multiple_leaders ya agrega este nodo internamente si es líder
            print(f"[DEBUG] 🔍 Llamando detect_multiple_leaders con peers: {all_peers}")
            detected_leaders = detect_multiple_leaders(all_peers)
            
            print(f"[DEBUG] 🔍 Resultado: {len(detected_leaders)} líder(es) detectado(s)")
            
            if len(detected_leaders) > 1:
                print(f"[DEBUG] 🚨 ========== MÚLTIPLES LÍDERES DETECTADOS ==========")
                print(f"[DEBUG] 🚨 Se detectaron {len(detected_leaders)} líderes activos:")
                for leader in detected_leaders:
                    print(f"[DEBUG] 🚨   - {leader['node_id']}: term={leader['term']}")
                print(f"[DEBUG] 🚨 Disparando reconciliación automática...")
                print(f"[DEBUG] 🚨 ====================================================")
                
                # Disparar reconciliación con todos los líderes detectados
                other_leaders = [l['node_id'] for l in detected_leaders if l['node_id'] != current_node_id]
                print(f"[DEBUG] 🚨 Otros líderes para reconciliar: {other_leaders}")
                
                if other_leaders:
                    with cluster_lock:
                        if not cluster_state["reconciliation_in_progress"]:
                            print(f"[DEBUG] 🚀 Iniciando thread de reconciliación...")
                            threading.Thread(
                                target=trigger_reconciliation, 
                                args=(other_leaders,), 
                                daemon=True
                            ).start()
                        else:
                            print(f"[DEBUG] ⚠️  Reconciliación ya en progreso, no iniciar otra")
                else:
                    print(f"[DEBUG] ⚠️  Lista de otros líderes vacía, no iniciar reconciliación")
            elif len(detected_leaders) == 1:
                print(f"[DEBUG] ✅ Un solo líder en el cluster: {detected_leaders[0]['node_id']}")
            else:
                print(f"[DEBUG] ⚠️  No se detectaron líderes (esto no debería pasar si este nodo es líder)")
        
        # Verificar seguidores activos vía DNS periódicamente (cada ~30 segundos)
        follower_check_counter += 1
        if follower_check_counter >= FOLLOWER_CHECK_INTERVAL:
            follower_check_counter = 0
            update_active_followers_from_dns()
        
        # Sincronizar tabla datanodes periódicamente (cada ~60 segundos)
        # Ejecutar en thread separado para no bloquear heartbeats
        datanode_sync_counter += 1
        if datanode_sync_counter >= DATANODE_SYNC_INTERVAL:
            datanode_sync_counter = 0
            try:
                # Ejecutar en background para no bloquear heartbeats
                def sync_in_background():
                    try:
                        sync_datanodes_to_followers()
                    except Exception as e:
                        print(f"[NAMENODE] ⚠️  [SYNC_DATANODES] Error en sincronización background: {e}")
                        import traceback
                        traceback.print_exc()
                
                sync_thread = threading.Thread(target=sync_in_background, daemon=True, name="datanode-sync")
                sync_thread.start()
            except Exception as e:
                print(f"[NAMENODE] ⚠️  [HEARTBEAT] Error iniciando sincronización de datanodes: {e}")
        
        # Enviar heartbeat para mantener el liderazgo
        with cluster_lock:
            term = cluster_state["term"]
            leader_id = cluster_state["node_id"]
            # Usar la lista de seguidores activos actualizada (si está disponible)
            # Si no está actualizada, usar todos los peers
            active_followers_from_state = cluster_state.get("active_followers", [])
            
            # Crear set con todas las variaciones del líder para filtrar
            leader_variations = {
                leader_id,
                f"tbfs-{leader_id}",
                leader_id.replace("tbfs-", ""),
                leader_id.replace("namenode-", ""),
                f"namenode-{leader_id.replace('namenode-', '')}" if "namenode" in leader_id else leader_id
            }
            
            # Filtrar el líder de la lista de peers (todas las variaciones)
            all_peers = [p for p in cluster_state["peers"] if p not in leader_variations]
            
            # Priorizar verificar seguidores que están marcados como activos
            # Pero también intentar con todos los peers por si alguno se recuperó
            peers_to_check = list(set(active_followers_from_state + all_peers))
            peers = [p for p in peers_to_check if p not in leader_variations]
            
            # all_known_peers es lo que se envía a los followers - debe excluir al líder
            all_known_peers = peers.copy()
        
        # Obtener token de servicio para autenticación
        try:
            service_token = generate_service_token(leader_id, "service")
        except Exception:
            service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
        
        # Detectar qué seguidores están activos mediante heartbeats
        active_followers = []
        
        # Actualizar estado del líder como vivo
        update_peer_status(leader_id, True)
        
        print(f"[NAMENODE] 💓 [HEARTBEAT] Enviando heartbeats desde líder {leader_id}")
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Verificando {len(peers)} seguidores: {sorted(peers)}")
        
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                response = requests.post(
                    f"{peer_url}/internal/heartbeat",
                    json={
                        "term": term, 
                        "leader_id": leader_id,
                        "peers": all_known_peers,
                        "active_followers": []  # Se actualizará después con la lista completa
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
        # Esto permite que los seguidores sepan quiénes son los otros seguidores activos
        for peer in peers:
            try:
                peer_url = get_peer_url(peer)
                response = requests.post(
                    f"{peer_url}/internal/heartbeat",
                    json={
                        "term": term, 
                        "leader_id": leader_id,
                        "peers": all_known_peers,
                        "active_followers": active_followers
                    },
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=2
                )
                if response.status_code == 200:
                    update_peer_status(peer, True)
                    # Si no estaba en la lista, agregarlo (por si se recuperó)
                    if peer not in active_followers:
                        active_followers.append(peer)
            except Exception:
                update_peer_status(peer, False)
        
        # Detectar nuevos seguidores antes de actualizar el estado
        with cluster_lock:
            previous_active_followers = set(cluster_state.get("active_followers", []))
        
        new_followers = set(active_followers) - previous_active_followers
        
        # Actualizar lista de seguidores activos en el estado del cluster
        with cluster_lock:
            cluster_state["last_heartbeat_time"] = time.time()
            # Combinar seguidores activos verificados vía heartbeat con los del estado anterior
            # Mantener solo los que respondieron exitosamente
            cluster_state["active_followers"] = active_followers.copy()
        
        # Sincronizar inmediatamente a nuevos seguidores
        if new_followers:
            print(f"[NAMENODE] 🆕 [HEARTBEAT] Nuevos seguidores detectados: {sorted(new_followers)}")
            print(f"[NAMENODE] 🔄 [HEARTBEAT] Sincronizando tabla datanodes inmediatamente a nuevos seguidores...")
            
            # Sincronizar solo a los nuevos seguidores (en background para no bloquear)
            def sync_new_followers():
                try:
                    # Obtener datanodes del líder
                    db_path = get_db_path(NODE_ID)
                    from namenode.database import get_connection, close_connection
                    from namenode.rw_lock import ReadLock
                    from namenode.database import metadata_rw_lock
                    
                    datanodes = []
                    with ReadLock(metadata_rw_lock):
                        conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
                        try:
                            cursor.execute("""
                                SELECT node_id, url, port, ip, total_space, free_space, 
                                       last_heartbeat, status, registered_at, draining
                                FROM datanodes
                            """)
                            for row in cursor.fetchall():
                                datanodes.append({
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
                        finally:
                            close_connection(conn)
                    
                    # Enviar a cada nuevo seguidor
                    with cluster_lock:
                        term = cluster_state["term"]
                        leader_id = cluster_state["node_id"]
                    
                    try:
                        service_token = generate_service_token(leader_id, "service")
                    except Exception:
                        service_token = os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token")
                    
                    for follower in new_followers:
                        try:
                            follower_url = get_peer_url(follower)
                            response = requests.post(
                                f"{follower_url}/internal/sync-datanodes",
                                json={
                                    "datanodes": datanodes,
                                    "term": term,
                                    "leader_id": leader_id,
                                    "timestamp": time.time()
                                },
                                headers={"Authorization": f"Bearer {service_token}"},
                                timeout=10
                            )
                            
                            if response.status_code == 200:
                                print(f"[NAMENODE] ✅ [HEARTBEAT] Tabla datanodes sincronizada a nuevo seguidor {follower}")
                            else:
                                print(f"[NAMENODE] ⚠️  [HEARTBEAT] Error sincronizando a nuevo seguidor {follower}: HTTP {response.status_code}")
                        except Exception as e:
                            print(f"[NAMENODE] ⚠️  [HEARTBEAT] Error sincronizando a nuevo seguidor {follower}: {e}")
                
                except Exception as e:
                    print(f"[NAMENODE] ❌ [HEARTBEAT] Error en sincronización de nuevos seguidores: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Ejecutar en background
            sync_thread = threading.Thread(target=sync_new_followers, daemon=True, name="new-follower-sync")
            sync_thread.start()
        
        # Log del estado de seguidores
        with cluster_lock:
            current_node_id = cluster_state["node_id"]
            current_peers = [p for p in cluster_state["peers"] if p != current_node_id]
        
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋 Estado final:")
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋   - Peers conocidos: {len(current_peers)}")
        print(f"[NAMENODE] 💓 [HEARTBEAT] 📋   - Seguidores activos: {len(active_followers)}/{len(peers)}")
        if active_followers:
            print(f"[NAMENODE] ✅ [HEARTBEAT] Seguidores activos: {sorted(active_followers)}")
            inactive = [p for p in peers if p not in active_followers]
            if inactive:
                print(f"[NAMENODE] ⚠️  [HEARTBEAT] Seguidores inactivos: {sorted(inactive)}")
        else:
            print(f"[NAMENODE] ⚠️  [HEARTBEAT] No hay seguidores activos de {len(peers)} peers verificados")


# Intervalo de sincronización (segundos)
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "30"))


def detect_and_log_table_changes(node_id: str = None):
    """
    Detecta cambios en las tablas file_replicas y datanodes y los registra en el log de operaciones.
    Solo se ejecuta en el líder.
    """
    if not is_leader():
        return
    
    if node_id is None:
        node_id = NODE_ID
    
    import json
    from namenode.database import get_db_path, get_connection, close_connection, db_lock
    
    try:
        db_path = get_db_path(node_id=node_id)
        
        with db_lock:
            conn, cursor = get_connection(db_path=db_path, node_id=node_id)
            
            try:
                # Obtener snapshot actual de file_replicas
                cursor.execute("""
                    SELECT file_id, datanode_id, replica_type
                    FROM file_replicas
                    ORDER BY file_id, datanode_id
                """)
                current_file_replicas = {(row[0], row[1]): row[2] for row in cursor.fetchall()}
                
                # Obtener snapshot actual de datanodes
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, draining, registered_at
                    FROM datanodes
                    ORDER BY node_id
                """)
                current_datanodes = {}
                for row in cursor.fetchall():
                    current_datanodes[row[0]] = {
                        "node_id": row[0],
                        "url": row[1],
                        "port": row[2],
                        "ip": row[3],
                        "total_space": row[4],
                        "free_space": row[5],
                        "last_heartbeat": row[6],
                        "status": row[7],
                        "draining": bool(row[8]),
                        "registered_at": row[9]
                    }
                
            finally:
                close_connection(conn)
        
        # Comparar con snapshots anteriores
        with cluster_lock:
            last_file_replicas = cluster_state.get("last_file_replicas_snapshot")
            last_datanodes = cluster_state.get("last_datanodes_snapshot")
            
            # Si es la primera vez (snapshots None), inicializar con estado actual (sin cambios)
            if last_file_replicas is None:
                cluster_state["last_file_replicas_snapshot"] = current_file_replicas.copy()
                last_file_replicas = {}
            if last_datanodes is None:
                cluster_state["last_datanodes_snapshot"] = current_datanodes.copy()
                last_datanodes = {}
        
        changes_detected = False
        
        # Detectar cambios en file_replicas
        if last_file_replicas != current_file_replicas:
            changes_detected = True
            
            # Identificar cambios: agregados, modificados, eliminados
            current_keys = set(current_file_replicas.keys())
            last_keys = set(last_file_replicas.keys())
            
            added = {k: current_file_replicas[k] for k in current_keys - last_keys}
            removed = {k: last_file_replicas[k] for k in last_keys - current_keys}
            modified = {
                k: current_file_replicas[k] 
                for k in current_keys & last_keys 
                if current_file_replicas[k] != last_file_replicas[k]
            }
            
            if added or removed or modified:
                # Crear lista completa de replicas para sincronización
                file_replicas_list = [
                    {"file_id": file_id, "datanode_id": datanode_id, "replica_type": replica_type}
                    for (file_id, datanode_id), replica_type in current_file_replicas.items()
                ]
                
                # Generar operación de sincronización
                with cluster_lock:
                    term = cluster_state["term"]
                
                operation = OperationLog(
                    operation="sync_file_replicas",
                    data={
                        "file_replicas": file_replicas_list,
                        "changes": {
                            "added": [{"file_id": k[0], "datanode_id": k[1], "replica_type": v} for k, v in added.items()],
                            "removed": [{"file_id": k[0], "datanode_id": k[1], "replica_type": v} for k, v in removed.items()],
                            "modified": [{"file_id": k[0], "datanode_id": k[1], "replica_type": v} for k, v in modified.items()]
                        }
                    },
                    term=term,
                    timestamp=time.time()
                )
                
                save_operation_to_log(operation, node_id)
                with log_lock:
                    operation_log.append(operation)
                
                change_summary = []
                if added:
                    change_summary.append(f"{len(added)} agregadas")
                if removed:
                    change_summary.append(f"{len(removed)} eliminadas")
                if modified:
                    change_summary.append(f"{len(modified)} modificadas")
                
                print(f"[NAMENODE] 📋 [TABLE_SYNC] Cambios detectados en file_replicas: {', '.join(change_summary)}")
                print(f"[NAMENODE] 📋 [TABLE_SYNC] Total de réplicas: {len(current_file_replicas)}")
        
        # Detectar cambios en datanodes
        if last_datanodes != current_datanodes:
            changes_detected = True
            
            # Identificar cambios: agregados, modificados, eliminados
            current_keys = set(current_datanodes.keys())
            last_keys = set(last_datanodes.keys())
            
            added = {k: current_datanodes[k] for k in current_keys - last_keys}
            removed = {k: last_datanodes[k] for k in last_keys - current_keys}
            modified = {
                k: current_datanodes[k] 
                for k in current_keys & last_keys 
                if current_datanodes[k] != last_datanodes[k]
            }
            
            if added or removed or modified:
                # Generar operación de sincronización
                with cluster_lock:
                    term = cluster_state["term"]
                
                operation = OperationLog(
                    operation="sync_datanodes",
                    data={
                        "datanodes": list(current_datanodes.values()),
                        "changes": {
                            "added": [added[k] for k in added],
                            "removed": [removed[k] for k in removed],
                            "modified": [modified[k] for k in modified]
                        }
                    },
                    term=term,
                    timestamp=time.time()
                )
                
                save_operation_to_log(operation, node_id)
                with log_lock:
                    operation_log.append(operation)
                
                change_summary = []
                if added:
                    change_summary.append(f"{len(added)} agregados")
                if removed:
                    change_summary.append(f"{len(removed)} eliminados")
                if modified:
                    change_summary.append(f"{len(modified)} modificados")
                
                print(f"[NAMENODE] 📋 [TABLE_SYNC] Cambios detectados en datanodes: {', '.join(change_summary)}")
                print(f"[NAMENODE] 📋 [TABLE_SYNC] Total de datanodes: {len(current_datanodes)}")
        
        # Actualizar snapshots si hubo cambios
        if changes_detected:
            with cluster_lock:
                cluster_state["last_file_replicas_snapshot"] = current_file_replicas.copy()
                cluster_state["last_datanodes_snapshot"] = current_datanodes.copy()
        
    except Exception as e:
        print(f"[NAMENODE] ⚠️  [TABLE_SYNC] Error detectando cambios en tablas: {e}")
        import traceback
        traceback.print_exc()


def leader_sync_loop():
    """
    Loop que sincroniza periódicamente el log de operaciones del líder con los seguidores.
    Esto asegura que los seguidores tengan una copia actualizada de los datos.
    También detecta y registra cambios en las tablas file_replicas y datanodes.
    """
    # Esperar a que el cluster se estabilice
    time.sleep(15)
    
    while True:
        time.sleep(SYNC_INTERVAL)
        
        if not is_leader():
            continue
        
        # Detectar y registrar cambios en file_replicas y datanodes
        try:
            detect_and_log_table_changes(NODE_ID)
        except Exception as e:
            print(f"[NAMENODE] ⚠️  [SYNC] Error en detección de cambios de tablas: {e}")
        
        with cluster_lock:
            peers = cluster_state["peers"].copy()
            leader_id = cluster_state["node_id"]
            active_followers = cluster_state.get("active_followers", []).copy()
        
        if not active_followers:
            print(f"[NAMENODE] 🔄 [SYNC] No hay seguidores activos para sincronizar")
            continue
        
        print(f"[NAMENODE] 🔄 [SYNC] Iniciando sincronización periódica con {len(active_followers)} seguidores...")
        
        # Cargar el log de operaciones local (incluye los cambios recién detectados)
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
                    # follower_operations puede ser una lista de diccionarios (JSON) o de objetos OperationLog
                    if isinstance(op, dict):
                        op_operation = op.get('operation', '')
                        op_timestamp = op.get('timestamp', 0)
                        op_term = op.get('term', 0)
                    else:
                        # Es un objeto OperationLog
                        op_operation = op.operation
                        op_timestamp = op.timestamp
                        op_term = op.term
                    key = f"{op_operation}_{op_timestamp}_{op_term}"
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
    """
    Verifica periódicamente si el líder sigue activo (para seguidores).
    Si el líder no responde después de LEADER_TIMEOUT (10-15s), inicia elección comunicándose con peers vía DNS.
    """
    while True:
        # Verificar cada FOLLOWER_LEADER_CHECK_INTERVAL segundos (10-15s)
        time.sleep(FOLLOWER_LEADER_CHECK_INTERVAL)
        
        if is_leader():
            continue
        
        with cluster_lock:
            leader_id = cluster_state["leader_id"]
            last_heartbeat_time = cluster_state.get("last_heartbeat_time", 0)
        
        current_time = time.time()
        time_since_heartbeat = current_time - last_heartbeat_time
        
        if not leader_id:
            # No hay líder conocido, iniciar elección
            print(f"[NAMENODE] 🗳️  [FOLLOWER] No hay líder conocido, iniciando elección vía DNS...")
            # Refrescar peers vía DNS antes de elección
            refresh_peers_from_dns()
            start_election()
            continue
        
        # Verificar si el líder responde directamente
        print(f"[NAMENODE] 🔍 [FOLLOWER] Verificando líder {leader_id} (último heartbeat hace {time_since_heartbeat:.1f}s)...")
        print(f"[DEBUG] 🔍 [FOLLOWER] Intentando contactar líder: {leader_id}")
        leader_responding = False
        
        try:
            leader_url = get_peer_url(leader_id)
            print(f"[DEBUG] 🔍 [FOLLOWER] URL del líder: {leader_url}")
            response = requests.get(f"{leader_url}/", timeout=3)
            print(f"[DEBUG] 🔍 [FOLLOWER] Respuesta del líder: HTTP {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"[DEBUG] 🔍 [FOLLOWER] Data del líder: is_leader={data.get('is_leader')}, term={data.get('term')}")
                
                if data.get("is_leader"):
                    # El líder está vivo, actualizar timestamp
                    with cluster_lock:
                        cluster_state["last_heartbeat_time"] = current_time
                    print(f"[NAMENODE] ✅ [FOLLOWER] Líder {leader_id} responde correctamente")
                    leader_responding = True
                else:
                    print(f"[NAMENODE] ⚠️  [FOLLOWER] Líder {leader_id} reportado pero no es líder activo")
                    print(f"[DEBUG] 🚨 [FOLLOWER] ¡Líder reporta NO ser líder! Posible split-brain")
        except Exception as e:
            print(f"[NAMENODE] ⚠️  [FOLLOWER] Líder {leader_id} no responde: {e}")
            print(f"[DEBUG] ❌ [FOLLOWER] Error contactando líder: {type(e).__name__}: {e}")
        
        # Si el líder no responde Y ha pasado LEADER_TIMEOUT, iniciar elección
        if not leader_responding:
            if time_since_heartbeat >= LEADER_TIMEOUT:
                print(f"[NAMENODE] ❌ [FOLLOWER] Líder {leader_id} no responde después de {time_since_heartbeat:.1f}s (timeout: {LEADER_TIMEOUT}s)")
                print(f"[NAMENODE] 🗳️  [FOLLOWER] Iniciando elección comunicándose con peers vía DNS...")
                
                # Refrescar peers vía DNS antes de elección (para asegurar lista actualizada)
                refresh_peers_from_dns()
                
                with cluster_lock:
                    cluster_state["leader_id"] = None
                    cluster_state["last_heartbeat_time"] = 0
                
                # Iniciar elección (start_election ya usa DNS para descubrimiento)
                start_election()
            else:
                print(f"[NAMENODE] ⏳ [FOLLOWER] Líder {leader_id} no responde temporalmente ({time_since_heartbeat:.1f}s < {LEADER_TIMEOUT}s), esperando...")


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
    print(f"[NAMENODE] ⏱️  [CONFIG]   - FOLLOWER_LEADER_CHECK_INTERVAL: {FOLLOWER_LEADER_CHECK_INTERVAL}s (verificación periódica del líder)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - LEADER_TIMEOUT: {LEADER_TIMEOUT}s (tiempo sin respuesta antes de considerar líder muerto)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - PEER_FAILURE_TIMEOUT: 30s (tiempo para marcar como suspected)")
    print(f"[NAMENODE] ⏱️  [CONFIG]   - PEER_DEAD_TIMEOUT: 60s (tiempo para marcar como dead)")
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
    
    # ========== LÓGICA DE INICIO: BUSCAR LÍDER VÍA DNS ==========
    print(f"[NAMENODE] 🔍 [STARTUP] Buscando líder existente vía DNS de Docker...")
    
    # Descubrir peers via DNS de Docker (excluyendo este nodo)
    refresh_peers_from_dns()
    
    with cluster_lock:
        peers = cluster_state["peers"].copy()
        current_node_id = cluster_state["node_id"]
    
    print(f"[NAMENODE] 🔍 [STARTUP] Peers descubiertos vía DNS ({len(peers)}): {sorted(peers) if peers else '[]'}")
    
    # Buscar líder activo consultando cada peer descubierto vía DNS
    leader_found = None
    if peers:
        print(f"[NAMENODE] 🔍 [STARTUP] Consultando peers vía DNS para encontrar líder...")
        for peer_ip in peers:
            try:
                peer_url = get_peer_url(peer_ip)
                print(f"[NAMENODE] 🔍 [STARTUP] Consultando peer {peer_ip} ({peer_url})...")
                response = requests.get(f"{peer_url}/", timeout=3)
                if response.status_code == 200:
                    data = response.json()
                    # Si este peer es el líder, verificar que responde directamente
                    if data.get("is_leader"):
                        leader_id = data.get("leader_id") or data.get("node_id")
                        # Verificar que el líder responde directamente
                        try:
                            leader_url = get_peer_url(leader_id)
                            leader_response = requests.get(f"{leader_url}/", timeout=3)
                            if leader_response.status_code == 200:
                                leader_data = leader_response.json()
                                if leader_data.get("is_leader"):
                                    leader_found = leader_id
                                    print(f"[NAMENODE] ✅ [STARTUP] Líder activo encontrado vía DNS: {leader_id}")
                                    break
                        except Exception as e:
                            print(f"[NAMENODE] ⚠️  [STARTUP] Líder {leader_id} reportado por {peer_ip} pero no responde: {e}")
                            continue
                    # Si este peer conoce un líder, verificar que responde directamente
                    elif data.get("leader_id"):
                        leader_id = data.get("leader_id")
                        try:
                            leader_url = get_peer_url(leader_id)
                            leader_response = requests.get(f"{leader_url}/", timeout=3)
                            if leader_response.status_code == 200:
                                leader_data = leader_response.json()
                                if leader_data.get("is_leader"):
                                    leader_found = leader_id
                                    print(f"[NAMENODE] ✅ [STARTUP] Líder conocido encontrado vía DNS: {leader_id} (reportado por {peer_ip})")
                                    break
                        except Exception as e:
                            print(f"[NAMENODE] ⚠️  [STARTUP] Líder {leader_id} reportado por {peer_ip} pero no responde: {e}")
                            continue
            except Exception as e:
                print(f"[NAMENODE] ⚠️  [STARTUP] No se pudo contactar peer {peer_ip}: {e}")
                continue
    
    # Decisión: ¿Líder encontrado o iniciar elección?
    if leader_found:
        # Incorporarse como seguidor
        print(f"[NAMENODE] 📋 [STARTUP] Incorporándose como seguidor del líder {leader_found}")
        with cluster_lock:
            cluster_state["leader_id"] = leader_found
            cluster_state["is_leader"] = False
            cluster_state["last_heartbeat_time"] = time.time()
        print(f"[NAMENODE] ✅ [STARTUP] Estado: seguidor del líder {leader_found}")
    else:
        # No hay líder, iniciar elección
        if peers:
            print(f"[NAMENODE] 🗳️  [STARTUP] No se encontró líder activo vía DNS. Iniciando elección...")
        else:
            print(f"[NAMENODE] 🗳️  [STARTUP] No hay peers conocidos. Iniciando elección (nodo único se convertirá en líder)...")
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
            # IMPORTANTE: Pasar el header de Authorization al líder
            response = requests.post(
                f"{leader_url}/datanodes/register",
                json=registration.dict(),
                headers={"Authorization": authorization},  # Pasar el token al líder
                timeout=5
            )
            return response.json()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Error conectando con líder: {e}")
    
    if not is_leader():
        raise HTTPException(status_code=503, detail="No hay líder disponible")
    
    # Logging detallado de la IP recibida
    ip_info = f" (IP: {registration.ip})" if registration.ip else " (IP: None/No proporcionada)"
    print(f"[NAMENODE] 📥 Registrando DataNode: {registration.node_id} - url={registration.url}, port={registration.port}{ip_info}")
    
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
            # IMPORTANTE: Pasar el header de Authorization al líder
            response = requests.post(
                f"{leader_url}/datanodes/{node_id}/heartbeat",
                json=heartbeat.dict(),
                headers={"Authorization": authorization},  # Pasar el token al líder
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
    
    # 🔍 DEBUG: Operación creada
    print(f"[DEBUG] 📝 Operación creada: add_file | archivo='{file.filename}' | term={operation.term} | t={operation.timestamp:.2f}")
    
    # Guardar en log persistente (Fase 2)
    save_operation_to_log(operation, NODE_ID)
    
    # 🔍 DEBUG: Confirmar guardado
    print(f"[DEBUG] ✅ Operación add_file guardada en log para '{file.filename}'")
    
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
        # Para archivos grandes, aumentamos el timeout (calculado basado en tamaño)
        # Timeout base: 30 segundos + 1 segundo por cada 100MB (mínimo 30s, máximo 300s)
        base_timeout = 30
        timeout_per_100mb = 1
        max_timeout = 300  # 5 minutos máximo
        file_size_mb = file_size / (1024 * 1024)
        calculated_timeout = base_timeout + int((file_size_mb / 100) * timeout_per_100mb)
        verification_timeout = min(max_timeout, max(base_timeout, calculated_timeout))
        
        print(f"[NAMENODE] [CHUNKED_UPLOAD] Verificando archivo ({file_size_mb:.2f} MB) con timeout de {verification_timeout}s...")
        
        # Hacer verificación con retries (hasta 3 intentos con backoff exponencial)
        max_retries = 3
        retry_delay = 2  # segundos
        verify_response = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                verify_response = requests.get(
                    f"{datanode_url}/retrieve/{file_hash}",
                    headers={"Authorization": f"Bearer {service_token}", "Range": "bytes=0-0"},
                    timeout=verification_timeout
                )
                # Si la respuesta es exitosa, salir del loop
                if verify_response.status_code in [200, 206, 404]:
                    break
                else:
                    last_error = f"HTTP {verify_response.status_code}"
            except requests.Timeout as e:
                last_error = f"Timeout después de {verification_timeout}s"
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)  # Backoff exponencial: 2s, 4s, 8s
                    print(f"[NAMENODE] [CHUNKED_UPLOAD] ⚠️  Intento {attempt + 1}/{max_retries} falló ({last_error}), esperando {wait_time}s antes de reintentar...")
                    time.sleep(wait_time)
                else:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Timeout verificando archivo en DataNode {datanode_id} después de {max_retries} intentos. El archivo puede estar aún siendo procesado."
                    )
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    print(f"[NAMENODE] [CHUNKED_UPLOAD] ⚠️  Intento {attempt + 1}/{max_retries} falló ({last_error}), esperando {wait_time}s antes de reintentar...")
                    time.sleep(wait_time)
                else:
                    raise
        
        if verify_response is None:
            raise HTTPException(
                status_code=503,
                detail=f"Error verificando archivo en DataNode {datanode_id} después de {max_retries} intentos: {last_error}"
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
    
    # 🔍 DEBUG: Operación creada (chunked upload)
    print(f"[DEBUG] 📝 Operación creada (chunked): add_file | archivo='{filename}' | term={operation.term} | t={operation.timestamp:.2f}")
    
    save_operation_to_log(operation, NODE_ID)
    
    # 🔍 DEBUG: Confirmar guardado
    print(f"[DEBUG] ✅ Operación add_file guardada en log para '{filename}' (chunked upload)")
    
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
        # CRÍTICO: Usar hash como identificador único para evitar colisiones de file_id entre particiones
        file_hash = file_info.get("hash", "")
        file_name = file_info.get("name", "N/A")
        
        operation = OperationLog(
            operation="delete_file",
            data={
                "file_id": file_id,  # Mantener para compatibilidad local
                "hash": file_hash,   # NUEVO: Identificador único global
                "name": file_name,   # Para debugging y búsqueda alternativa
                "user_id": current_user.username
            },
            term=cluster_state["term"],
            timestamp=time.time()
        )
        
        # 🔍 DEBUG: Operación delete creada
        print(f"[DEBUG] 🗑️  Operación delete_file creada: ID={file_id}, nombre='{file_name}', hash={file_hash[:16] if file_hash else 'N/A'}..., term={cluster_state['term']}")
        
        with log_lock:
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
            # Usar apply_operation_safely para aprovechar la búsqueda por hash
            op_log = OperationLog(
                operation="delete_file",
                data=operation_data,
                term=term,
                timestamp=timestamp
            )
            apply_operation_safely(op_log, NODE_ID)
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
        elif operation == "sync_file_replicas":
            # Sincronizar tabla file_replicas desde el líder
            from namenode.database import get_db_path, get_connection, close_connection, db_lock
            
            db_path = get_db_path(node_id=NODE_ID)
            file_replicas_data = operation_data.get("file_replicas", [])
            
            with db_lock:
                conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
                try:
                    # Limpiar tabla file_replicas (sincronización completa)
                    cursor.execute("DELETE FROM file_replicas")
                    
                    # Insertar todas las réplicas del líder
                    for replica in file_replicas_data:
                        cursor.execute("""
                            INSERT OR REPLACE INTO file_replicas (file_id, datanode_id, replica_type)
                            VALUES (?, ?, ?)
                        """, (replica["file_id"], replica["datanode_id"], replica["replica_type"]))
                    
                    conn.commit()
                    
                    changes = operation_data.get("changes", {})
                    change_count = (
                        len(changes.get("added", [])) +
                        len(changes.get("removed", [])) +
                        len(changes.get("modified", []))
                    )
                    print(f"[NAMENODE] [REPLICATE] ✅ Tabla file_replicas sincronizada: {len(file_replicas_data)} réplicas ({change_count} cambios)")
                except Exception as e:
                    conn.rollback()
                    print(f"[NAMENODE] [REPLICATE] ❌ Error sincronizando file_replicas: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    close_connection(conn)
        elif operation == "sync_datanodes":
            # Sincronizar tabla datanodes desde el líder
            from namenode.database import get_db_path, get_connection, close_connection, db_lock
            
            db_path = get_db_path(node_id=NODE_ID)
            datanodes_data = operation_data.get("datanodes", [])
            
            with db_lock:
                conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
                try:
                    # Limpiar tabla datanodes (sincronización completa)
                    cursor.execute("DELETE FROM datanodes")
                    
                    # Insertar todos los datanodes del líder
                    for datanode in datanodes_data:
                        cursor.execute("""
                            INSERT OR REPLACE INTO datanodes 
                            (node_id, url, port, ip, total_space, free_space, 
                             last_heartbeat, status, draining, registered_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            datanode["node_id"],
                            datanode["url"],
                            datanode["port"],
                            datanode.get("ip"),
                            datanode["total_space"],
                            datanode["free_space"],
                            datanode.get("last_heartbeat"),
                            datanode["status"],
                            1 if datanode.get("draining") else 0,
                            datanode.get("registered_at")
                        ))
                    
                    conn.commit()
                    
                    changes = operation_data.get("changes", {})
                    change_count = (
                        len(changes.get("added", [])) +
                        len(changes.get("removed", [])) +
                        len(changes.get("modified", []))
                    )
                    print(f"[NAMENODE] [REPLICATE] ✅ Tabla datanodes sincronizada: {len(datanodes_data)} datanodes ({change_count} cambios)")
                    
                    # Actualizar cache de datanodes si está disponible
                    try:
                        from namenode.datanode_cache import get_datanode_cache
                        cache = get_datanode_cache(node_id_db=NODE_ID)
                        for datanode in datanodes_data:
                            cache.update(datanode["node_id"], {
                                "url": datanode["url"],
                                "port": datanode["port"],
                                "ip": datanode.get("ip"),
                                "total_space": datanode["total_space"],
                                "free_space": datanode["free_space"],
                                "last_heartbeat": datanode.get("last_heartbeat"),
                                "status": datanode["status"],
                                "draining": datanode.get("draining", False)
                            })
                        print(f"[NAMENODE] [REPLICATE] ✅ Cache de datanodes actualizado")
                    except Exception as cache_error:
                        print(f"[NAMENODE] [REPLICATE] ⚠️  No se pudo actualizar cache de datanodes: {cache_error}")
                
                except Exception as e:
                    conn.rollback()
                    print(f"[NAMENODE] [REPLICATE] ❌ Error sincronizando datanodes: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    close_connection(conn)
        
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
        
        # Log detallado de recepción
        datanodes_writes_count = sum(1 for w in sql_writes if 'datanodes' in w.get('sql', '').lower())
        print(f"[NAMENODE] [SQL_REPLICATE] 📥 Recibida solicitud de replicación: {len(sql_writes)} escrituras SQL (término={term}, timestamp={timestamp})")
        if datanodes_writes_count > 0:
            print(f"[NAMENODE] [SQL_REPLICATE] 📥 ⚠️ IMPORTANTE: {datanodes_writes_count} escrituras en tabla 'datanodes' recibidas")
        
        with cluster_lock:
            current_term = cluster_state["term"]
            
            # Actualizar término si es mayor
            if term > current_term:
                cluster_state["term"] = term
                cluster_state["is_leader"] = False
                cluster_state["leader_id"] = data.get("leader_id")
                cluster_state["last_heartbeat_time"] = time.time()
        
        # NOTA: NO inicializar las bases de datos aquí en cada replicación SQL
        # Las bases de datos ya deberían estar inicializadas al iniciar el nodo.
        # Solo se inicializarán automáticamente si se detecta que falta una tabla
        # (ver manejo de excepciones sqlite3.OperationalError más abajo)
        
        # Aplicar escrituras SQL localmente
        from namenode.database import get_connection, close_connection, metadata_db_lock, operations_db_lock
        import sqlite3
        
        applied_count = 0
        skipped_count = 0
        error_count = 0
        
        for idx, write in enumerate(sql_writes):
            sql = write.get("sql")
            params = write.get("params")
            db_type = write.get("db_type", "metadata")
            
            if not sql:
                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Escritura {idx+1} sin SQL, saltando...")
                continue
            
            # Identificar si es escritura en datanodes para logging especial
            is_datanodes_table = 'datanodes' in sql.lower()
            table_info = "tabla 'datanodes'" if is_datanodes_table else f"db_type={db_type}"
            
            print(f"[NAMENODE] [SQL_REPLICATE] 🔄 [{idx+1}/{len(sql_writes)}] Aplicando escritura ({table_info}): {sql[:70]}...")
            
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
                            
                            # Logging detallado para UPDATEs en datanodes que incluyen IP antes de ejecutar
                            if is_datanodes_table and "UPDATE" in sql.upper() and len(deserialized_params) == 8:
                                try:
                                    url, port, ip, total_space, free_space, last_heartbeat, status, node_id = deserialized_params
                                    ip_info = f" (IP deserializada: {ip})" if ip else " (IP deserializada: NULL)"
                                    print(f"[NAMENODE] [SQL_REPLICATE] 🔄 Antes de ejecutar UPDATE completo: node_id={node_id}, url={url}, port={port}{ip_info}")
                                except Exception as e:
                                    print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error parseando parámetros antes de ejecutar: {e}, params={deserialized_params}")
                            
                            cursor.execute(sql, deserialized_params)
                        else:
                            # Parámetro único
                            if isinstance(params, dict) and params.get('__type__') == 'bytes':
                                import base64
                                params = base64.b64decode(params['__value__'])
                            cursor.execute(sql, params)
                    else:
                        cursor.execute(sql)
                    
                    # Verificar que la ejecución fue exitosa antes de commit
                    rowcount = cursor.rowcount if hasattr(cursor, 'rowcount') else 0
                    
                    # Logging especial para UPDATEs en datanodes que incluyen IP
                    if is_datanodes_table and "UPDATE" in sql.upper() and params and isinstance(params, list):
                        if len(params) == 8:
                            # UPDATE completo con IP
                            try:
                                url, port, ip, total_space, free_space, last_heartbeat, status, node_id = params
                                ip_info = f" (IP: {ip})" if ip else " (IP: NULL)"
                                print(f"[NAMENODE] [SQL_REPLICATE] 📥 UPDATE completo en datanodes: node_id={node_id}, url={url}, port={port}{ip_info}, rowcount={rowcount}")
                            except Exception as e:
                                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error parseando parámetros de UPDATE completo: {e}")
                    
                    # CORRECCIÓN CRÍTICA: Si es UPDATE en datanodes y no afectó filas, convertir a INSERT OR REPLACE
                    if is_datanodes_table and "UPDATE" in sql.upper() and rowcount == 0:
                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ UPDATE en 'datanodes' afectó 0 filas - el datanode no existe en este seguidor")
                        print(f"[NAMENODE] [SQL_REPLICATE] 💡 Convirtiendo UPDATE a INSERT OR REPLACE para sincronizar inmediatamente...")
                        
                        # IMPORTANTE: Antes de hacer rollback, intentar obtener valores existentes si el datanode existe parcialmente
                        existing_url = None
                        existing_port = None
                        existing_ip = None
                        try:
                            if params and isinstance(params, list) and len(params) >= 1:
                                node_id_param = params[-1]
                                cursor.execute("SELECT url, port, ip FROM datanodes WHERE node_id = ?", (node_id_param,))
                                existing_row = cursor.fetchone()
                                if existing_row:
                                    existing_url, existing_port, existing_ip = existing_row[0], existing_row[1], existing_row[2]
                                    print(f"[NAMENODE] [SQL_REPLICATE] 💡 Datanode parcial encontrado - usando valores existentes de url/port/ip")
                        except Exception as fetch_e:
                            print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error obteniendo valores existentes: {fetch_e}")
                        
                        # Hacer rollback del UPDATE sin efecto
                        conn.rollback()
                        
                        # Intentar convertir UPDATE a INSERT OR REPLACE
                        try:
                            if params:
                                # Hay dos tipos de UPDATE en datanodes:
                                # 1. UPDATE de registro completo: UPDATE datanodes SET url=?, port=?, ip=?, total_space=?, free_space=?, last_heartbeat=?, status=? WHERE node_id=?
                                #    Parámetros: [url, port, ip, total_space, free_space, last_heartbeat, status, node_id] - 8 parámetros
                                # 2. UPDATE de heartbeat: UPDATE datanodes SET free_space=?, total_space=?, last_heartbeat=?, status=? WHERE node_id=?
                                #    Parámetros: [free_space, total_space, last_heartbeat, status, node_id] - 5 parámetros
                                
                                if isinstance(params, list):
                                    node_id_param = params[-1]  # El node_id siempre es el último parámetro
                                    
                                    if len(params) == 8:
                                        # UPDATE completo con todos los campos
                                        url, port, ip, total_space, free_space, last_heartbeat, status, node_id = params
                                        
                                        # Log de la IP recibida
                                        ip_info = f" (IP: {ip})" if ip else " (IP: NULL)"
                                        print(f"[NAMENODE] [SQL_REPLICATE] 📥 UPDATE completo recibido para datanode '{node_id}': url={url}, port={port}{ip_info}")
                                        
                                        # Insertar o reemplazar el datanode con todos los campos
                                        cursor.execute("""
                                            INSERT OR REPLACE INTO datanodes 
                                            (node_id, url, port, ip, total_space, free_space, last_heartbeat, status, draining)
                                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                                        """, (node_id, url, port, ip, total_space, free_space, last_heartbeat, status))
                                        
                                        conn.commit()
                                        applied_count += 1
                                        print(f"[NAMENODE] [SQL_REPLICATE] ✅ UPDATE completo convertido a INSERT OR REPLACE exitosamente para datanode '{node_id}'{ip_info}")
                                        
                                        # Verificar que se guardó correctamente
                                        cursor.execute("SELECT ip FROM datanodes WHERE node_id = ?", (node_id,))
                                        saved_ip = cursor.fetchone()
                                        if saved_ip:
                                            saved_ip_value = saved_ip[0]
                                            if saved_ip_value != ip:
                                                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ ADVERTENCIA: IP guardada ({saved_ip_value}) no coincide con IP recibida ({ip})")
                                            else:
                                                print(f"[NAMENODE] [SQL_REPLICATE] ✅ IP verificada correctamente: {saved_ip_value}")
                                        
                                        continue  # Saltar el commit normal ya que lo hicimos arriba
                                        
                                    elif len(params) == 5:
                                        # UPDATE de heartbeat - solo tiene free_space, total_space, last_heartbeat, status, node_id
                                        # No tenemos url, port, ip que son NOT NULL
                                        # Usar valores existentes si están disponibles, o construir la URL correcta
                                        free_space, total_space, last_heartbeat, status, node_id = params
                                        
                                        # Usar valores existentes si los obtuvimos antes del rollback
                                        if existing_url and existing_port:
                                            # El datanode existe parcialmente, usar valores existentes (incluyendo IP si existe)
                                            url, port, ip = existing_url, existing_port, existing_ip
                                            ip_info = f" (IP: {ip})" if ip else " (IP: NULL)"
                                            print(f"[NAMENODE] [SQL_REPLICATE] 💡 Usando valores existentes de url/port/ip del datanode parcial{ip_info}")
                                        else:
                                            # No existe, construir la URL correcta basándose en el node_id
                                            # El patrón es: node_id = "datanode-X" -> url = "http://tbfs-datanode-X:8001"
                                            # Extraer el número del datanode del node_id
                                            import re
                                            datanode_match = re.search(r'datanode[-_]?(\d+)', node_id, re.IGNORECASE)
                                            if datanode_match:
                                                datanode_num = datanode_match.group(1)
                                                url = f"http://tbfs-datanode-{datanode_num}:8001"
                                                port = 8001
                                                print(f"[NAMENODE] [SQL_REPLICATE] 💡 Construyendo URL desde node_id: {node_id} -> {url}:{port}")
                                            else:
                                                # Fallback: usar el node_id directamente en la URL
                                                url = f"http://tbfs-{node_id}:8001"
                                                port = 8001
                                                print(f"[NAMENODE] [SQL_REPLICATE] 💡 Construyendo URL fallback desde node_id: {node_id} -> {url}:{port}")
                                            
                                            # Intentar resolver la IP del hostname
                                            ip = None
                                            try:
                                                import socket
                                                hostname = f"tbfs-datanode-{datanode_match.group(1)}" if datanode_match else f"tbfs-{node_id}"
                                                ip = socket.gethostbyname(hostname)
                                                print(f"[NAMENODE] [SQL_REPLICATE] ✅ IP resuelta para {hostname}: {ip}")
                                            except Exception as dns_e:
                                                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ No se pudo resolver IP para {hostname}: {dns_e}")
                                                ip = None
                                        
                                        # Insertar o reemplazar con todos los campos requeridos
                                        # IMPORTANTE: Si el datanode ya existe, preservar la IP existente si no tenemos una nueva
                                        cursor.execute("SELECT ip FROM datanodes WHERE node_id = ?", (node_id,))
                                        existing_ip_row = cursor.fetchone()
                                        if existing_ip_row and existing_ip_row[0] and not ip:
                                            # Si ya existe una IP y no tenemos una nueva, preservarla
                                            ip = existing_ip_row[0]
                                            print(f"[NAMENODE] [SQL_REPLICATE] 💡 Preservando IP existente: {ip}")
                                        
                                        cursor.execute("""
                                            INSERT OR REPLACE INTO datanodes 
                                            (node_id, url, port, ip, total_space, free_space, last_heartbeat, status, draining)
                                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                                        """, (node_id, url, port, ip, total_space, free_space, last_heartbeat, status))
                                        
                                        conn.commit()
                                        applied_count += 1
                                        ip_info_final = f" (IP: {ip})" if ip else " (IP: NULL)"
                                        print(f"[NAMENODE] [SQL_REPLICATE] ✅ UPDATE de heartbeat convertido a INSERT OR REPLACE exitosamente para datanode '{node_id}' -> {url}:{port}{ip_info_final}")
                                        
                                        # Continuar para verificar que se guardó
                                        cursor.execute("SELECT url, port, ip FROM datanodes WHERE node_id = ?", (node_id,))
                                        saved_row = cursor.fetchone()
                                        if saved_row:
                                            print(f"[NAMENODE] [SQL_REPLICATE] ✅ Datanode '{node_id}' ahora existe en BD del seguidor: url={saved_row[0]}, port={saved_row[1]}, ip={saved_row[2]}")
                                        
                                        continue  # Saltar el commit normal ya que lo hicimos arriba
                                    
                                    else:
                                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ No se pudo convertir UPDATE - formato desconocido ({len(params)} parámetros). Datanode '{node_id_param}' necesita sincronización completa")
                                        skipped_count += 1
                                        continue
                                    
                                    # Verificar que se guardó
                                    cursor.execute("SELECT COUNT(*) FROM datanodes WHERE node_id = ?", (node_id_param,))
                                    exists_after = cursor.fetchone()[0] > 0
                                    if exists_after:
                                        print(f"[NAMENODE] [SQL_REPLICATE] ✅ Datanode '{node_id_param}' ahora existe en BD del seguidor")
                                    else:
                                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error: Datanode '{node_id_param}' no se guardó después de INSERT OR REPLACE")
                                    
                                    continue  # Saltar el commit normal ya que lo hicimos arriba
                                else:
                                    print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Parámetros no son una lista, no se puede convertir UPDATE")
                                    skipped_count += 1
                                    continue
                        except Exception as convert_e:
                            print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error convirtiendo UPDATE a INSERT OR REPLACE: {convert_e}")
                            import traceback
                            traceback.print_exc()
                            skipped_count += 1
                            continue
                    
                    # Hacer commit y verificar que se persistió
                    conn.commit()
                    
                    # VERIFICACIÓN: Leer inmediatamente después del commit para confirmar persistencia
                    if is_datanodes_table:
                        # Si es INSERT o UPDATE en datanodes, verificar que se guardó
                        try:
                            if "INSERT" in sql.upper() or "UPDATE" in sql.upper():
                                # Extraer node_id de los parámetros si están disponibles
                                check_sql = "SELECT COUNT(*) FROM datanodes"
                                cursor.execute(check_sql)
                                count_after = cursor.fetchone()[0]
                                print(f"[NAMENODE] [SQL_REPLICATE] ✅ Escritura en 'datanodes' aplicada exitosamente (filas afectadas: {rowcount}, total datanodes en BD: {count_after})")
                            else:
                                print(f"[NAMENODE] [SQL_REPLICATE] ✅ Escritura en 'datanodes' aplicada exitosamente (filas afectadas: {rowcount})")
                        except Exception as verify_e:
                            print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error verificando persistencia de 'datanodes': {verify_e}")
                            print(f"[NAMENODE] [SQL_REPLICATE] ✅ Escritura en 'datanodes' aplicada exitosamente (filas afectadas: {rowcount})")
                    else:
                        print(f"[NAMENODE] [SQL_REPLICATE] ✅ Escritura aplicada exitosamente (filas afectadas: {rowcount})")
                    
                    applied_count += 1
                except sqlite3.IntegrityError as e:
                    # Error de UNIQUE constraint - el dato ya existe, esto es normal en replicación
                    conn.rollback()
                    error_msg = str(e)
                    skipped_count += 1
                    # Loguear todos los errores de integridad para diagnóstico
                    if is_datanodes_table:
                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error de integridad en 'datanodes' (esperado si es duplicado): {sql[:70]}... - {error_msg}")
                    else:
                        if "UNIQUE constraint" not in error_msg:
                            print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error de integridad: {sql[:50]}... - {e}")
                except sqlite3.OperationalError as e:
                    error_msg = str(e)
                    conn.rollback()
                    
                    # Si la tabla no existe, intentar crearla (caso excepcional)
                    if "no such table" in error_msg.lower():
                        table_name = error_msg.split("no such table:")[-1].strip().split()[0] if "no such table:" in error_msg.lower() else "unknown"
                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Tabla faltante detectada: '{table_name}' en BD '{db_type}', inicializando BD...")
                        print(f"[NAMENODE] [SQL_REPLICATE] SQL que causó el error: {sql[:100]}...")
                        try:
                            # Importar aquí solo cuando sea necesario (evitar imports innecesarios)
                            from namenode.database import init_metadata_db, init_operations_db
                            # Cerrar la conexión anterior antes de reinicializar
                            try:
                                close_connection(conn)
                            except:
                                pass
                            
                            # Re-inicializar la base de datos correspondiente según db_type
                            if db_type == "operations":
                                print(f"[NAMENODE] [SQL_REPLICATE] Inicializando BD de OPERACIONES...")
                                init_operations_db(node_id=NODE_ID)
                            else:
                                print(f"[NAMENODE] [SQL_REPLICATE] Inicializando BD de METADATOS...")
                                init_metadata_db(node_id=NODE_ID)
                            
                            # Reintentar la operación con nueva conexión
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
                            print(f"[NAMENODE] [SQL_REPLICATE] ✅ Tabla '{table_name}' creada y operación aplicada: {sql[:50]}...")
                        except Exception as retry_e:
                            print(f"[NAMENODE] [SQL_REPLICATE] ❌ Error después de crear tabla '{table_name}': {retry_e}")
                            print(f"[NAMENODE] [SQL_REPLICATE] SQL problemático: {sql[:200]}")
                            print(f"[NAMENODE] [SQL_REPLICATE] db_type: {db_type}")
                            import traceback
                            traceback.print_exc()
                            try:
                                conn.rollback()
                            except:
                                pass
                    else:
                        print(f"[NAMENODE] [SQL_REPLICATE] Error operacional: {sql[:50]}... - {e}")
                        import traceback
                        traceback.print_exc()
                except Exception as e:
                    conn.rollback()
                    error_count += 1
                    error_msg = f"[NAMENODE] [SQL_REPLICATE] ❌ Error aplicando SQL: {sql[:70]}... - {e}"
                    if is_datanodes_table:
                        error_msg = f"[NAMENODE] [SQL_REPLICATE] ❌ ⚠️⚠️⚠️ ERROR CRÍTICO en 'datanodes': {sql[:70]}... - {e}"
                    print(error_msg)
                    import traceback
                    traceback.print_exc()
                finally:
                    # IMPORTANTE: Asegurar que el commit se haya completado antes de cerrar
                    try:
                        # Si la conexión tiene in_transaction, verificar que no hay transacciones pendientes
                        if hasattr(conn, '_conn') and hasattr(conn._conn, 'in_transaction'):
                            if conn._conn.in_transaction:
                                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ ADVERTENCIA: Transacción pendiente antes de cerrar conexión")
                                conn._conn.commit()
                    except Exception as e:
                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error verificando transacción antes de cerrar: {e}")
                    
                    conn._is_replicating = False
                    # Cerrar la conexión real (el wrapper delega close() a _conn)
                    close_connection(conn)
        
        # VERIFICACIÓN FINAL: Confirmar que los datos en 'datanodes' están en la BD
        needs_full_sync = False
        if datanodes_writes_count > 0:
            try:
                from namenode.database import get_connection, close_connection
                conn_check, cursor_check = get_connection(db_type="metadata", node_id=NODE_ID)
                try:
                    cursor_check.execute("SELECT COUNT(*) FROM datanodes")
                    final_count = cursor_check.fetchone()[0]
                    print(f"[NAMENODE] [SQL_REPLICATE] 🔍 VERIFICACIÓN FINAL: Total de datanodes en BD después de replicación: {final_count}")
                    
                    # Si recibimos escrituras en datanodes pero la BD está vacía o hay muy pocos,
                    # y aplicamos menos escrituras de las esperadas, necesitamos sincronización completa
                    if final_count == 0 and applied_count < datanodes_writes_count:
                        needs_full_sync = True
                        print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ DETECTADO: BD de datanodes vacía después de replicación SQL")
                        print(f"[NAMENODE] [SQL_REPLICATE] 💡 SOLUCIÓN: Solicitar sincronización completa de datanodes al líder")
                finally:
                    close_connection(conn_check)
            except Exception as verify_e:
                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error en verificación final de 'datanodes': {verify_e}")
        
        # SOLUCIÓN: Solicitar sincronización completa si es necesario
        if needs_full_sync:
            try:
                from namenode.namenode import get_leader_url
                leader_url = get_leader_url()
                if leader_url:
                    print(f"[NAMENODE] [SQL_REPLICATE] 🔄 Solicitando sincronización completa de datanodes al líder: {leader_url}")
                    # Intentar obtener los datanodes del líder directamente
                    # Nota: Esto podría requerir un endpoint adicional, pero por ahora solo logueamos
                    print(f"[NAMENODE] [SQL_REPLICATE] 💡 La sincronización completa se realizará automáticamente en el próximo ciclo de sync_datanodes")
                else:
                    print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ No se puede solicitar sincronización completa: no hay líder conocido")
            except Exception as sync_e:
                print(f"[NAMENODE] [SQL_REPLICATE] ⚠️ Error solicitando sincronización completa: {sync_e}")
        
        # Log final detallado
        final_msg = f"[NAMENODE] [SQL_REPLICATE] ✅ {applied_count}/{len(sql_writes)} escrituras SQL aplicadas"
        if skipped_count > 0:
            final_msg += f" ({skipped_count} duplicados ignorados)"
        if error_count > 0:
            final_msg += f" ({error_count} errores)"
        if datanodes_writes_count > 0:
            final_msg += f" - IMPORTANTE: {datanodes_writes_count} escrituras en 'datanodes' procesadas"
        print(final_msg)
        
        return {"success": True, "applied": applied_count, "skipped": skipped_count, "errors": error_count}
        
    except Exception as e:
        print(f"[NAMENODE] [SQL_REPLICATE] Error en internal_replicate_sql: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


@app.get("/internal/get-datanodes")
def internal_get_datanodes(
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Endpoint interno para obtener la tabla completa de datanodes de este nodo.
    Usado durante la reconciliación para fusionar información de todos los peers.
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
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        raise HTTPException(status_code=403, detail="Solo namenodes pueden obtener datanodes")
    
    try:
        # Obtener todos los datanodes de este nodo
        db_path = get_db_path(NODE_ID)
        
        from namenode.rw_lock import ReadLock
        from namenode.database import metadata_rw_lock
        
        datanodes = []
        with ReadLock(metadata_rw_lock):
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            
            try:
                cursor.execute("""
                    SELECT node_id, url, port, ip, total_space, free_space, 
                           last_heartbeat, status, registered_at, draining
                    FROM datanodes
                """)
                
                for row in cursor.fetchall():
                    datanodes.append({
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
            finally:
                close_connection(conn)
        
        print(f"[NAMENODE] 📤 [GET_DATANODES] Compartiendo tabla de datanodes: {len(datanodes)} entradas (solicitado por {service_id})")
        
        return {
            "node_id": NODE_ID,
            "datanodes": datanodes,
            "timestamp": time.time(),
            "count": len(datanodes)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMENODE] ❌ [GET_DATANODES] Error: {e}")
        import traceback
        traceback.print_exc()
        return {"node_id": NODE_ID, "datanodes": [], "error": str(e)}


@app.post("/internal/sync-datanodes")
def internal_sync_datanodes(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Endpoint interno para recibir sincronización completa de la tabla datanodes del líder.
    Este endpoint reemplaza completamente la tabla datanodes local con el snapshot del líder.
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
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        raise HTTPException(status_code=403, detail="Solo namenodes pueden sincronizar datanodes")
    
    try:
        datanodes = data.get("datanodes", [])
        term = data.get("term")
        leader_id = data.get("leader_id")
        timestamp = data.get("timestamp")
        
        print(f"[NAMENODE] 🔄 [SYNC_DATANODES] Recibido snapshot de datanodes del líder {leader_id}")
        print(f"[NAMENODE] 🔄 [SYNC_DATANODES] Term: {term}, Timestamp: {timestamp}")
        print(f"[NAMENODE] 🔄 [SYNC_DATANODES] Total datanodes en snapshot: {len(datanodes)}")
        
        # Actualizar término si es necesario
        with cluster_lock:
            current_term = cluster_state["term"]
            if term > current_term:
                cluster_state["term"] = term
                cluster_state["is_leader"] = False
                cluster_state["leader_id"] = leader_id
                cluster_state["last_heartbeat_time"] = time.time()
        
        # Sincronizar tabla datanodes (optimizado para reducir tiempo con lock)
        db_path = get_db_path(NODE_ID)
        
        # Preparar datos fuera del lock
        old_datanodes = set()
        new_datanodes = {dn["node_id"] for dn in datanodes}
        inserted_count = 0
        
        # Adquirir write lock solo durante la escritura
        with db_lock:
            conn, cursor = get_connection(db_path=db_path, node_id=NODE_ID)
            
            try:
                # Marcar que estamos replicando para evitar replicación recursiva
                conn._is_replicating = True
                
                # Obtener estado actual antes de sincronizar (rápido)
                cursor.execute("SELECT node_id FROM datanodes")
                old_datanodes = {row[0] for row in cursor.fetchall()}
                
                # Limpiar tabla datanodes (sincronización completa)
                cursor.execute("DELETE FROM datanodes")
                
                # Preparar batch insert para ser más eficiente
                insert_values = []
                for datanode in datanodes:
                    insert_values.append((
                        datanode["node_id"],
                        datanode["url"],
                        datanode["port"],
                        datanode.get("ip"),
                        datanode["total_space"],
                        datanode["free_space"],
                        datanode.get("last_heartbeat"),
                        datanode["status"],
                        1 if datanode.get("draining") else 0,
                        datanode.get("registered_at")
                    ))
                
                # Insertar todos de una vez (batch insert - mucho más rápido)
                if insert_values:
                    cursor.executemany("""
                        INSERT INTO datanodes 
                        (node_id, url, port, ip, total_space, free_space, 
                         last_heartbeat, status, draining, registered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, insert_values)
                    inserted_count = len(insert_values)
                
                conn.commit()
                
            except Exception as e:
                conn.rollback()
                print(f"[NAMENODE] ❌ [SYNC_DATANODES] Error sincronizando tabla: {e}")
                import traceback
                traceback.print_exc()
                raise
            finally:
                close_connection(conn)
        
        # Calcular diferencias y logging fuera del lock
        added = new_datanodes - old_datanodes
        removed = old_datanodes - new_datanodes
        
        print(f"[NAMENODE] ✅ [SYNC_DATANODES] Sincronización completada:")
        print(f"[NAMENODE] ✅ [SYNC_DATANODES]   - Total insertados: {inserted_count}")
        print(f"[NAMENODE] ✅ [SYNC_DATANODES]   - Nuevos datanodes: {len(added)}")
        print(f"[NAMENODE] ✅ [SYNC_DATANODES]   - Datanodes removidos: {len(removed)}")
        
        # Actualizar cache de datanodes (fuera del lock)
        try:
            from namenode.datanode_cache import get_datanode_cache
            cache = get_datanode_cache(node_id_db=NODE_ID)
            
            # Invalidar cache para forzar recarga desde BD
            cache.invalidate()
            
            print(f"[NAMENODE] ✅ [SYNC_DATANODES] Cache de datanodes invalidado")
        except Exception as cache_error:
            print(f"[NAMENODE] ⚠️  [SYNC_DATANODES] Error actualizando cache: {cache_error}")
        
        return {
            "success": True,
            "inserted": inserted_count,
            "added": len(added),
            "removed": len(removed)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMENODE] ❌ [SYNC_DATANODES] Error en internal_sync_datanodes: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


# Variable global para almacenar propuestas de consenso pendientes
_consensus_proposals = {}
_consensus_lock = threading.Lock()


@app.post("/internal/consensus-propose")
def internal_consensus_propose(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Fase 1 del protocolo de consenso: PROPOSE
    El coordinador propone un orden de operaciones a los followers.
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
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        raise HTTPException(status_code=403, detail="Solo namenodes pueden proponer consenso")
    
    try:
        coordinator_id = data.get("coordinator_id")
        term = data.get("term")
        operations_data = data.get("operations", [])
        proposed_checksum = data.get("checksum")
        operation_count = data.get("operation_count", len(operations_data))
        
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] Recibida propuesta de {coordinator_id}")
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] Term: {term}, Operaciones: {operation_count}")
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] Checksum propuesto: {proposed_checksum[:16]}...")
        print(f"[DEBUG] 🔍 PROPOSE recibido: {operation_count} operaciones, checksum={proposed_checksum}")
        
        # Verificar que el term es válido
        with cluster_lock:
            current_term = cluster_state["term"]
            if term < current_term:
                print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] ❌ Rechazado: term {term} < current term {current_term}")
                return {
                    "accepted": False,
                    "message": f"Term obsoleto: {term} < {current_term}",
                    "current_term": current_term
                }
        
        # Reconstruir operaciones desde los datos
        operations = []
        for op_data in operations_data:
            operations.append(OperationLog(
                operation=op_data["operation"],
                data=op_data["data"],
                term=op_data["term"],
                timestamp=op_data["timestamp"]
            ))
        
        # Verificar checksum
        calculated_checksum = calculate_operations_checksum(operations)
        
        if calculated_checksum != proposed_checksum:
            print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] ❌ Checksums no coinciden!")
            print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE]   Propuesto: {proposed_checksum[:16]}...")
            print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE]   Calculado: {calculated_checksum[:16]}...")
            return {
                "accepted": False,
                "message": "Checksum no coincide",
                "proposed_checksum": proposed_checksum,
                "calculated_checksum": calculated_checksum
            }
        
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] ✅ Checksum verificado correctamente")
        
        # Guardar propuesta pendiente
        with _consensus_lock:
            _consensus_proposals[proposed_checksum] = {
                "coordinator_id": coordinator_id,
                "term": term,
                "operations": operations,
                "checksum": proposed_checksum,
                "timestamp": time.time(),
                "applied": False
            }
        
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] ✅ Propuesta aceptada, esperando COMMIT")
        
        return {
            "accepted": True,
            "checksum": calculated_checksum,
            "message": f"Propuesta aceptada: {operation_count} operaciones",
            "node_id": NODE_ID
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMENODE] 🤝 [CONSENSUS-PROPOSE] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "accepted": False,
            "message": f"Error procesando propuesta: {str(e)}"
        }


@app.post("/internal/consensus-commit")
def internal_consensus_commit(
    data: Dict,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Fase 3 del protocolo de consenso: COMMIT
    El coordinador notifica que el consenso fue alcanzado y se deben aplicar las operaciones.
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
    if not (service_id.startswith("namenode-") or service_id.startswith("tbfs-namenode-")):
        raise HTTPException(status_code=403, detail="Solo namenodes pueden confirmar consenso")
    
    try:
        coordinator_id = data.get("coordinator_id")
        term = data.get("term")
        checksum = data.get("checksum")
        
        print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] Recibida confirmación de {coordinator_id}")
        print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] Term: {term}, Checksum: {checksum[:16]}...")
        
        # Buscar propuesta pendiente
        with _consensus_lock:
            proposal = _consensus_proposals.get(checksum)
        
        if not proposal:
            print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] ❌ No hay propuesta pendiente para este checksum")
            return {
                "committed": False,
                "message": "No hay propuesta pendiente para este checksum"
            }
        
        if proposal["applied"]:
            print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] ⚠️  Propuesta ya fue aplicada anteriormente")
            return {
                "committed": True,
                "message": "Operaciones ya fueron aplicadas",
                "node_id": NODE_ID
            }
        
        # Aplicar operaciones Y guardarlas en el log
        print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] Aplicando {len(proposal['operations'])} operaciones...")
        
        applied_count = 0
        for operation in proposal['operations']:
            try:
                # Aplicar operación
                apply_operation_safely(operation, NODE_ID)
                
                # CRÍTICO: Guardar en log persistente para que no se pierda
                save_operation_to_log(operation, NODE_ID)
                
                # Agregar al log en memoria
                with log_lock:
                    # Verificar si ya existe antes de agregar
                    op_key = get_operation_key(operation)
                    already_exists = any(
                        get_operation_key(op) == op_key and 
                        abs(op.timestamp - operation.timestamp) < 0.1
                        for op in operation_log
                    )
                    if not already_exists:
                        operation_log.append(operation)
                
                applied_count += 1
                
                # Log detallado de cada operación aplicada
                if operation.operation == "add_file":
                    print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT]   ✅ {operation.operation}: {operation.data.get('name', 'N/A')} (term={operation.term})")
                elif operation.operation == "delete_file":
                    print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT]   ✅ {operation.operation}: {operation.data.get('name', 'N/A')} (term={operation.term})")
                else:
                    print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT]   ✅ {operation.operation} (term={operation.term})")
                    
            except Exception as e:
                print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] ⚠️  Error aplicando operación {operation.operation}: {e}")
                import traceback
                traceback.print_exc()
        
        # Marcar como aplicado
        with _consensus_lock:
            proposal["applied"] = True
            proposal["applied_at"] = time.time()
        
        # Actualizar estado del cluster
        with cluster_lock:
            cluster_state["last_consensus_checksum"] = checksum
            cluster_state["last_consensus_term"] = term
            if term > cluster_state["term"]:
                cluster_state["term"] = term
        
        print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] ✅ {applied_count}/{len(proposal['operations'])} operaciones aplicadas y guardadas en log")
        
        return {
            "committed": True,
            "applied_count": applied_count,
            "total_operations": len(proposal['operations']),
            "message": f"Consenso aplicado: {applied_count} operaciones",
            "node_id": NODE_ID
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"[NAMENODE] 🤝 [CONSENSUS-COMMIT] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return {
            "committed": False,
            "message": f"Error aplicando consenso: {str(e)}"
        }


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
            # Crear set completo con todas las variaciones posibles del nodo actual
            node_id_variations = {
                current_node_id,
                f"tbfs-{current_node_id}",
                current_node_id.replace("tbfs-", ""),
                current_node_id.replace("namenode-", "") if "namenode" in current_node_id else current_node_id
            }
            
            # Agregar IP local si está disponible
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                local_ip = s.getsockname()[0]
                s.close()
                node_id_variations.add(local_ip)
            except Exception:
                pass
            
            # Filtrar TODAS las variaciones del nodo actual
            updated_peers = [p for p in peers_from_leader if p not in node_id_variations]
            
            # Asegurar que el líder esté incluido en la lista de peers conocidos
            # SOLO si no es este nodo (verificar contra todas las variaciones)
            if leader_id and leader_id not in node_id_variations:
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
            # Usar las mismas variaciones que ya calculamos arriba
            updated_active_followers = [f for f in active_followers_from_leader if f not in node_id_variations]
            
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
