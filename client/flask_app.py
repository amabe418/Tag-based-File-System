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

# Almacenamiento de progreso de descargas (download_id -> progreso)
download_progress = {}
download_lock = threading.Lock()

# Directorio de descargas (mapeado desde el host)
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/app/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


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
            file_id = upload_data.get("file_id")  # Puede ser None hasta que finalice
            file_hash = upload_data["file_hash"]
            
            print(f"[FLASK] Upload iniciado: upload_id={upload_id}, datanode={datanode_url}, file_id={file_id if file_id else 'None (se creará al finalizar)'}, file_hash={file_hash[:32]}...")
            
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
                    resumed = datanode_session.get("resumed", False)
                    received_chunks = set(datanode_session.get("received_chunks", []))
                    missing_chunks = datanode_session.get("missing_chunks", [])
                    
                    # Determinar qué chunks subir
                    if resumed and received_chunks:
                        # Reanudar: solo subir chunks faltantes
                        chunks_to_upload = missing_chunks if missing_chunks else [i for i in range(total_chunks) if i not in received_chunks]
                        print(f"[FLASK] Reanudando upload: {len(received_chunks)} chunks ya recibidos, subiendo {len(chunks_to_upload)} chunks faltantes")
                    else:
                        # Nuevo upload: subir todos los chunks
                        chunks_to_upload = list(range(total_chunks))
                        print(f"[FLASK] Nuevo upload: subiendo todos los {total_chunks} chunks")
                    
                    # Actualizar estado: empezando a subir chunks
                    with progress_lock:
                        if upload_id in upload_progress:
                            upload_progress[upload_id]["status"] = "uploading_chunks"
                            if resumed:
                                upload_progress[upload_id]["chunks_uploaded"] = len(received_chunks)
                                upload_progress[upload_id]["progress"] = (len(received_chunks) / total_chunks) * 100
                                upload_progress[upload_id]["bytes_uploaded"] = len(received_chunks) * CHUNK_SIZE
                    
                    # 3. Subir chunks directamente al DataNode (solo los faltantes si se reanudó)
                    chunks_uploaded_count = 0
                    for i in chunks_to_upload:
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
                        
                        chunks_uploaded_count += 1
                        # Calcular progreso total (chunks recibidos previamente + chunks subidos ahora)
                        total_chunks_received = len(received_chunks) + chunks_uploaded_count
                        chunks_uploaded = total_chunks_received
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
            # Nota: file_id será None hasta que finalice el upload
            return jsonify({
                "success": True,
                "upload_id": upload_id,
                "message": f"Upload iniciado para '{filename}'",
                "total_chunks": total_chunks,
                "file_size": file_size,
                "file_id": None  # Se creará al finalizar cuando el archivo esté en el DataNode
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
    """Inicia descarga por chunks de un archivo."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        # 1. Iniciar descarga en NameNode (obtiene info del archivo y token)
        init_response = requests.post(
            f"{leader_url}/download/init",
            data={"filename": filename},
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if init_response.status_code != 200:
            error_msg = init_response.json().get("detail", "Error iniciando descarga") if init_response.status_code < 500 else "Error del servidor"
            return jsonify({"success": False, "error": error_msg}), init_response.status_code
        
        download_data = init_response.json()
        download_id = download_data["download_id"]
        file_id = download_data["file_id"]
        file_hash = download_data["file_hash"]
        file_size = download_data["file_size"]
        datanode_url = download_data["datanode_url"]
        client_token = download_data["client_token"]
        total_chunks = download_data["total_chunks"]
        chunk_size = download_data["chunk_size"]
        
        print(f"[FLASK] Descarga iniciada: download_id={download_id}, file_id={file_id}, chunks={total_chunks}")
        
        # Inicializar progreso
        with download_lock:
            download_progress[download_id] = {
                "filename": filename,
                "file_id": file_id,
                "file_hash": file_hash,
                "file_size": file_size,
                "total_chunks": total_chunks,
                "chunks_downloaded": 0,
                "progress": 0.0,
                "bytes_downloaded": 0,
                "status": "initializing"
            }
        
        # Función para ejecutar la descarga en un hilo separado
        def download_chunks_thread():
            try:
                # Verificar si hay chunks ya descargados (reanudación)
                chunks_dir = os.path.join(DOWNLOAD_DIR, f"{file_hash}_chunks")
                received_chunks = set()
                if os.path.exists(chunks_dir):
                    # Buscar chunks existentes
                    for chunk_file in os.listdir(chunks_dir):
                        if chunk_file.startswith("chunk_") and chunk_file.endswith(".tmp"):
                            try:
                                chunk_idx = int(chunk_file.replace("chunk_", "").replace(".tmp", ""))
                                received_chunks.add(chunk_idx)
                            except ValueError:
                                pass
                    
                    if received_chunks:
                        print(f"[FLASK] Reanudando descarga: {len(received_chunks)} chunks ya descargados")
                
                # Determinar qué chunks descargar
                if received_chunks:
                    chunks_to_download = [i for i in range(total_chunks) if i not in received_chunks]
                else:
                    chunks_to_download = list(range(total_chunks))
                    os.makedirs(chunks_dir, exist_ok=True)
                
                # Actualizar estado: empezando a descargar chunks
                with download_lock:
                    if download_id in download_progress:
                        download_progress[download_id]["status"] = "downloading_chunks"
                        if received_chunks:
                            download_progress[download_id]["chunks_downloaded"] = len(received_chunks)
                            download_progress[download_id]["progress"] = (len(received_chunks) / total_chunks) * 100
                            download_progress[download_id]["bytes_downloaded"] = len(received_chunks) * chunk_size
                
                # 2. Descargar chunks directamente del DataNode
                chunks_downloaded_count = 0
                for i in chunks_to_download:
                    chunk_path = os.path.join(chunks_dir, f"chunk_{i:06d}.tmp")
                    
                    # Descargar chunk
                    chunk_response = requests.get(
                        f"{datanode_url}/client/download/{file_hash}/chunk/{i}",
                        headers={"Authorization": f"Bearer {client_token}"},
                        timeout=120
                    )
                    
                    if chunk_response.status_code != 200:
                        try:
                            error_msg = chunk_response.json().get("detail", f"Error descargando chunk {i}")
                        except:
                            error_msg = f"Error HTTP {chunk_response.status_code} descargando chunk {i}"
                        with download_lock:
                            if download_id in download_progress:
                                download_progress[download_id]["status"] = "error"
                                download_progress[download_id]["error"] = error_msg
                        return
                    
                    # Guardar chunk en disco
                    with open(chunk_path, 'wb') as f:
                        f.write(chunk_response.content)
                    
                    chunks_downloaded_count += 1
                    total_chunks_received = len(received_chunks) + chunks_downloaded_count
                    progress = (total_chunks_received / total_chunks) * 100
                    bytes_downloaded = min(total_chunks_received * chunk_size, file_size)
                    
                    # Actualizar progreso
                    with download_lock:
                        if download_id in download_progress:
                            download_progress[download_id]["chunks_downloaded"] = total_chunks_received
                            download_progress[download_id]["progress"] = progress
                            download_progress[download_id]["bytes_downloaded"] = bytes_downloaded
                    
                    print(f"[FLASK] Progreso descarga: {total_chunks_received}/{total_chunks} chunks ({progress:.1f}%)")
                
                # Actualizar estado: chunks completados, ahora ensamblando
                with download_lock:
                    if download_id in download_progress:
                        download_progress[download_id]["status"] = "assembling"
                
                # 3. Ensamblar archivo desde chunks
                # Usar secure_filename para asegurar que el nombre sea seguro
                safe_filename = secure_filename(filename)
                final_file_path = os.path.join(DOWNLOAD_DIR, safe_filename)
                
                # Si el archivo ya existe, agregar un sufijo numérico
                if os.path.exists(final_file_path):
                    base_name, ext = os.path.splitext(safe_filename)
                    counter = 1
                    while os.path.exists(final_file_path):
                        final_file_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_{counter}{ext}")
                        counter += 1
                    safe_filename = os.path.basename(final_file_path)
                    print(f"[FLASK] Archivo ya existe, guardando como: {safe_filename}")
                
                print(f"[FLASK] Ensamblando archivo desde {total_chunks} chunks...")
                
                with open(final_file_path, 'wb') as final_file:
                    for i in range(total_chunks):
                        chunk_path = os.path.join(chunks_dir, f"chunk_{i:06d}.tmp")
                        if not os.path.exists(chunk_path):
                            error_msg = f"Chunk {i} no encontrado después de descarga"
                            with download_lock:
                                if download_id in download_progress:
                                    download_progress[download_id]["status"] = "error"
                                    download_progress[download_id]["error"] = error_msg
                            return
                        
                        with open(chunk_path, 'rb') as chunk_file:
                            final_file.write(chunk_file.read())
                
                # Verificar hash del archivo ensamblado
                with open(final_file_path, 'rb') as f:
                    file_content = f.read()
                    calculated_hash = hashlib.sha256(file_content).hexdigest()
                
                if calculated_hash != file_hash:
                    error_msg = f"Hash del archivo no coincide (esperado: {file_hash[:16]}..., calculado: {calculated_hash[:16]}...)"
                    with download_lock:
                        if download_id in download_progress:
                            download_progress[download_id]["status"] = "error"
                            download_progress[download_id]["error"] = error_msg
                    os.remove(final_file_path)
                    return
                
                # Limpiar chunks temporales
                try:
                    import shutil
                    shutil.rmtree(chunks_dir)
                    print(f"[FLASK] Chunks temporales eliminados: {chunks_dir}")
                except Exception as e:
                    print(f"[FLASK] Advertencia: No se pudieron eliminar chunks temporales: {e}")
                
                # Actualizar estado: completado
                with download_lock:
                    if download_id in download_progress:
                        download_progress[download_id]["status"] = "completed"
                        download_progress[download_id]["file_path"] = final_file_path
                        download_progress[download_id]["final_filename"] = safe_filename
                
                print(f"[FLASK] Archivo descargado y ensamblado: {final_file_path} ({len(file_content)} bytes)")
                
            except Exception as e:
                print(f"[FLASK] Error en hilo de descarga: {e}")
                import traceback
                traceback.print_exc()
                with download_lock:
                    if download_id in download_progress:
                        download_progress[download_id]["status"] = "error"
                        download_progress[download_id]["error"] = str(e)
        
        # Iniciar descarga en hilo separado
        download_thread = threading.Thread(target=download_chunks_thread, daemon=True)
        download_thread.start()
        
        # Retornar inmediatamente con download_id para que el frontend pueda consultar progreso
        return jsonify({
            "success": True,
            "download_id": download_id,
            "message": f"Descarga iniciada para '{filename}'",
            "total_chunks": total_chunks,
            "file_size": file_size
        })
            
    except Exception as e:
        print(f"[FLASK] Error iniciando descarga: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/download-progress/<download_id>", methods=["GET"])
def api_download_progress(download_id):
    """Consulta el progreso de una descarga en curso."""
    with download_lock:
        progress_data = download_progress.get(download_id)
    
    if not progress_data:
        return jsonify({"success": False, "error": "Descarga no encontrada"}), 404
    
    return jsonify({
        "success": True,
        "download_id": download_id,
        "filename": progress_data.get("filename"),
        "total_chunks": progress_data.get("total_chunks"),
        "chunks_downloaded": progress_data.get("chunks_downloaded", 0),
        "progress": progress_data.get("progress", 0.0),
        "bytes_downloaded": progress_data.get("bytes_downloaded", 0),
        "file_size": progress_data.get("file_size"),
        "status": progress_data.get("status"),
        "error": progress_data.get("error") if progress_data.get("status") == "error" else None,
        "file_path": progress_data.get("file_path") if progress_data.get("status") == "completed" else None,
        "final_filename": progress_data.get("final_filename") if progress_data.get("status") == "completed" else None
    })


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
