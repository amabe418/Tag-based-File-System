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

from security.service_auth import verify_service_token, validate_service_request
from security.rate_limit import RateLimitMiddleware

from datanode.storage import (
    store_file, retrieve_file, delete_file, file_exists, get_storage_info
)
from datanode.registry_client import registry_client
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


# ========== ENDPOINT ORIGINAL (COMPATIBILIDAD) ==========

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

