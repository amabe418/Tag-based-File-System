"""
Módulo de seguridad centralizado para TBFS
Proporciona autenticación JWT, autorización RBAC, tokens de servicio y rate limiting
"""
from security.auth import (
    create_access_token,
    verify_token,
    get_current_user,
    get_current_service,
    require_role,
    require_permission
)
from security.service_auth import (
    generate_service_token,
    verify_service_token,
    ServiceTokenPayload
)
from security.rate_limit import rate_limit_middleware, RateLimiter
from security.models import User, Role, Permission

__all__ = [
    "create_access_token",
    "verify_token",
    "get_current_user",
    "get_current_service",
    "require_role",
    "require_permission",
    "generate_service_token",
    "verify_service_token",
    "ServiceTokenPayload",
    "rate_limit_middleware",
    "RateLimiter",
    "User",
    "Role",
    "Permission"
]

