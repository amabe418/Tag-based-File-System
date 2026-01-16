"""
Interfaz web Flask para el sistema de archivos basado en etiquetas.
Sin límites de tamaño de archivo, descarga directa nativa.
"""
import os
import hashlib
import requests
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
import json
from werkzeug.utils import secure_filename
from namenode_client import namenode_client
import io
import threading

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tbfs-secret-key-change-me")

# Configuración
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB

# Almacenamiento de progreso de uploads (upload_id -> progreso)
upload_progress = {}
progress_lock = threading.Lock()


def get_leader_url():
    """Obtiene la URL del líder del cluster."""
    url, error = namenode_client.get_leader_url()
    return url, error


def get_auth_headers(token):
    """Construye headers de autenticación."""
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ============ RUTAS DE LA INTERFAZ ============

@app.route("/")
def index():
    """Página principal."""
    return render_template("index.html")


# ============ API: AUTENTICACIÓN ============

@app.route("/api/login", methods=["POST"])
def api_login():
    """Login de usuario."""
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    if not username or not password:
        return jsonify({"success": False, "error": "Usuario y contraseña requeridos"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        response = requests.post(
            f"{leader_url}/auth/login",
            json={"username": username, "password": password},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                "success": True,
                "token": data.get("access_token"),
                "username": username
            })
        else:
            return jsonify({"success": False, "error": "Credenciales inválidas"}), 401
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/register", methods=["POST"])
def api_register():
    """Registro de nuevo usuario (público)."""
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    if not username or not password:
        return jsonify({"success": False, "error": "Usuario y contraseña requeridos"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        # Usar /auth/signup que es público (no requiere autenticación)
        response = requests.post(
            f"{leader_url}/auth/signup",
            json={"username": username, "password": password},
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            return jsonify({"success": True, "message": "Usuario creado exitosamente"})
        else:
            error_msg = response.json().get("detail", "Error al crear usuario")
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============ API: ARCHIVOS ============

@app.route("/api/files", methods=["GET"])
def api_list_files():
    """Lista archivos del usuario."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    tags = request.args.get("tags", "")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        endpoint = f"{leader_url}/list"
        if tags:
            endpoint += f"?tags={tags}"
        
        print(f"[FLASK] Listando archivos desde: {endpoint}")
        
        response = requests.get(
            endpoint,
            headers=get_auth_headers(token),
            timeout=10
        )
        
        print(f"[FLASK] Respuesta: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            # El endpoint devuelve {"files": [...]}
            files = data.get("files", data) if isinstance(data, dict) else data
            return jsonify({"success": True, "files": files})
        else:
            try:
                error_data = response.json()
                error_msg = error_data.get("detail", f"Error HTTP {response.status_code}")
            except:
                error_msg = f"Error HTTP {response.status_code}: {response.text[:200]}"
            print(f"[FLASK] Error listando: {error_msg}")
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        print(f"[FLASK] Excepción listando archivos: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Sube un archivo."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No se envió archivo"}), 400
    
    file = request.files["file"]
    tags = request.form.get("tags", "")
    
    if file.filename == "":
        return jsonify({"success": False, "error": "Nombre de archivo vacío"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        filename = secure_filename(file.filename)
        file_content = file.read()
        file_size = len(file_content)
        
        print(f"[FLASK] Subiendo archivo: {filename} ({file_size} bytes)")
        
        # Usar método directo para archivos pequeños, chunked para grandes
        # Asegurar que tags no esté vacío (usar "general" por defecto)
        if not tags or not tags.strip():
            tags = "general"
        
        if file_size <= CHUNK_SIZE:
            # Subida directa
            response = requests.post(
                f"{leader_url}/add",
                files={"file": (filename, file_content)},
                data={"tags": tags},
                headers=get_auth_headers(token),
                timeout=120
            )
            
            # Procesar respuesta del NameNode
            if response.status_code == 200:
                try:
                    data = response.json()
                    return jsonify({
                        "success": True,
                        "file_id": data.get("file_id"),
                        "message": data.get("message", f"Archivo '{filename}' subido correctamente")
                    })
                except ValueError as e:
                    # Si la respuesta no es JSON válido
                    print(f"[FLASK] Error: respuesta del NameNode no es JSON válido: {response.text[:200]}")
                    return jsonify({"success": False, "error": "Respuesta inválida del servidor"}), 500
            else:
                # Manejar errores del NameNode
                try:
                    error_data = response.json()
                    error_msg = error_data.get("detail", error_data.get("error", f"Error HTTP {response.status_code}"))
                except:
                    error_msg = f"Error HTTP {response.status_code}: {response.text[:200]}"
                print(f"[FLASK] Error del servidor: {error_msg}")
                return jsonify({"success": False, "error": str(error_msg)}), response.status_code
        else:
            # Subida por chunks - transferencia directa a DataNode
            file_hash_hex = hashlib.sha256(file_content).hexdigest()
            file_hash = f"sha256:{file_hash_hex}"
            total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
            
            # 1. Iniciar upload en NameNode (crea metadatos, asigna DataNode, genera token)
            init_response = requests.post(
                f"{leader_url}/upload/init",
                data={
                    "filename": filename,
                    "file_hash": file_hash,
                    "file_size": file_size,
                    "tags": tags,
                    "chunk_size": CHUNK_SIZE
                },
                headers=get_auth_headers(token),
                timeout=30
            )
            
            if init_response.status_code != 200:
                error_msg = init_response.json().get("detail", "Error iniciando upload") if init_response.status_code < 500 else "Error del servidor"
                return jsonify({"success": False, "error": error_msg}), init_response.status_code
            
            upload_data = init_response.json()
            upload_id = upload_data["upload_id"]
            datanode_url = upload_data["datanode_url"]
            client_token = upload_data["client_token"]
            file_id = upload_data["file_id"]
            file_hash = upload_data["file_hash"]
            
            print(f"[FLASK] Upload iniciado: upload_id={upload_id}, datanode={datanode_url}, file_id={file_id}, file_hash={file_hash[:32]}...")
            
            # Inicializar progreso ANTES de empezar a subir chunks
            with progress_lock:
                upload_progress[upload_id] = {
                    "filename": filename,
                    "total_chunks": total_chunks,
                    "chunks_uploaded": 0,
                    "progress": 0.0,
                    "bytes_uploaded": 0,
                    "file_size": file_size,
                    "status": "initializing"
                }
            
            # Función para ejecutar la subida en un hilo separado
            def upload_chunks_thread():
                try:
                    # 2. Iniciar sesión en DataNode
                    datanode_payload = {
                        "file_id": file_hash,
                        "total_chunks": total_chunks,
                        "chunk_size": CHUNK_SIZE,
                        "file_size": file_size
                    }
                    datanode_init_response = requests.post(
                        f"{datanode_url}/client/upload/init",
                        data=datanode_payload,
                        headers={"Authorization": f"Bearer {client_token}"},
                        timeout=30
                    )
                    
                    if datanode_init_response.status_code != 200:
                        error_msg = datanode_init_response.json().get("detail", "Error iniciando sesión en DataNode")
                        with progress_lock:
                            if upload_id in upload_progress:
                                upload_progress[upload_id]["status"] = "error"
                                upload_progress[upload_id]["error"] = error_msg
                        return
                    
                    datanode_session = datanode_init_response.json()
                    session_id = datanode_session["session_id"]
                    
                    # Actualizar estado: empezando a subir chunks
                    with progress_lock:
                        if upload_id in upload_progress:
                            upload_progress[upload_id]["status"] = "uploading_chunks"
                    
                    # 3. Subir chunks directamente al DataNode
                    for i in range(total_chunks):
                        start = i * CHUNK_SIZE
                        end = min(start + CHUNK_SIZE, file_size)
                        chunk_data = file_content[start:end]
                        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                        
                        chunk_response = requests.post(
                            f"{datanode_url}/client/upload/session/{session_id}/chunk/{i}",
                            files={"chunk": (f"chunk_{i}", chunk_data)},
                            data={"chunk_hash": chunk_hash},
                            headers={"Authorization": f"Bearer {client_token}"},
                            timeout=120
                        )
                        
                        if chunk_response.status_code != 200:
                            error_msg = chunk_response.json().get("detail", f"Error en chunk {i}")
                            with progress_lock:
                                if upload_id in upload_progress:
                                    upload_progress[upload_id]["status"] = "error"
                                    upload_progress[upload_id]["error"] = error_msg
                            return
                        
                        chunks_uploaded = i + 1
                        progress = (chunks_uploaded / total_chunks) * 100
                        bytes_uploaded = min(chunks_uploaded * CHUNK_SIZE, file_size)
                        
                        # Actualizar progreso en memoria
                        with progress_lock:
                            if upload_id in upload_progress:
                                upload_progress[upload_id]["chunks_uploaded"] = chunks_uploaded
                                upload_progress[upload_id]["progress"] = progress
                                upload_progress[upload_id]["bytes_uploaded"] = bytes_uploaded
                        
                        print(f"[FLASK] Progreso: {chunks_uploaded}/{total_chunks} chunks ({progress:.1f}%)")
                    
                    # Actualizar estado: chunks completados, ahora finalizando
                    with progress_lock:
                        if upload_id in upload_progress:
                            upload_progress[upload_id]["status"] = "finalizing"
                    
                    # 4. Finalizar en DataNode
                    datanode_finalize_response = requests.post(
                        f"{datanode_url}/client/upload/session/{session_id}/finalize",
                        headers={"Authorization": f"Bearer {client_token}"},
                        timeout=300
                    )
                    
                    if datanode_finalize_response.status_code != 200:
                        error_msg = datanode_finalize_response.json().get("detail", "Error finalizando en DataNode")
                        with progress_lock:
                            if upload_id in upload_progress:
                                upload_progress[upload_id]["status"] = "error"
                                upload_progress[upload_id]["error"] = error_msg
                        return
                    
                    # Actualizar estado: finalizando en NameNode
                    with progress_lock:
                        if upload_id in upload_progress:
                            upload_progress[upload_id]["status"] = "finalizing_namenode"
                    
                    # 5. Finalizar en NameNode
                    finalize_response = requests.post(
                        f"{leader_url}/upload/{upload_id}/finalize",
                        headers=get_auth_headers(token),
                        timeout=30
                    )
                    
                    # Limpiar progreso después de completar
                    with progress_lock:
                        if upload_id in upload_progress:
                            if finalize_response.status_code == 200:
                                upload_progress[upload_id]["status"] = "completed"
                                upload_progress[upload_id]["result"] = finalize_response.json()
                            else:
                                upload_progress[upload_id]["status"] = "error"
                                try:
                                    upload_progress[upload_id]["error"] = finalize_response.json().get("detail", "Error finalizando")
                                except:
                                    upload_progress[upload_id]["error"] = f"Error HTTP {finalize_response.status_code}"
                    
                except Exception as e:
                    print(f"[FLASK] Error en hilo de upload: {e}")
                    import traceback
                    traceback.print_exc()
                    with progress_lock:
                        if upload_id in upload_progress:
                            upload_progress[upload_id]["status"] = "error"
                            upload_progress[upload_id]["error"] = str(e)
            
            # Iniciar subida en hilo separado
            upload_thread = threading.Thread(target=upload_chunks_thread, daemon=True)
            upload_thread.start()
            
            # Retornar inmediatamente con upload_id para que el frontend pueda consultar progreso
            return jsonify({
                "success": True,
                "upload_id": upload_id,
                "message": f"Upload iniciado para '{filename}'",
                "total_chunks": total_chunks,
                "file_size": file_size
            })
            
    except Exception as e:
        print(f"[FLASK] Error subiendo archivo: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload-progress/<upload_id>", methods=["GET"])
def api_upload_progress(upload_id):
    """Consulta el progreso de un upload en curso."""
    with progress_lock:
        progress_data = upload_progress.get(upload_id)
    
    if not progress_data:
        return jsonify({"success": False, "error": "Upload no encontrado"}), 404
    
    return jsonify({
        "success": True,
        "upload_id": upload_id,
        "filename": progress_data.get("filename"),
        "total_chunks": progress_data.get("total_chunks"),
        "chunks_uploaded": progress_data.get("chunks_uploaded", 0),
        "progress": progress_data.get("progress", 0.0),
        "bytes_uploaded": progress_data.get("bytes_uploaded", 0),
        "file_size": progress_data.get("file_size"),
        "status": progress_data.get("status"),
        "error": progress_data.get("error") if progress_data.get("status") == "error" else None,
        "result": progress_data.get("result") if progress_data.get("status") == "completed" else None
    })


@app.route("/api/download/<filename>", methods=["GET"])
def api_download(filename):
    """Descarga un archivo - streaming directo."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        # Hacer petición al namenode con streaming
        response = requests.get(
            f"{leader_url}/download/{filename}",
            headers=get_auth_headers(token),
            stream=True,
            timeout=300
        )
        
        if response.status_code == 200:
            # Stream directo al cliente
            def generate():
                for chunk in response.iter_content(chunk_size=8192):
                    yield chunk
            
            return Response(
                stream_with_context(generate()),
                headers={
                    "Content-Disposition": f"attachment; filename={filename}",
                    "Content-Type": response.headers.get("Content-Type", "application/octet-stream"),
                    "Content-Length": response.headers.get("Content-Length", "")
                }
            )
        else:
            return jsonify({"success": False, "error": "Archivo no encontrado"}), response.status_code
            
    except Exception as e:
        print(f"[FLASK] Error descargando archivo: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/delete/<int:file_id>", methods=["DELETE"])
def api_delete(file_id):
    """Elimina un archivo por su ID."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        response = requests.delete(
            f"{leader_url}/delete-by-id/{file_id}",
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            return jsonify({"success": data.get("success", True), "message": data.get("message", "Archivo eliminado")})
        else:
            try:
                error_msg = response.json().get("detail", "Error al eliminar")
            except:
                error_msg = f"Error HTTP {response.status_code}"
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/add-tags", methods=["POST"])
def api_add_tags():
    """Agrega etiquetas a archivos."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    data = request.json
    
    query_tags = data.get("query_tags", "")
    new_tags = data.get("new_tags", "")
    
    if not query_tags or not new_tags:
        return jsonify({"success": False, "error": "Se requieren etiquetas de búsqueda y nuevas etiquetas"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        response = requests.post(
            f"{leader_url}/add-tags",
            params={"query": query_tags, "new_tags": new_tags},
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if response.status_code == 200:
            return jsonify({"success": True, "message": "Etiquetas agregadas correctamente"})
        else:
            try:
                error_msg = response.json().get("detail", f"Error HTTP {response.status_code}")
            except:
                error_msg = f"Error HTTP {response.status_code}"
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/delete-tags", methods=["POST"])
def api_delete_tags():
    """Elimina etiquetas de archivos."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    data = request.json
    
    query_tags = data.get("query_tags", "")
    del_tags = data.get("del_tags", "")
    
    if not query_tags or not del_tags:
        return jsonify({"success": False, "error": "Se requieren etiquetas de búsqueda y etiquetas a eliminar"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        response = requests.post(
            f"{leader_url}/delete-tags",
            params={"query": query_tags, "del_tags": del_tags},
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if response.status_code == 200:
            return jsonify({"success": True, "message": "Etiquetas eliminadas correctamente"})
        else:
            try:
                error_msg = response.json().get("detail", f"Error HTTP {response.status_code}")
            except:
                error_msg = f"Error HTTP {response.status_code}"
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/delete-by-tags", methods=["DELETE"])
def api_delete_by_tags():
    """Elimina archivos que tienen EXACTAMENTE las etiquetas especificadas."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    tags = request.args.get("tags", "")
    
    if not tags:
        return jsonify({"success": False, "error": "Se requieren etiquetas"}), 400
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        # Usar endpoint de coincidencia exacta
        response = requests.delete(
            f"{leader_url}/delete-exact",
            params={"tags": tags},
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            deleted = data.get("deleted", 0)
            message = data.get("message", f"{deleted} archivo(s) eliminado(s)")
            return jsonify({"success": data.get("success", deleted > 0), "message": message})
        else:
            try:
                error_msg = response.json().get("detail", f"Error HTTP {response.status_code}")
            except:
                error_msg = f"Error HTTP {response.status_code}"
            return jsonify({"success": False, "error": error_msg}), response.status_code
            
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Estado del sistema."""
    leader_url, error = get_leader_url()
    
    if leader_url:
        try:
            response = requests.get(f"{leader_url}/", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return jsonify({
                    "success": True,
                    "connected": True,
                    "leader_url": leader_url,
                    "node_id": data.get("node_id"),
                    "is_leader": data.get("is_leader")
                })
        except:
            pass
    
    return jsonify({
        "success": True,
        "connected": False,
        "error": error
    })


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"[FLASK] Iniciando servidor en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
