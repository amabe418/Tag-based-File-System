"""
DataNode - Servicio de almacenamiento distribuido
Almacena archivos físicos y se registra con el MetaNameNode
"""
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import os
from contextlib import asynccontextmanager

from datanode.storage import (
    store_file, retrieve_file, delete_file, file_exists, get_storage_info
)
from datanode.registry_client import registry_client

# Configuración
DATANODE_ID = os.getenv("DATANODE_ID", "datanode-1")
DATANODE_PORT = int(os.getenv("DATANODE_PORT", "8001"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de la aplicación"""
    # Startup
    print(f"[DATANODE] Iniciando DataNode: {DATANODE_ID}")
    print(f"[DATANODE] Puerto: {DATANODE_PORT}")
    
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
async def store(file_id: str = Form(...), file: UploadFile = File(...)):
    """
    Almacena un archivo en el DataNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
        file: Archivo a almacenar
    
    Returns:
        Confirmación de almacenamiento
    """
    print(f"[DATANODE] POST /store recibido: file_id={file_id}, filename={file.filename}")
    
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
def retrieve(file_id: str):
    """
    Recupera un archivo del DataNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        Contenido del archivo
    """
    print(f"[DATANODE] GET /retrieve/{file_id}")
    
    file_content = retrieve_file(file_id)
    
    if file_content is None:
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    # Retornar archivo como respuesta
    from fastapi.responses import Response
    return Response(
        content=file_content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{file_id}"'
        }
    )


@app.delete("/delete/{file_id}")
def delete(file_id: str):
    """
    Elimina un archivo del DataNode.
    
    Args:
        file_id: Identificador único del archivo (hash)
    
    Returns:
        Confirmación de eliminación
    """
    print(f"[DATANODE] DELETE /delete/{file_id}")
    
    if not file_exists(file_id):
        raise HTTPException(status_code=404, detail=f"Archivo '{file_id}' no encontrado")
    
    if delete_file(file_id):
        return {
            "success": True,
            "message": f"Archivo '{file_id}' eliminado correctamente"
        }
    else:
        raise HTTPException(status_code=500, detail="Error al eliminar archivo")


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

