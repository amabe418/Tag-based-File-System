"""
Autenticación entre servicios usando tokens de servicio
"""
from datetime import datetime, timedelta
from typing import Dict, Optional
from jose import jwt
import os
import hashlib

from security.models import Role

# Clave secreta para tokens de servicio (diferente de JWT de usuarios)
SERVICE_SECRET_KEY = os.getenv("SERVICE_SECRET_KEY", "service-secret-key-change-in-production")
SERVICE_TOKEN_EXPIRE_DAYS = int(os.getenv("SERVICE_TOKEN_EXPIRE_DAYS", "365"))

# Servicios autorizados y sus tokens pre-compartidos
# En producción, estos deberían estar en un sistema de gestión de secretos
AUTHORIZED_SERVICES = {
    "namenode": os.getenv("NAMENODE_SERVICE_TOKEN", "namenode-service-token"),
    "datanode": os.getenv("DATANODE_SERVICE_TOKEN", "datanode-service-token"),
    "registry": os.getenv("REGISTRY_SERVICE_TOKEN", "registry-service-token"),
    "client": os.getenv("CLIENT_SERVICE_TOKEN", "client-service-token")
}


class ServiceTokenPayload:
    """Payload de token de servicio"""
    def __init__(self, service_id: str, service_type: str, permissions: list):
        self.service_id = service_id
        self.service_type = service_type
        self.permissions = permissions
        self.iat = datetime.utcnow()
        self.exp = datetime.utcnow() + timedelta(days=SERVICE_TOKEN_EXPIRE_DAYS)


def generate_service_token(service_id: str, service_type: str = "service") -> str:
    """
    Genera un token de servicio para comunicación inter-servicio
    
    Args:
        service_id: Identificador del servicio (ej: "namenode-1", "datanode-3", "tbfs-registry-1")
        service_type: Tipo de servicio (default: "service")
    
    Returns:
        Token JWT firmado
    """
    # Verificar que el servicio está autorizado
    # Intentar encontrar el tipo de servicio en el ID
    service_base = None
    service_id_lower = service_id.lower()
    
    # Buscar el tipo de servicio en el ID (puede estar en cualquier parte)
    for service_type_key in AUTHORIZED_SERVICES.keys():
        if service_type_key in service_id_lower:
            service_base = service_type_key
            break
    
    # Si no se encuentra, intentar el primer elemento del split (compatibilidad)
    if not service_base:
        service_base = service_id.split("-")[0] if "-" in service_id else service_id
        if service_base not in AUTHORIZED_SERVICES:
            raise ValueError(f"Servicio no autorizado: {service_id}")
    
    payload = {
        "sub": service_id,
        "service_type": service_type,
        "service_id": service_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=SERVICE_TOKEN_EXPIRE_DAYS)
    }
    
    token = jwt.encode(payload, SERVICE_SECRET_KEY, algorithm="HS256")
    return token


def verify_service_token(token: str) -> Optional[Dict]:
    """
    Verifica un token de servicio
    
    Args:
        token: Token JWT a verificar
    
    Returns:
        Payload del token si es válido, None en caso contrario
    """
    try:
        payload = jwt.decode(token, SERVICE_SECRET_KEY, algorithms=["HS256"])
        
        # Verificar que es un token de servicio
        # Nota: service_type puede ser "service" o cualquier otro valor válido
        # Solo rechazamos si no tiene service_type o service_id
        if not payload.get("service_id") and not payload.get("sub"):
            return None
        
        # Verificar que el servicio está autorizado
        service_id = payload.get("service_id") or payload.get("sub")
        if service_id:
            # Buscar el tipo de servicio en el ID (puede estar en cualquier parte)
            service_base = None
            service_id_lower = service_id.lower()
            
            for service_type_key in AUTHORIZED_SERVICES.keys():
                if service_type_key in service_id_lower:
                    service_base = service_type_key
                    break
            
            # Si no se encuentra, intentar el primer elemento del split (compatibilidad)
            if not service_base:
                service_base = service_id.split("-")[0] if "-" in service_id else service_id
                if service_base not in AUTHORIZED_SERVICES:
                    return None
        
        return payload
    except jwt.JWTError:
        return None


def get_service_token_for_service(service_id: str) -> Optional[str]:
    """
    Obtiene el token pre-compartido para un servicio específico
    En producción, esto debería consultar un sistema de gestión de secretos
    """
    # Buscar el tipo de servicio en el ID (puede estar en cualquier parte)
    service_id_lower = service_id.lower()
    for service_type_key in AUTHORIZED_SERVICES.keys():
        if service_type_key in service_id_lower:
            return AUTHORIZED_SERVICES.get(service_type_key)
    
    # Si no se encuentra, intentar el primer elemento del split (compatibilidad)
    service_base = service_id.split("-")[0] if "-" in service_id else service_id
    return AUTHORIZED_SERVICES.get(service_base)


def validate_service_request(service_id: str, token: str) -> bool:
    """
    Valida una petición de servicio usando token pre-compartido o JWT
    
    Args:
        service_id: ID del servicio que hace la petición
        token: Token de autenticación
    
    Returns:
        True si la petición es válida
    """
    # Intentar verificar como token JWT
    payload = verify_service_token(token)
    if payload:
        return payload.get("service_id") == service_id or payload.get("sub") == service_id
    
    # Intentar verificar como token pre-compartido
    expected_token = get_service_token_for_service(service_id)
    if expected_token and token == expected_token:
        return True
    
    return False

