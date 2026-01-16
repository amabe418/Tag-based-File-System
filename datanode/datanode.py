"""
DataNode - Servicio de almacenamiento distribuido
Almacena archivos físicos y se registra con el MetaNameNode
"""
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import os
from contextlib import asynccontextmanager
import sys
from pathlib import Path

# Agregar directorio raíz al path para importar security
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.service_auth import verify_service_token, validate_service_request, verify_client_token
from security.rate_limit import RateLimitMiddleware

from datanode.storage import (
    store_file, retrieve_file, delete_file, file_exists, get_storage_info
)
from datanode.namenode_client import registry_client
from datanode.chunked_storage import chunked_storage_manager
import uuid

# Configuración
DATANODE_ID = os.getenv("DATANODE_ID", "datanode-1")
DATANODE_PORT = int(os.getenv("DATANODE_PORT", "8001"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de la aplicación"""
    # Startup
    print(f"[DATANODE] Iniciando DataNode: {DATANODE_ID}")
    print(f"[DATANODE] Puerto: {DATANODE_PORT}")
    
    # Iniciar manager de chunked storage
    chunked_storage_manager.start_cleanup_thread(max_age_hours=24)
    print(f"[DATANODE] Chunked storage manager iniciado")
    
    # Registrar con MetaNameNode
    if registry_client.register():
        print(f"[DATANODE] Registrado exitosamente con MetaNameNode")
    else:
        print(f"[DATANODE] ⚠️  No se pudo registrar inicialmente, se reintentará en heartbeats")
    
    # Iniciar heartbeats
    registry_client.start_heartbeat()
    
    yield
    
    # Shutdown
    print(f"[DATANODE] Deteniendo DataNode...")
    chunked_storage_manager.stop_cleanup_thread()
    registry_client.stop_heartbeat()


app = FastAPI(title=f"TBFS DataNode ({DATANODE_ID})", lifespan=lifespan)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permitir todos los orígenes para desarrollo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurar rate limiting
app.add_middleware(RateLimitMiddleware, max_requests=100, time_window=60)


@app.get("/")
def root():
    """Endpoint de estado del DataNode"""
    storage_info = get_storage_info()
    
    return {
        "message": "DataNode funcionando",
        "node_id": DATANODE_ID,
        "port": DATANODE_PORT,
        "registered": registry_client.registered,
        "namenode_url": registry_client.namenode_url,
        "storage": storage_info
    }


@app.get("/health")
def health():
    """Endpoint de health check"""
    return {
        "status": "healthy",
        "node_id": DATANODE_ID,
        "registered": registry_client.registered
    }


@app.post("/store")
async def store(
    file_id: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Almacena un archivo en el DataNode.
    Requiere token de servicio del MetaNameNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        file: Archivo a almacenar
        authorization: Token de servicio
    
    Returns:
        Confirmación de almacenamiento
    """
    # Verificar autenticación de servicio (solo MetaNameNode puede almacenar)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene del MetaNameNode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        print(f"[DATANODE] ❌ Acceso denegado a /store: service_id={service_id} no es un namenode")
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede almacenar archivos")
    print(f"[DATANODE] POST /store recibido: file_id={file_id}, filename={file.filename}, service_id={service_id}")
    
    # Validar que file_id no esté vacío
    if not file_id or not file_id.strip():
        raise HTTPException(
            status_code=400,
            detail="file_id no puede estar vacío"
        )
    
    try:
        # Leer contenido del archivo
        file_content = await file.read()
        file_size = len(file_content)
        
        print(f"[DATANODE] Archivo recibido: {file_size} bytes")
        
        # Verificar espacio disponible
        storage_info = get_storage_info()
        if storage_info["free_space"] < file_size:
            raise HTTPException(
                status_code=507,
                detail=f"Espacio insuficiente. Disponible: {storage_info['free_space']} bytes, Requerido: {file_size} bytes"
            )
        
        # Almacenar archivo
        if store_file(file_id, file_content):
            print(f"[DATANODE] Archivo almacenado exitosamente: {file_id}")
            return {
                "success": True,
                "message": f"Archivo '{file_id}' almacenado correctamente",
                "file_id": file_id,
                "size": file_size
            }
        else:
            raise HTTPException(status_code=500, detail="Error al almacenar archivo")
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"[DATANODE] Error en /store: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")


@app.get("/retrieve/{file_id}")
def retrieve(
    file_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    range_header: Optional[str] = Header(None, alias="Range")
):
    """
    Recupera un archivo del DataNode con soporte para HTTP Range requests.
    Requiere token de servicio del MetaNameNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        authorization: Token de servicio
        range_header: Header Range para descarga parcial (opcional)
    
    Returns:
        Contenido del archivo (completo o parcial)
    """
    # Verificar autenticación de servicio (solo MetaNameNode puede leer)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene del MetaNameNode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        print(f"[DATANODE] ❌ Acceso denegado a /retrieve: service_id={service_id} no es un namenode")
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede leer archivos")
    print(f"[DATANODE] GET /retrieve/{file_id} (service_id={service_id}, range={range_header})")
    
    file_content = retrieve_file(file_id)
    
    if file_content is None:
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    file_size = len(file_content)
    
    # Soporte para HTTP Range requests
    if range_header:
        try:
            # Parsear header Range (formato: "bytes=start-end")
            range_value = range_header.replace("bytes=", "")
            
            # Soportar múltiples rangos (tomar solo el primero por simplicidad)
            if "," in range_value:
                range_value = range_value.split(",")[0]
            
            parts = range_value.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
            
            # Validar rangos
            if start < 0 or end >= file_size or start > end:
                raise HTTPException(
                    status_code=416,
                    detail=f"Range inválido: {range_header}",
                    headers={"Content-Range": f"bytes */{file_size}"}
                )
            
            # Extraer contenido del rango
            content = file_content[start:end + 1]
            content_length = len(content)
            
            print(f"[DATANODE] Enviando rango: bytes {start}-{end}/{file_size} ({content_length} bytes)")
            
            # Retornar contenido parcial con código 206
            from fastapi.responses import Response
            return Response(
                content=content,
                status_code=206,
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{file_id}"',
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(content_length),
                    "Accept-Ranges": "bytes"
                }
            )
            
        except ValueError as e:
            print(f"[DATANODE] Error parseando Range header: {e}")
            raise HTTPException(status_code=400, detail=f"Range header inválido: {range_header}")
    
    # Retornar archivo completo si no hay Range header
    from fastapi.responses import Response
    return Response(
        content=file_content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_id}"',
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes"
        }
    )


@app.delete("/delete/{file_id}")
def delete(
    file_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Elimina un archivo del DataNode.
    Requiere token de servicio del MetaNameNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        authorization: Token de servicio
    
    Returns:
        Confirmación de eliminación
    """
    # Verificar autenticación de servicio (solo MetaNameNode puede eliminar)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    # Verificar que viene del MetaNameNode
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        print(f"[DATANODE] ❌ Acceso denegado a /delete: service_id={service_id} no es un namenode")
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede eliminar archivos")
    
    print(f"[DATANODE] DELETE /delete/{file_id}")
    
    # Verificar si el archivo existe antes de intentar eliminarlo
    if not file_exists(file_id):
        print(f"[DATANODE] ⚠️  Archivo {file_id} no existe, retornando 404")
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    # Intentar eliminar el archivo
    print(f"[DATANODE] Intentando eliminar archivo físicamente: {file_id}")
    if delete_file(file_id):
        # Verificar que realmente se eliminó
        if file_exists(file_id):
            print(f"[DATANODE] ❌ ERROR: Archivo {file_id} aún existe después de delete_file()")
            raise HTTPException(status_code=500, detail="Error: archivo no se eliminó correctamente")
        print(f"[DATANODE] ✅ Archivo {file_id} eliminado correctamente del disco")
        return {
            "success": True,
            "message": f"Archivo '{file_id}' eliminado correctamente"
        }
    else:
        print(f"[DATANODE] ❌ Error en delete_file() para {file_id}")
        raise HTTPException(status_code=500, detail="Error al eliminar archivo")


# ========== CHUNKED STORAGE ENDPOINTS ==========

@app.post("/store/init")
def init_chunked_store(
    file_id: str = Form(...),
    total_chunks: int = Form(...),
    chunk_size: int = Form(...),
    file_size: int = Form(...),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Inicia una sesión de recepción por chunks desde el NameNode.
    """
    # Verificar autenticación
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload or "namenode" not in payload.get("service_id", "").lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede iniciar almacenamiento")
    
    # Crear sesión
    session_id = str(uuid.uuid4())
    
    try:
        session = chunked_storage_manager.create_session(
            session_id=session_id,
            file_id=file_id,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            expected_file_size=file_size
        )
        
        return {
            "session_id": session_id,
            "file_id": file_id,
            "total_chunks": total_chunks
        }
    except Exception as e:
        print(f"[DATANODE] Error creando sesión de almacenamiento: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/store/session/{session_id}/chunk/{chunk_index}")
async def store_chunk(
    session_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
    chunk_hash: str = Form(...),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Recibe un chunk individual del archivo desde el NameNode.
    """
    # Verificar autenticación
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload or "namenode" not in payload.get("service_id", "").lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede almacenar chunks")
    
    session = chunked_storage_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    # Leer chunk
    chunk_data = await chunk.read()
    
    # Guardar chunk
    success = chunked_storage_manager.save_chunk(
        session_id=session_id,
        chunk_index=chunk_index,
        chunk_data=chunk_data,
        chunk_hash=chunk_hash
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="Error guardando chunk (hash inválido)")
    
    return {
        "success": True,
        "chunk_index": chunk_index,
        "progress_percentage": session.progress_percentage
    }


@app.post("/store/session/{session_id}/finalize")
def finalize_chunked_store(
    session_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Finaliza la recepción: ensambla chunks y guarda en almacenamiento final.
    """
    # Verificar autenticación
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload or "namenode" not in payload.get("service_id", "").lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede finalizar almacenamiento")
    
    session = chunked_storage_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    # Verificar completitud
    if not session.is_complete:
        missing = session.total_chunks - len(session.received_chunks)
        raise HTTPException(
            status_code=400,
            detail=f"Sesión incompleta: faltan {missing} chunks"
        )
    
    # Ensamblar y guardar
    print(f"[DATANODE] Ensamblando y guardando file_id={session.file_id}...")
    
    if chunked_storage_manager.assemble_and_store(session_id):
        # Limpiar sesión
        chunked_storage_manager.cleanup_session(session_id)
        
        return {
            "success": True,
            "message": f"Archivo {session.file_id} almacenado correctamente",
            "file_id": session.file_id
        }
    else:
        raise HTTPException(status_code=500, detail="Error ensamblando archivo")


@app.get("/store/session/{session_id}/status")
def get_store_session_status(
    session_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Obtiene el estado de una sesión de almacenamiento.
    """
    # Verificar autenticación
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload or "namenode" not in payload.get("service_id", "").lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede ver estado")
    
    session = chunked_storage_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    return {
        "session_id": session_id,
        "file_id": session.file_id,
        "total_chunks": session.total_chunks,
        "received_chunks": list(session.received_chunks),
        "progress_percentage": session.progress_percentage,
        "is_complete": session.is_complete
    }


# ========== CLIENT DIRECT UPLOAD ENDPOINTS ==========

@app.post("/client/upload/init")
def init_client_chunked_upload(
    file_id: str = Form(...),
    total_chunks: int = Form(...),
    chunk_size: int = Form(...),
    file_size: int = Form(...),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Inicia una sesión de recepción por chunks desde un cliente.
    Requiere token temporal emitido por el NameNode.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_upload")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    # Verificar que el file_hash coincide
    expected_file_hash = payload.get("file_hash")
    print(f"[DATANODE] [CLIENT_UPLOAD_INIT] Verificando file_hash: expected={expected_file_hash}, received={file_id}, match={expected_file_hash == file_id if expected_file_hash else 'N/A'}")
    if expected_file_hash and expected_file_hash != file_id:
        raise HTTPException(status_code=403, detail=f"file_id no coincide con el token (esperado: {expected_file_hash}, recibido: {file_id})")
    
    # Verificar que el datanode_id coincide
    expected_datanode_id = payload.get("datanode_id")
    if expected_datanode_id and expected_datanode_id != DATANODE_ID:
        raise HTTPException(status_code=403, detail="Token no válido para este DataNode")
    
    # Verificar si ya existe una sesión para este file_id (reanudación)
    existing_session = chunked_storage_manager.find_session_by_file_id(file_id)
    
    if existing_session:
        # Sesión existente encontrada, reanudar desde donde se quedó
        print(f"[DATANODE] Sesión existente encontrada para file_id={file_id}, reanudando upload...")
        print(f"[DATANODE] Progreso: {len(existing_session.received_chunks)}/{existing_session.total_chunks} chunks recibidos")
        
        return {
            "session_id": existing_session.session_id,
            "file_id": file_id,
            "total_chunks": existing_session.total_chunks,
            "resumed": True,
            "received_chunks": sorted(list(existing_session.received_chunks)),
            "missing_chunks": sorted(list(set(range(existing_session.total_chunks)) - existing_session.received_chunks)),
            "progress_percentage": existing_session.progress_percentage
        }
    
    # Crear nueva sesión
    session_id = str(uuid.uuid4())
    
    try:
        session = chunked_storage_manager.create_session(
            session_id=session_id,
            file_id=file_id,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            expected_file_size=file_size
        )
        
        print(f"[DATANODE] Sesión de upload de cliente creada: session_id={session_id}, file_id={file_id}, user={payload.get('user_id')}")
        
        return {
            "session_id": session_id,
            "file_id": file_id,
            "total_chunks": total_chunks,
            "resumed": False
        }
    except Exception as e:
        print(f"[DATANODE] Error creando sesión de upload de cliente: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/client/upload/session/{session_id}/chunk/{chunk_index}")
async def client_store_chunk(
    session_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
    chunk_hash: str = Form(...),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Recibe un chunk individual del archivo desde un cliente.
    Requiere token temporal emitido por el NameNode.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_upload")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    session = chunked_storage_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    # Verificar que el file_id coincide
    expected_file_hash = payload.get("file_hash")
    if expected_file_hash and expected_file_hash != session.file_id:
        raise HTTPException(status_code=403, detail="file_id no coincide con el token")
    
    # Leer chunk
    chunk_data = await chunk.read()
    
    # Guardar chunk
    success = chunked_storage_manager.save_chunk(
        session_id=session_id,
        chunk_index=chunk_index,
        chunk_data=chunk_data,
        chunk_hash=chunk_hash
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="Error guardando chunk (hash inválido)")
    
    return {
        "success": True,
        "chunk_index": chunk_index,
        "progress_percentage": session.progress_percentage
    }


@app.get("/client/upload/progress/{file_id}")
def get_client_upload_progress(
    file_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Consulta el progreso de un upload por file_id (hash del archivo).
    Útil para reanudar uploads interrumpidos.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_upload")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    # Verificar que el file_hash coincide
    expected_file_hash = payload.get("file_hash")
    if expected_file_hash and expected_file_hash != file_id:
        raise HTTPException(status_code=403, detail="file_id no coincide con el token")
    
    # Buscar sesión existente
    progress = chunked_storage_manager.get_session_progress(file_id)
    
    if not progress:
        raise HTTPException(status_code=404, detail="No hay sesión activa para este archivo")
    
    return progress


@app.get("/client/download/{file_id}/chunk/{chunk_index}")
def client_download_chunk(
    file_id: str,
    chunk_index: int,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Descarga un chunk individual de un archivo.
    Requiere token temporal emitido por el NameNode.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_download")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    # Verificar que el file_hash coincide
    expected_file_hash = payload.get("file_hash")
    if expected_file_hash and expected_file_hash != file_id:
        raise HTTPException(status_code=403, detail="file_id no coincide con el token")
    
    # Verificar que el datanode_id coincide
    expected_datanode_id = payload.get("datanode_id")
    if expected_datanode_id and expected_datanode_id != DATANODE_ID:
        raise HTTPException(status_code=403, detail="Token no válido para este DataNode")
    
    print(f"[DATANODE] GET /client/download/{file_id}/chunk/{chunk_index} (user={payload.get('user_id')})")
    
    # Obtener chunk específico
    from datanode.storage import get_chunk
    
    chunk_data = get_chunk(file_id, chunk_index)
    if chunk_data is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_index} no encontrado para archivo '{file_id}'")
    
    from fastapi.responses import Response
    return Response(
        content=chunk_data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="chunk_{chunk_index:06d}"',
            "Content-Length": str(len(chunk_data)),
            "Accept-Ranges": "bytes"
        }
    )


@app.post("/client/upload/session/{session_id}/finalize")
def client_finalize_chunked_upload(
    session_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Finaliza la recepción de chunks desde un cliente: ensambla chunks y guarda en almacenamiento final.
    Requiere token temporal emitido por el NameNode.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_upload")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    session = chunked_storage_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    # Verificar que el file_id coincide
    expected_file_hash = payload.get("file_hash")
    if expected_file_hash and expected_file_hash != session.file_id:
        raise HTTPException(status_code=403, detail="file_id no coincide con el token")
    
    # Verificar que la sesión está completa
    if not session.is_complete:
        missing = session.total_chunks - len(session.received_chunks)
        print(f"[DATANODE] ❌ Sesión incompleta: faltan {missing} chunks de {session.total_chunks}")
        raise HTTPException(
            status_code=400,
            detail=f"Sesión incompleta: faltan {missing} chunks de {session.total_chunks}"
        )
    
    # Finalizar sesión
    try:
        print(f"[DATANODE] Finalizando upload de cliente: session_id={session_id}, file_id={session.file_id[:16]}..., chunks={session.total_chunks}, user={payload.get('user_id')}")
        result = chunked_storage_manager.assemble_and_store(session_id)
        if result:
            # Calcular tamaño total de los chunks
            total_size = sum(chunk_info.size for chunk_info in session.chunks.values())
            print(f"[DATANODE] ✅ Upload de cliente finalizado: session_id={session_id}, file_id={session.file_id[:16]}..., size={total_size} bytes, user={payload.get('user_id')}")
            return {
                "success": True,
                "message": f"Archivo '{session.file_id}' almacenado correctamente",
                "file_id": session.file_id,
                "size": total_size
            }
        else:
            print(f"[DATANODE] ❌ Error al finalizar almacenamiento: assemble_and_store retornó False")
            raise HTTPException(status_code=500, detail="Error al finalizar almacenamiento: no se pudieron guardar los chunks")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[DATANODE] ❌ Error finalizando upload de cliente: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al finalizar almacenamiento: {str(e)}")


@app.get("/client/download/{file_id}")
def client_download(
    file_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization"),
    range_header: Optional[str] = Header(None, alias="Range")
):
    """
    Permite a un cliente descargar directamente un archivo desde el DataNode.
    Requiere token temporal emitido por el NameNode.
    """
    # Verificar autenticación de cliente
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de cliente")
    
    token = authorization.split(" ")[1]
    payload = verify_client_token(token, expected_type="client_download")
    if not payload:
        raise HTTPException(status_code=403, detail="Token de cliente inválido o expirado")
    
    # Verificar que el file_hash coincide
    expected_file_hash = payload.get("file_hash")
    if expected_file_hash and expected_file_hash != file_id:
        raise HTTPException(status_code=403, detail="file_id no coincide con el token")
    
    # Verificar que el datanode_id coincide
    expected_datanode_id = payload.get("datanode_id")
    if expected_datanode_id and expected_datanode_id != DATANODE_ID:
        raise HTTPException(status_code=403, detail="Token no válido para este DataNode")
    
    print(f"[DATANODE] GET /client/download/{file_id} (user={payload.get('user_id')}, range={range_header})")
    
    # Recuperar archivo
    file_content = retrieve_file(file_id)
    
    if file_content is None:
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    file_size = len(file_content)
    
    # Soporte para HTTP Range requests
    if range_header:
        # Parsear Range header (ej: "bytes=0-1023")
        try:
            range_match = range_header.replace("bytes=", "").split("-")
            start = int(range_match[0]) if range_match[0] else 0
            end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1
            
            if start < 0 or end >= file_size or start > end:
                raise HTTPException(status_code=416, detail="Range no satisfacible")
            
            content = file_content[start:end + 1]
            content_length = len(content)
            
            from fastapi.responses import Response
            return Response(
                content=content,
                status_code=206,
                media_type="application/octet-stream",
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(content_length),
                    "Accept-Ranges": "bytes"
                }
            )
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Range header inválido")
    
    # Retornar archivo completo
    from fastapi.responses import Response
    return Response(
        content=file_content,
        status_code=200,
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes"
        }
    )


# ========== ENDPOINT ORIGINAL (COMPATIBILIDAD) ==========

@app.get("/chunks/{file_id}/info")
def get_chunks_info(
    file_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Obtiene información sobre los chunks de un archivo.
    Requiere token de servicio del MetaNameNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        authorization: Token de servicio
    
    Returns:
        Información sobre los chunks (número total, tamaño, etc.)
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede leer información de chunks")
    
    from datanode.storage import get_chunk_count, get_chunks_dir
    import os
    
    chunks_dir = get_chunks_dir(file_id)
    if not os.path.exists(chunks_dir):
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    chunk_count = get_chunk_count(file_id)
    if chunk_count == 0:
        raise HTTPException(status_code=404, detail=f"No se encontraron chunks para '{file_id}'")
    
    # Calcular tamaño total
    total_size = 0
    chunk_sizes = []
    for i in range(chunk_count):
        from datanode.storage import get_chunk_path
        chunk_path = get_chunk_path(file_id, i)
        if os.path.exists(chunk_path):
            size = os.path.getsize(chunk_path)
            chunk_sizes.append(size)
            total_size += size
    
    return {
        "file_id": file_id,
        "chunk_count": chunk_count,
        "total_size": total_size,
        "chunk_sizes": chunk_sizes
    }


@app.get("/chunks/{file_id}/{chunk_index}")
def get_chunk(
    file_id: str,
    chunk_index: int,
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    Obtiene un chunk específico de un archivo.
    Requiere token de servicio del MetaNameNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        chunk_index: Índice del chunk
        authorization: Token de servicio
    
    Returns:
        Contenido del chunk
    """
    # Verificar autenticación de servicio
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Se requiere token de servicio")
    
    token = authorization.split(" ")[1]
    payload = verify_service_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Token de servicio inválido")
    
    service_id = payload.get("service_id") or payload.get("sub", "")
    if "namenode" not in service_id.lower():
        raise HTTPException(status_code=403, detail="Solo MetaNameNode puede leer chunks")
    
    from datanode.storage import get_chunk
    
    chunk_data = get_chunk(file_id, chunk_index)
    if chunk_data is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_index} no encontrado para archivo '{file_id}'")
    
    from fastapi.responses import Response
    return Response(
        content=chunk_data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="chunk_{chunk_index:06d}"',
            "Content-Length": str(len(chunk_data))
        }
    )


@app.get("/info")
def info():
    """Obtiene información del DataNode"""
    storage_info = get_storage_info()
    
    return {
        "node_id": DATANODE_ID,
        "port": DATANODE_PORT,
        "registered": registry_client.registered,
        "namenode_url": registry_client.namenode_url,
        "storage": storage_info
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DATANODE_PORT)

