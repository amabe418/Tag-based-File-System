"""
Sistema de almacenamiento local del DataNode
Gestiona el almacenamiento físico de archivos en disco
"""
import os
import shutil
from typing import Optional, Dict
from pathlib import Path

# Ruta base de almacenamiento (configurable por variable de entorno)
STORAGE_BASE = os.getenv("STORAGE_PATH", "/app/storage")


def get_storage_path() -> str:
    """Obtiene la ruta base de almacenamiento"""
    return STORAGE_BASE


def ensure_storage_dir():
    """Asegura que el directorio de almacenamiento existe"""
    os.makedirs(STORAGE_BASE, exist_ok=True)


def get_file_path(file_id: str) -> str:
    """
    Obtiene la ruta completa de un archivo basado en su ID.
    Organiza archivos en subdirectorios por los primeros 2 caracteres del hash.
    Ejemplo: file_id="abc123" -> storage/ab/abc123
    """
    if not file_id or not file_id.strip():
        raise ValueError("file_id no puede estar vacío")
    
    ensure_storage_dir()
    # Usar los primeros 2 caracteres para crear subdirectorio
    subdir = file_id[:2] if len(file_id) >= 2 else "00"
    subdir_path = os.path.join(STORAGE_BASE, subdir)
    os.makedirs(subdir_path, exist_ok=True)
    return os.path.join(subdir_path, file_id)


def store_file(file_id: str, file_content: bytes) -> bool:
    """
    Guarda un archivo en el almacenamiento local.
    
    Args:
        file_id: Identificador único del archivo (hash)
        file_content: Contenido del archivo en bytes
    
    Returns:
        True si se guardó correctamente, False en caso de error
    """
    try:
        ensure_storage_dir()
        file_path = get_file_path(file_id)
        
        # Escribir archivo
        with open(file_path, 'wb') as f:
            f.write(file_content)
        
        print(f"[STORAGE] Archivo guardado: {file_id} -> {file_path}")
        return True
    except Exception as e:
        print(f"[STORAGE] Error al guardar archivo {file_id}: {e}")
        return False


def retrieve_file(file_id: str) -> Optional[bytes]:
    """
    Lee un archivo del almacenamiento local.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        Contenido del archivo en bytes, o None si no existe
    """
    try:
        file_path = get_file_path(file_id)
        
        if not os.path.exists(file_path):
            print(f"[STORAGE] Archivo no encontrado: {file_id}")
            return None
        
        with open(file_path, 'rb') as f:
            content = f.read()
        
        print(f"[STORAGE] Archivo leído: {file_id} ({len(content)} bytes)")
        return content
    except Exception as e:
        print(f"[STORAGE] Error al leer archivo {file_id}: {e}")
        return None


def delete_file(file_id: str) -> bool:
    """
    Elimina un archivo del almacenamiento local.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        True si se eliminó correctamente, False en caso de error
    """
    try:
        file_path = get_file_path(file_id)
        
        if not os.path.exists(file_path):
            print(f"[STORAGE] Archivo no existe para eliminar: {file_id}")
            return False
        
        os.remove(file_path)
        
        # Intentar eliminar subdirectorio si está vacío (opcional, para limpieza)
        subdir = os.path.dirname(file_path)
        try:
            if os.path.exists(subdir) and not os.listdir(subdir):
                os.rmdir(subdir)
        except:
            pass  # Ignorar si no se puede eliminar (puede tener otros archivos)
        
        print(f"[STORAGE] Archivo eliminado: {file_id}")
        return True
    except Exception as e:
        print(f"[STORAGE] Error al eliminar archivo {file_id}: {e}")
        return False


def file_exists(file_id: str) -> bool:
    """
    Verifica si un archivo existe en el almacenamiento.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        True si existe, False en caso contrario
    """
    file_path = get_file_path(file_id)
    return os.path.exists(file_path)


def get_storage_info() -> Dict:
    """
    Obtiene información sobre el almacenamiento (espacio total, usado, libre).
    
    Returns:
        Diccionario con información de almacenamiento:
        {
            "total_space": bytes totales,
            "used_space": bytes usados,
            "free_space": bytes libres,
            "storage_path": ruta de almacenamiento
        }
    """
    try:
        ensure_storage_dir()
        
        # Obtener información del sistema de archivos
        stat = shutil.disk_usage(STORAGE_BASE)
        
        # Calcular espacio usado por nuestros archivos (recursivo)
        used_space = 0
        for root, dirs, files in os.walk(STORAGE_BASE):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    used_space += os.path.getsize(file_path)
                except:
                    pass
        
        return {
            "total_space": stat.total,
            "used_space": used_space,
            "free_space": stat.free,
            "storage_path": STORAGE_BASE
        }
    except Exception as e:
        print(f"[STORAGE] Error al obtener información de almacenamiento: {e}")
        # Retornar valores por defecto en caso de error
        return {
            "total_space": 0,
            "used_space": 0,
            "free_space": 0,
            "storage_path": STORAGE_BASE
        }

