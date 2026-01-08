"""
Módulo para transferir archivos del NameNode a DataNodes por chunks
Optimiza el uso de memoria y permite reintentos granulares
"""
import hashlib
import requests
from typing import Tuple
import time

# Tamaño de chunk para transferencia a DataNodes
TRANSFER_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB


def send_file_to_datanode_chunked(
    datanode_url: str,
    file_id: str,
    file_content: bytes,
    service_token: str,
    chunk_size: int = TRANSFER_CHUNK_SIZE
) -> Tuple[bool, str]:
    """
    Envía un archivo a un DataNode por chunks
    
    Args:
        datanode_url: URL del DataNode
        file_id: Hash del archivo
        file_content: Contenido del archivo
        service_token: Token de autenticación
        chunk_size: Tamaño de cada chunk
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    file_size = len(file_content)
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    
    print(f"[DATANODE_TRANSFER] Enviando archivo a {datanode_url} por chunks ({total_chunks} chunks)")
    
    try:
        # 1. Iniciar sesión de almacenamiento en el DataNode
        response = requests.post(
            f"{datanode_url}/store/init",
            data={
                "file_id": file_id,
                "total_chunks": total_chunks,
                "chunk_size": chunk_size,
                "file_size": file_size
            },
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        session_id = data["session_id"]
        
        print(f"[DATANODE_TRANSFER] Sesión creada en DataNode: {session_id}")
        
        # 2. Enviar chunks
        for chunk_index in range(total_chunks):
            start = chunk_index * chunk_size
            end = min(start + chunk_size, file_size)
            chunk_data = file_content[start:end]
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            
            # Enviar chunk con reintentos
            max_retries = 3
            success = False
            
            for retry in range(max_retries):
                try:
                    response = requests.post(
                        f"{datanode_url}/store/session/{session_id}/chunk/{chunk_index}",
                        files={"chunk": (f"chunk_{chunk_index}", chunk_data)},
                        data={"chunk_hash": chunk_hash},
                        headers={"Authorization": f"Bearer {service_token}"},
                        timeout=120
                    )
                    response.raise_for_status()
                    success = True
                    
                    if (chunk_index + 1) % 10 == 0 or chunk_index == total_chunks - 1:
                        progress = ((chunk_index + 1) / total_chunks) * 100
                        print(f"[DATANODE_TRANSFER] Progreso: {chunk_index + 1}/{total_chunks} ({progress:.1f}%)")
                    
                    break
                except Exception as e:
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        print(f"[DATANODE_TRANSFER] Error en chunk {chunk_index}, reintentando en {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        return False, f"Error en chunk {chunk_index}: {e}"
            
            if not success:
                return False, f"No se pudo enviar chunk {chunk_index}"
        
        # 3. Finalizar almacenamiento
        print(f"[DATANODE_TRANSFER] Finalizando almacenamiento en DataNode...")
        response = requests.post(
            f"{datanode_url}/store/session/{session_id}/finalize",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=120
        )
        response.raise_for_status()
        
        print(f"[DATANODE_TRANSFER] ✅ Archivo enviado exitosamente a DataNode")
        return True, "Archivo almacenado correctamente"
        
    except requests.RequestException as e:
        return False, f"Error de red: {e}"
    except Exception as e:
        return False, f"Error inesperado: {e}"


def send_file_to_datanode_legacy(
    datanode_url: str,
    file_id: str,
    filename: str,
    file_content: bytes,
    service_token: str
) -> Tuple[bool, str]:
    """
    Envía un archivo a un DataNode usando el método tradicional
    
    Args:
        datanode_url: URL del DataNode
        file_id: Hash del archivo
        filename: Nombre del archivo
        file_content: Contenido del archivo
        service_token: Token de autenticación
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    try:
        response = requests.post(
            f"{datanode_url}/store",
            files={"file": (filename, file_content)},
            data={"file_id": file_id},
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=120
        )
        response.raise_for_status()
        
        return True, "Archivo almacenado correctamente"
    except requests.RequestException as e:
        return False, f"Error: {e}"

