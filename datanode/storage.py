"""
Sistema de almacenamiento local del DataNode
Gestiona el almacenamiento físico de archivos en disco
"""
import os
import shutil
import hashlib
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


def get_chunks_dir(file_id: str) -> str:
    """
    Obtiene el directorio donde se almacenan los chunks de un archivo.
    Ejemplo: file_id="abc123" -> storage/ab/abc123/
    """
    if not file_id or not file_id.strip():
        raise ValueError("file_id no puede estar vacío")
    
    ensure_storage_dir()
    subdir = file_id[:2] if len(file_id) >= 2 else "00"
    subdir_path = os.path.join(STORAGE_BASE, subdir)
    chunks_dir = os.path.join(subdir_path, file_id)
    os.makedirs(chunks_dir, exist_ok=True)
    return chunks_dir


def get_chunk_path(file_id: str, chunk_index: int) -> str:
    """
    Obtiene la ruta de un chunk específico.
    Ejemplo: file_id="abc123", chunk_index=5 -> storage/ab/abc123/chunk_000005
    """
    chunks_dir = get_chunks_dir(file_id)
    return os.path.join(chunks_dir, f"chunk_{chunk_index:06d}")


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
    Si el archivo está almacenado como chunks, los ensambla.
    Si está como archivo completo, lo lee directamente.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        Contenido del archivo en bytes, o None si no existe
    """
    try:
        # Primero verificar si existe como archivo completo (compatibilidad hacia atrás)
        file_path = get_file_path(file_id)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            with open(file_path, 'rb') as f:
                content = f.read()
            print(f"[STORAGE] Archivo leído (completo): {file_id} ({len(content)} bytes)")
            return content
        
        # Si no existe como archivo completo, buscar chunks
        chunks_dir = get_chunks_dir(file_id)
        if not os.path.exists(chunks_dir):
            print(f"[STORAGE] Archivo no encontrado (ni completo ni chunks): {file_id}")
            return None
        
        # Leer chunks y ensamblar
        chunks = []
        chunk_index = 0
        while True:
            chunk_path = get_chunk_path(file_id, chunk_index)
            if not os.path.exists(chunk_path):
                break
            
            with open(chunk_path, 'rb') as f:
                chunk_data = f.read()
            chunks.append(chunk_data)
            chunk_index += 1
        
        if not chunks:
            print(f"[STORAGE] No se encontraron chunks para: {file_id}")
            return None
        
        # Ensamblar archivo desde chunks
        content = b''.join(chunks)
        print(f"[STORAGE] Archivo leído (desde {len(chunks)} chunks): {file_id} ({len(content)} bytes)")
        return content
        
    except Exception as e:
        print(f"[STORAGE] Error al leer archivo {file_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def delete_file(file_id: str) -> bool:
    """
    Elimina un archivo del almacenamiento local.
    Elimina tanto archivos completos como chunks.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        True si se eliminó correctamente, False en caso de error
    """
    try:
        deleted_anything = False
        
        # Intentar eliminar archivo completo (compatibilidad hacia atrás)
        file_path = get_file_path(file_id)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            print(f"[STORAGE] Eliminando archivo completo: {file_id} (ruta: {file_path})")
            os.remove(file_path)
            deleted_anything = True
            
            # Verificar que realmente se eliminó
            if os.path.exists(file_path):
                print(f"[STORAGE] ❌ ERROR: Archivo {file_id} aún existe después de os.remove()")
                return False
        
        # Intentar eliminar directorio de chunks
        chunks_dir = get_chunks_dir(file_id)
        if os.path.exists(chunks_dir) and os.path.isdir(chunks_dir):
            print(f"[STORAGE] Eliminando chunks: {file_id} (directorio: {chunks_dir})")
            shutil.rmtree(chunks_dir)
            deleted_anything = True
        
        if not deleted_anything:
            print(f"[STORAGE] ⚠️  Archivo no existe para eliminar: {file_id}")
            return False
        
        # Intentar eliminar subdirectorio si está vacío (opcional, para limpieza)
        subdir = os.path.dirname(file_path)
        try:
            if os.path.exists(subdir):
                contents = os.listdir(subdir)
                if not contents:
                    os.rmdir(subdir)
                    print(f"[STORAGE] Subdirectorio vacío eliminado: {subdir}")
                else:
                    print(f"[STORAGE] Subdirectorio {subdir} no está vacío ({len(contents)} elementos), no se elimina")
        except OSError as e:
            # Error al eliminar subdirectorio (puede tener otros archivos o problemas de permisos)
            print(f"[STORAGE] ⚠️  No se pudo eliminar subdirectorio {subdir}: {e}")
        except Exception as e:
            # Otro tipo de error inesperado
            print(f"[STORAGE] ⚠️  Error inesperado al intentar eliminar subdirectorio {subdir}: {e}")
        
        print(f"[STORAGE] ✅ Archivo eliminado exitosamente: {file_id}")
        return True
    except FileNotFoundError:
        print(f"[STORAGE] ⚠️  Archivo no encontrado (ya fue eliminado): {file_id}")
        return False
    except PermissionError as e:
        print(f"[STORAGE] ❌ Error de permisos al eliminar {file_id}: {e}")
        return False
    except Exception as e:
        print(f"[STORAGE] ❌ Error al eliminar archivo {file_id}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


def file_exists(file_id: str) -> bool:
    """
    Verifica si un archivo existe en el almacenamiento.
    Verifica tanto archivos completos como chunks.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        True si existe, False en caso contrario
    """
    # Verificar archivo completo
    file_path = get_file_path(file_id)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return True
    
    # Verificar chunks
    chunks_dir = get_chunks_dir(file_id)
    if os.path.exists(chunks_dir) and os.path.isdir(chunks_dir):
        # Verificar que hay al menos un chunk
        chunk_path = get_chunk_path(file_id, 0)
        if os.path.exists(chunk_path):
            return True
    
    return False


def store_chunks(file_id: str, chunks_info: Dict[int, tuple]) -> bool:
    """
    Guarda chunks directamente en el almacenamiento final.
    
    Args:
        file_id: Identificador único del archivo (hash)
        chunks_info: Diccionario con {chunk_index: (chunk_data, chunk_hash)}
    
    Returns:
        True si se guardaron correctamente, False en caso de error
    """
    try:
        chunks_dir = get_chunks_dir(file_id)
        
        # Mover cada chunk a su ubicación final
        for chunk_index, (chunk_data, chunk_hash) in sorted(chunks_info.items()):
            chunk_path = get_chunk_path(file_id, chunk_index)
            
            # Verificar hash del chunk
            calculated_hash = hashlib.sha256(chunk_data).hexdigest()
            if calculated_hash != chunk_hash:
                print(f"[STORAGE] ❌ Hash inválido para chunk {chunk_index} de {file_id}")
                return False
            
            # Guardar chunk
            with open(chunk_path, 'wb') as f:
                f.write(chunk_data)
        
        print(f"[STORAGE] ✅ {len(chunks_info)} chunks guardados para {file_id}")
        return True
    except Exception as e:
        print(f"[STORAGE] ❌ Error guardando chunks para {file_id}: {e}")
        import traceback
        traceback.print_exc()
        return False


def get_chunk_count(file_id: str) -> int:
    """
    Obtiene el número de chunks de un archivo.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        Número de chunks, o 0 si no existe
    """
    chunks_dir = get_chunks_dir(file_id)
    if not os.path.exists(chunks_dir):
        return 0
    
    chunk_count = 0
    chunk_index = 0
    while True:
        chunk_path = get_chunk_path(file_id, chunk_index)
        if not os.path.exists(chunk_path):
            break
        chunk_count += 1
        chunk_index += 1
    
    return chunk_count


def get_chunk(file_id: str, chunk_index: int) -> Optional[bytes]:
    """
    Lee un chunk específico del almacenamiento.
    
    Args:
        file_id: Identificador único del archivo (hash)
        chunk_index: Índice del chunk
    
    Returns:
        Contenido del chunk en bytes, o None si no existe
    """
    try:
        chunk_path = get_chunk_path(file_id, chunk_index)
        if not os.path.exists(chunk_path):
            return None
        
        with open(chunk_path, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"[STORAGE] Error leyendo chunk {chunk_index} de {file_id}: {e}")
        return None


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

