"""
Modelos de datos para seguridad: usuarios, roles y permisos
"""
from pydantic import BaseModel
from typing import List, Optional
from enum import Enum


class Permission(str, Enum):
    """Permisos disponibles en el sistema"""
    READ_FILES = "read:files"
    WRITE_FILES = "write:files"
    DELETE_FILES = "delete:files"
    MANAGE_TAGS = "manage:tags"
    MANAGE_DATANODES = "manage:datanodes"
    ADMIN = "admin"


class Role(str, Enum):
    """Roles disponibles en el sistema"""
    USER = "user"  # Usuario básico: lectura y escritura de archivos
    ADMIN = "admin"  # Administrador: acceso completo
    SERVICE = "service"  # Servicio interno: comunicación inter-servicio


# Mapeo de roles a permisos
ROLE_PERMISSIONS = {
    Role.USER: [
        Permission.READ_FILES,
        Permission.WRITE_FILES,
        Permission.MANAGE_TAGS
    ],
    Role.ADMIN: [
        Permission.READ_FILES,
        Permission.WRITE_FILES,
        Permission.DELETE_FILES,
        Permission.MANAGE_TAGS,
        Permission.MANAGE_DATANODES,
        Permission.ADMIN
    ],
    Role.SERVICE: [
        Permission.READ_FILES,
        Permission.WRITE_FILES,
        Permission.DELETE_FILES,
        Permission.MANAGE_DATANODES
    ]
}


class User(BaseModel):
    """Modelo de usuario"""
    username: str
    password_hash: str
    role: Role
    is_active: bool = True
    created_at: Optional[str] = None

    def has_permission(self, permission: Permission) -> bool:
        """Verifica si el usuario tiene un permiso específico"""
        if not self.is_active:
            return False
        user_permissions = ROLE_PERMISSIONS.get(self.role, [])
        return permission in user_permissions

    def has_role(self, role: Role) -> bool:
        """Verifica si el usuario tiene un rol específico"""
        return self.role == role


class UserCreate(BaseModel):
    """Modelo para crear un usuario"""
    username: str
    password: str
    role: Role = Role.USER


class UserLogin(BaseModel):
    """Modelo para login de usuario"""
    username: str
    password: str


class UserSignup(BaseModel):
    """Modelo para registro de usuario"""
    username: str
    password: str


class PasswordChange(BaseModel):
    """Modelo para cambiar contraseña"""
    old_password: str
    new_password: str


class TokenResponse(BaseModel):
    """Respuesta con token de acceso"""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict

