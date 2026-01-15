"""
Interfaz web Flask para el sistema de archivos basado en etiquetas.
Sin límites de tamaño de archivo, descarga directa nativa.
"""
import os
import hashlib
import requests
from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
from werkzeug.utils import secure_filename
from namenode_client import namenode_client
import io

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tbfs-secret-key-change-me")

# Configuración
NAMENODE_PORT = int(os.getenv("NAMENODE_PORT", "8010"))
CHUNK_SIZE = 5 * 1024 * 1024  # 5MB


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
        else:
            # Subida por chunks
            file_hash = f"sha256:{hashlib.sha256(file_content).hexdigest()}"
            total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
            
            # 1. Iniciar sesión de upload
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
                return jsonify({"success": False, "error": "Error iniciando upload"}), 500
            
            upload_data = init_response.json()
            upload_id = upload_data["upload_id"]
            
            # 2. Subir chunks
            for i in range(total_chunks):
                start = i * CHUNK_SIZE
                end = min(start + CHUNK_SIZE, file_size)
                chunk_data = file_content[start:end]
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                
                chunk_response = requests.post(
                    f"{leader_url}/upload/{upload_id}/chunk/{i}",
                    files={"chunk": (f"chunk_{i}", chunk_data)},
                    data={"chunk_hash": chunk_hash},
                    headers=get_auth_headers(token),
                    timeout=120
                )
                
                if chunk_response.status_code != 200:
                    return jsonify({"success": False, "error": f"Error en chunk {i}"}), 500
            
            # 3. Finalizar
            response = requests.post(
                f"{leader_url}/upload/{upload_id}/finalize",
                headers=get_auth_headers(token),
                timeout=300
            )
        
        if response.status_code == 200:
            data = response.json()
            return jsonify({
                "success": True,
                "file_id": data.get("file_id"),
                "message": f"Archivo '{filename}' subido correctamente"
            })
        else:
            # Intentar obtener el mensaje de error
            try:
                error_data = response.json()
                error_msg = error_data.get("detail", error_data.get("error", f"Error HTTP {response.status_code}"))
            except:
                error_msg = f"Error HTTP {response.status_code}: {response.text[:200]}"
            print(f"[FLASK] Error del servidor: {error_msg}")
            return jsonify({"success": False, "error": str(error_msg)}), response.status_code
            
    except Exception as e:
        print(f"[FLASK] Error subiendo archivo: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


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


@app.route("/api/delete/<filename>", methods=["DELETE"])
def api_delete(filename):
    """Elimina un archivo."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        return jsonify({"success": False, "error": f"No hay líder disponible: {error}"}), 503
    
    try:
        response = requests.delete(
            f"{leader_url}/delete/{filename}",
            headers=get_auth_headers(token),
            timeout=30
        )
        
        if response.status_code == 200:
            return jsonify({"success": True, "message": f"Archivo '{filename}' eliminado"})
        else:
            error_msg = response.json().get("detail", "Error al eliminar")
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
