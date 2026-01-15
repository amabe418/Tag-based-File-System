"""
Módulo para transferir archivos del NameNode a DataNodes por chunks
Optimiza el uso de memoria y permite reintentos granulares
"""
import hashlib
import requests
from typing import Tuple
import time

# Tamaño de chunk para transferencia a DataNodes
TRANSFER_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB


def send_file_to_datanode_chunked(
    datanode_url: str,
    file_id: str,
    file_content: bytes = None,
    service_token: str = None,
    chunk_size: int = TRANSFER_CHUNK_SIZE,
    source_datanode_url: str = None
) -> Tuple[bool, str]:
    """
    Envía un archivo a un DataNode por chunks.
    Puede leer chunks directamente desde otro DataNode (source_datanode_url) 
    o usar file_content si se proporciona.
    
    Args:
        datanode_url: URL del DataNode destino
        file_id: Hash del archivo
        file_content: Contenido del archivo (opcional, si no se proporciona se lee desde source_datanode_url)
        service_token: Token de autenticación
        chunk_size: Tamaño de cada chunk
        source_datanode_url: URL del DataNode fuente (opcional, para leer chunks directamente)
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    # Si tenemos source_datanode_url, leer chunks directamente sin ensamblar
    if source_datanode_url and not file_content:
        return send_chunks_from_datanode(
            source_datanode_url=source_datanode_url,
            target_datanode_url=datanode_url,
            file_id=file_id,
            service_token=service_token
        )
    
    # Método original: usar file_content
    if file_content is None:
        return False, "Se requiere file_content o source_datanode_url"
    
    file_size = len(file_content)
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    
    file_size_mb = file_size / (1024 * 1024)
    chunk_size_mb = chunk_size / (1024 * 1024)
    print(f"[DATANODE_TRANSFER] 📤 Iniciando transferencia chunked | destino={datanode_url} | file_id={file_id[:16]}... | archivo={file_size_mb:.2f} MB | chunks={total_chunks} | chunk_size={chunk_size_mb:.2f} MB")
    
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
        
        print(f"[DATANODE_TRANSFER] ✅ Sesión creada en DataNode | session_id={session_id[:8]} | destino={datanode_url}")
        
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
                    
                    chunk_size_mb = len(chunk_data) / (1024 * 1024)
                    # Mostrar progreso cada 5 chunks o en el último
                    if (chunk_index + 1) % 5 == 0 or chunk_index == total_chunks - 1:
                        progress = ((chunk_index + 1) / total_chunks) * 100
                        elapsed = time.time() - start_time if 'start_time' in locals() else 0
                        speed = (chunk_index + 1) * chunk_size / elapsed / (1024 * 1024) if elapsed > 0 else 0
                        print(f"[DATANODE_TRANSFER] 📊 Progreso: {chunk_index + 1}/{total_chunks} ({progress:.1f}%) | chunk={chunk_size_mb:.2f} MB | velocidad={speed:.2f} MB/s | destino={datanode_url}")
                    if chunk_index == 0:
                        start_time = time.time()
                    
                    break
                except Exception as e:
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        print(f"[DATANODE_TRANSFER] ⚠️  Error en chunk {chunk_index + 1}/{total_chunks} | reintento {retry + 1}/{max_retries} en {wait_time}s | error={str(e)[:100]} | destino={datanode_url}")
                        time.sleep(wait_time)
                    else:
                        return False, f"Error en chunk {chunk_index}: {e}"
            
            if not success:
                return False, f"No se pudo enviar chunk {chunk_index}"
        
        # 3. Finalizar almacenamiento
        print(f"[DATANODE_TRANSFER] 🔄 Finalizando almacenamiento | session_id={session_id[:8]} | destino={datanode_url}")
        response = requests.post(
            f"{datanode_url}/store/session/{session_id}/finalize",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=120
        )
        response.raise_for_status()
        
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        avg_speed = file_size / total_time / (1024 * 1024) if total_time > 0 else 0
        print(f"[DATANODE_TRANSFER] ✅ Transferencia completada | file_id={file_id[:16]}... | {total_chunks} chunks | tiempo={total_time:.2f}s | velocidad_promedio={avg_speed:.2f} MB/s | destino={datanode_url}")
        return True, "Archivo almacenado correctamente"
        
    except requests.RequestException as e:
        return False, f"Error de red: {e}"
    except Exception as e:
        return False, f"Error inesperado: {e}"


def send_chunks_from_datanode(
    source_datanode_url: str,
    target_datanode_url: str,
    file_id: str,
    service_token: str
) -> Tuple[bool, str]:
    """
    Lee chunks directamente desde un DataNode y los envía a otro DataNode
    sin ensamblar el archivo completo. Esto es más eficiente para re-replicación.
    
    Args:
        source_datanode_url: URL del DataNode fuente (tiene los chunks)
        target_datanode_url: URL del DataNode destino
        file_id: Hash del archivo
        service_token: Token de autenticación
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    print(f"[DATANODE_TRANSFER] 🔄 Transferencia directa de chunks | origen={source_datanode_url} | destino={target_datanode_url} | file_id={file_id[:16]}...")
    
    try:
        # 1. Obtener información de chunks desde el DataNode fuente
        response = requests.get(
            f"{source_datanode_url}/chunks/{file_id}/info",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=30
        )
        response.raise_for_status()
        chunks_info = response.json()
        
        chunk_count = chunks_info["chunk_count"]
        total_size = chunks_info["total_size"]
        chunk_sizes = chunks_info.get("chunk_sizes", [])
        
        total_size_mb = total_size / (1024 * 1024)
        print(f"[DATANODE_TRANSFER] 📦 Archivo tiene {chunk_count} chunks | tamaño_total={total_size_mb:.2f} MB | origen={source_datanode_url}")
        
        # 2. Iniciar sesión en el DataNode destino
        # Usar el tamaño del primer chunk como chunk_size (o un valor por defecto)
        chunk_size = chunk_sizes[0] if chunk_sizes else TRANSFER_CHUNK_SIZE
        
        response = requests.post(
            f"{target_datanode_url}/store/init",
            data={
                "file_id": file_id,
                "total_chunks": chunk_count,
                "chunk_size": chunk_size,
                "file_size": total_size
            },
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        session_id = data["session_id"]
        
        print(f"[DATANODE_TRANSFER] ✅ Sesión creada en destino | session_id={session_id[:8]} | destino={target_datanode_url}")
        
        # 3. Leer y enviar cada chunk
        for chunk_index in range(chunk_count):
            # Leer chunk desde DataNode fuente
            response = requests.get(
                f"{source_datanode_url}/chunks/{file_id}/{chunk_index}",
                headers={"Authorization": f"Bearer {service_token}"},
                timeout=120
            )
            response.raise_for_status()
            chunk_data = response.content
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            
            # Enviar chunk al DataNode destino con reintentos
            max_retries = 3
            success = False
            
            for retry in range(max_retries):
                try:
                    response = requests.post(
                        f"{target_datanode_url}/store/session/{session_id}/chunk/{chunk_index}",
                        files={"chunk": (f"chunk_{chunk_index}", chunk_data)},
                        data={"chunk_hash": chunk_hash},
                        headers={"Authorization": f"Bearer {service_token}"},
                        timeout=120
                    )
                    response.raise_for_status()
                    success = True
                    
                    chunk_size_mb = len(chunk_data) / (1024 * 1024)
                    # Mostrar progreso cada 5 chunks o en el último
                    if (chunk_index + 1) % 5 == 0 or chunk_index == chunk_count - 1:
                        progress = ((chunk_index + 1) / chunk_count) * 100
                        elapsed = time.time() - transfer_start_time if 'transfer_start_time' in locals() else 0
                        speed = (chunk_index + 1) * (total_size / chunk_count) / elapsed / (1024 * 1024) if elapsed > 0 else 0
                        print(f"[DATANODE_TRANSFER] 📊 Progreso: {chunk_index + 1}/{chunk_count} ({progress:.1f}%) | chunk={chunk_size_mb:.2f} MB | velocidad={speed:.2f} MB/s | origen→destino")
                    if chunk_index == 0:
                        transfer_start_time = time.time()
                    
                    break
                except Exception as e:
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        print(f"[DATANODE_TRANSFER] Error en chunk {chunk_index}, reintentando en {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        return False, f"Error enviando chunk {chunk_index}: {e}"
            
            if not success:
                return False, f"No se pudo enviar chunk {chunk_index}"
        
        # 4. Finalizar almacenamiento
        print(f"[DATANODE_TRANSFER] 🔄 Finalizando almacenamiento en destino | session_id={session_id[:8]} | destino={target_datanode_url}")
        response = requests.post(
            f"{target_datanode_url}/store/session/{session_id}/finalize",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=120
        )
        response.raise_for_status()
        
        total_time = time.time() - transfer_start_time if 'transfer_start_time' in locals() else 0
        avg_speed = total_size / total_time / (1024 * 1024) if total_time > 0 else 0
        print(f"[DATANODE_TRANSFER] ✅ Transferencia directa completada | file_id={file_id[:16]}... | {chunk_count} chunks | tamaño={total_size_mb:.2f} MB | tiempo={total_time:.2f}s | velocidad_promedio={avg_speed:.2f} MB/s | origen→destino")
        return True, "Chunks transferidos correctamente"
        
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

