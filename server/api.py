# server/api.py
from fastapi import FastAPI, UploadFile, Form, HTTPException, Query
from fastapi.responses import FileResponse
from Server.core import manager
from Server.core import database
from Server.core import registry_client
import os
import shutil
from typing import List, Optional
from contextlib import asynccontextmanager

database.init_db()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Maneja el ciclo de vida de la aplicación"""
    # Startup
    registry_client.registry_client.start()
    yield
    # Shutdown
    registry_client.registry_client.stop()

app = FastAPI(title="Tag-Based File System API", lifespan=lifespan)

@app.get("/")
def root():
    return {"message": "Servidor funcionando"}

@app.post("/add")
async def add_file(file: UploadFile, tags: str = Form(...)):
    """
    Sube un archivo al sistema con etiquetas.
    """
    os.makedirs("uploads", exist_ok=True)
    temp_path = os.path.join("uploads", file.filename)

    # Guardar temporalmente el archivo subido
    with open(temp_path, "wb") as f:
        f.write(await file.read())

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    added = manager.add_files([temp_path], tag_list)

    # Eliminar temporal después de copiar a storage
    if os.path.exists(temp_path):
        os.remove(temp_path)

    if not added:
        raise HTTPException(status_code=400, detail="No se pudo agregar el archivo")

    return {"success": True, "message": f"Archivo '{file.filename}' agregado correctamente"}

@app.get("/list")
def list_files(tags: Optional[List[str]] = Query(None)):
    """
    Lista todos los archivos y sus etiquetas.
    """
    files = manager.query_files(query_tags=tags)
    formatted = [
        {"id": fid, "name": name, "tags": tags, "path": path}
        for fid, name, tags, path in files
    ]
    return {"files": formatted}

@app.delete("/delete")
def delete_files(tags: str):
    """
    Elimina archivos según etiquetas.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    deleted = manager.delete_files(tag_list)
    return {"success": deleted, "message": "Archivos eliminados" if deleted else "No se encontró coincidencia"}

@app.post("/add-tags")
def add_tags(query: str, new_tags: str):
    query_tags = [t.strip() for t in query.split(",") if t.strip()]
    tags = [t.strip() for t in new_tags.split(",") if t.strip()]
    ok = manager.add_tags(query_tags, tags)
    return {"success": ok}

@app.post("/delete-tags")
def delete_tags(query: str, del_tags: str):
    query_tags = [t.strip() for t in query.split(",") if t.strip()]
    tags = [t.strip() for t in del_tags.split(",") if t.strip()]
    ok = manager.delete_tags(query_tags, tags)
    return {"success": ok}

@app.get("/download/{file_name}")
def download_file(file_name: str):
    path = manager.get_file_path(file_name)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path=path, filename=file_name)
