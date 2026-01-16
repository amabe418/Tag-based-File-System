"""
Autenticación JWT y autorización RBAC para clientes
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
import os
import sqlite3
from pathlib import Path

from security.models import User, Role, Permission, UserLogin, UserCreate, PasswordChange

# Configuración JWT
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))  # 8 horas por defecto asi evitamos que el token expire en el medio de una operacion

# Configuración de hash de contraseñas
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Security scheme
security = HTTPBearer()

# Base de datos de usuarios - ahora usa la base de datos del namenode
def get_users_db_path(node_id: str = None) -> str:
    """Obtiene la ruta de la base de datos del namenode que contiene los usuarios"""
    try:
        from namenode.database import get_db_path
    except ImportError:
        # Si namenode no está disponible (ej: registry, datanode), usar base de datos local
        # Esto es un fallback para servicios que no son namenode
        DB_PATH = os.getenv("USERS_DB_PATH", "security/users.db")
        USERS_DB_DIR = Path(DB_PATH).parent
        USERS_DB_DIR.mkdir(parents=True, exist_ok=True)
        return DB_PATH
    
    if node_id:
        # Si se proporciona node_id, usar la base de datos de ese namenode
        return get_db_path(node_id)
    else:
        # Usar variable de entorno o fallback
        node_id = os.getenv("NODE_ID", "namenode-1")
        return get_db_path(node_id)


def _normalize_usernames(cursor, conn):
    """Normaliza usernames a minúsculas para evitar problemas de login"""
    try:
        cursor.execute("UPDATE users SET username = lower(username)")
        conn.commit()
    except Exception as e:
        # Si hay conflicto por duplicados, registrar y continuar
        print(f"[SECURITY] No se pudo normalizar usernames: {e}")
        conn.rollback()


def init_users_db(node_id: str = None):
    """
    Inicializa la base de datos de usuarios en el namenode.
    La tabla de usuarios se crea automáticamente en init_db del namenode.
    Esta función solo asegura que el usuario admin exista.
    
    NOTA: Esta función solo debe ser llamada desde el namenode, no desde otros servicios.
    """
    try:
        db_path = get_users_db_path(node_id)
    except Exception as e:
        # Si no se puede obtener la ruta (ej: servicio que no es namenode), no hacer nada
        print(f"[SECURITY] No se puede inicializar base de datos de usuarios: {e}")
        return
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Normalizar usernames a minúsculas
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            if cursor.fetchone():
                _normalize_usernames(cursor, conn)
        except Exception as e:
            print(f"[SECURITY] Error normalizando usernames: {e}")
        
        try:
            cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
            if cursor.fetchone()[0] == 0:
                admin_password_hash = get_password_hash("admin")
                cursor.execute("""
                    INSERT INTO users (username, password_hash, role, is_active)
                    VALUES (?, ?, ?, ?)
                """, ("admin", admin_password_hash, Role.ADMIN.value, 1))
                print("[SECURITY] Usuario admin creado con contraseña 'admin'")
                conn.commit()
        except sqlite3.OperationalError as e:
            # La tabla de usuarios aún no existe, se creará en init_db del namenode
            print(f"[SECURITY] Tabla de usuarios aún no existe, se creará en init_db: {e}")
        finally:
            conn.close()
    except Exception as e:
        # Si hay algún error, solo loguear pero no fallar
        print(f"[SECURITY] Error al inicializar base de datos de usuarios: {e}")


def get_password_hash(password: str) -> str:
    """Genera hash de contraseña"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica contraseña"""
    return pwd_context.verify(plain_password, hashed_password)


def get_user(username: str, node_id: str = None) -> Optional[User]:
    """Obtiene un usuario de la base de datos del namenode"""
    username = username.lower()
    db_path = get_users_db_path(node_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT username, password_hash, role, is_active, created_at
            FROM users WHERE username = ?
        """, (username,))
        
        row = cursor.fetchone()
        
        if row:
            return User(
                username=row[0],
                password_hash=row[1],
                role=Role(row[2]),
                is_active=bool(row[3]),
                created_at=row[4] if len(row) > 4 else None
            )
        return None
    finally:
        conn.close()


def create_user(user_data: UserCreate, node_id: str = None) -> User:
    """Crea un nuevo usuario en la base de datos del namenode"""
    user_data.username = user_data.username.lower()
    if get_user(user_data.username, node_id):
        raise HTTPException(
            status_code=400,
            detail="Usuario ya existe"
        )
    
    password_hash = get_password_hash(user_data.password)
    db_path = get_users_db_path(node_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, is_active)
            VALUES (?, ?, ?, ?)
        """, (user_data.username, password_hash, user_data.role.value, 1))
        
        conn.commit()
    finally:
        conn.close()
    
    return User(
        username=user_data.username,
        password_hash=password_hash,
        role=user_data.role,
        is_active=True
    )


def change_password(username: str, old_password: str, new_password: str, node_id: str = None) -> bool:
    """
    Cambia la contraseña de un usuario.
    Verifica que la contraseña antigua sea correcta antes de cambiarla.
    
    Returns:
        True si se cambió exitosamente, False si la contraseña antigua es incorrecta
    """
    username = username.lower()
    # Verificar que el usuario existe y la contraseña antigua es correcta
    user = authenticate_user(username, old_password, node_id)
    if not user:
        return False
    
    # Generar hash de la nueva contraseña
    new_password_hash = get_password_hash(new_password)
    
    # Actualizar en la base de datos
    db_path = get_users_db_path(node_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Actualizar la contraseña
        cursor.execute("""
            UPDATE users 
            SET password_hash = ?
            WHERE username = ?
        """, (new_password_hash, username))
        
        # Verificar que se actualizó al menos una fila
        if cursor.rowcount == 0:
            print(f"[SECURITY] ERROR: No se pudo actualizar la contraseña para usuario {username}")
            conn.rollback()
            return False
        
        # Confirmar la transacción
        conn.commit()
        
        # Verificar que la nueva contraseña funciona
        # Esto asegura que el cambio se aplicó correctamente
        test_user = get_user(username, node_id)
        if test_user and verify_password(new_password, test_user.password_hash):
            print(f"[SECURITY] Contraseña cambiada exitosamente para usuario {username}")
            return True
        else:
            print(f"[SECURITY] ERROR: La nueva contraseña no funciona después del cambio")
            return False
    except Exception as e:
        print(f"[SECURITY] ERROR al cambiar contraseña: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def authenticate_user(username: str, password: str, node_id: str = None) -> Optional[User]:
    """Autentica un usuario verificando usuario y contraseña en la base de datos del namenode"""
    username = username.lower()
    user = get_user(username, node_id)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    
    # Actualizar last_login
    db_path = get_users_db_path(node_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE username = ?
        """, (username,))
        conn.commit()
    finally:
        conn.close()
    
    return user


def create_access_token(data: Dict, expires_delta: Optional[timedelta] = None) -> str:
    """Crea un token JWT"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_token(token: str) -> Optional[Dict]:
    """Verifica y decodifica un token JWT"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    node_id: str = None
) -> User:
    """Obtiene el usuario actual desde el token JWT"""
    token = credentials.credentials
    payload = verify_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    username: str = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Si no se proporciona node_id, intentar obtenerlo de la variable de entorno
    if node_id is None:
        node_id = os.getenv("NODE_ID")
    
    user = get_user(username, node_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo"
        )
    
    return user


async def get_current_service(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> Dict:
    """Obtiene información del servicio desde el token de servicio"""
    token = credentials.credentials
    payload = verify_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de servicio inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    service_type = payload.get("service_type")
    if service_type != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token no es de servicio"
        )
    
    return payload


def require_role(allowed_roles: List[Role]):
    """Dependency para requerir roles específicos"""
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Se requiere uno de los roles: {[r.value for r in allowed_roles]}"
            )
        return current_user
    return role_checker


def require_permission(permission: Permission):
    """Dependency para requerir un permiso específico"""
    async def permission_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Se requiere el permiso: {permission.value}"
            )
        return current_user
    return permission_checker


# NO inicializar automáticamente al importar
# init_users_db() debe ser llamado explícitamente por el namenode en su startup
# Esto evita errores cuando se importa desde otros servicios (registry, datanode)

