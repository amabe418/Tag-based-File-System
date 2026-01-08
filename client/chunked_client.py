"""
Cliente Python con soporte para chunked upload
Permite subir archivos grandes dividiéndolos en bloques
"""
import os
import sys
import hashlib
import requests
from typing import Optional, List
import time

# Tamaño de chunk por defecto: 10 MB
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024

class ChunkedUploadClient:
    """Cliente para upload de archivos por chunks"""
    
    def __init__(self, server_url: str, auth_token: Optional[str] = None):
        """
        Inicializa el cliente
        
        Args:
            server_url: URL del namenode (ej: http://localhost:8010)
            auth_token: Token JWT de autenticación (opcional)
        """
        self.server_url = server_url.rstrip("/")
        self.auth_token = auth_token
        self.session = requests.Session()
        if auth_token:
            self.session.headers["Authorization"] = f"Bearer {auth_token}"
    
    def login(self, username: str, password: str) -> bool:
        """
        Inicia sesión y obtiene token de autenticación
        
        Args:
            username: Nombre de usuario
            password: Contraseña
        
        Returns:
            True si el login fue exitoso
        """
        try:
            response = requests.post(
                f"{self.server_url}/auth/login",
                json={"username": username, "password": password},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            self.auth_token = data["access_token"]
            self.session.headers["Authorization"] = f"Bearer {self.auth_token}"
            print(f"✅ Login exitoso: {username}")
            return True
        except Exception as e:
            print(f"❌ Error en login: {e}")
            return False
    
    def calculate_file_hash(self, file_path: str) -> str:
        """
        Calcula el hash SHA-256 de un archivo
        
        Args:
            file_path: Ruta del archivo
        
        Returns:
            Hash en formato hexadecimal
        """
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def upload_file_chunked(
        self,
        file_path: str,
        tags: List[str],
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        resume: bool = True
    ) -> bool:
        """
        Sube un archivo usando chunked upload
        
        Args:
            file_path: Ruta del archivo a subir
            tags: Lista de etiquetas
            chunk_size: Tamaño de cada chunk en bytes
            resume: Si es True, intenta reanudar uploads interrumpidos
        
        Returns:
            True si el upload fue exitoso
        """
        if not os.path.exists(file_path):
            print(f"❌ Archivo no encontrado: {file_path}")
            return False
        
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        
        print(f"📤 Subiendo: {filename} ({file_size:,} bytes)")
        print(f"📦 Tamaño de chunk: {chunk_size:,} bytes")
        
        # Calcular hash del archivo
        print("🔐 Calculando hash del archivo...")
        file_hash = f"sha256:{self.calculate_file_hash(file_path)}"
        print(f"🔐 Hash: {file_hash[:40]}...")
        
        # Iniciar sesión de upload
        print("🚀 Iniciando sesión de upload...")
        try:
            response = self.session.post(
                f"{self.server_url}/upload/init",
                data={
                    "filename": filename,
                    "file_hash": file_hash,
                    "file_size": file_size,
                    "tags": ",".join(tags),
                    "chunk_size": chunk_size
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            upload_id = data["upload_id"]
            total_chunks = data["total_chunks"]
            print(f"✅ Sesión creada: {upload_id}")
            print(f"📊 Total de chunks: {total_chunks}")
        except Exception as e:
            print(f"❌ Error iniciando upload: {e}")
            return False
        
        # Verificar qué chunks ya están subidos (para resumir)
        uploaded_chunks = set()
        if resume:
            try:
                response = self.session.get(
                    f"{self.server_url}/upload/{upload_id}/status",
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                uploaded_chunks = set(data["uploaded_chunks"])
                if uploaded_chunks:
                    print(f"🔄 Reanudando: {len(uploaded_chunks)}/{total_chunks} chunks ya subidos")
            except Exception:
                pass  # Si falla, simplemente empezar desde el principio
        
        # Subir chunks
        start_time = time.time()
        last_progress_time = start_time
        
        with open(file_path, 'rb') as f:
            for chunk_index in range(total_chunks):
                # Saltar chunks ya subidos
                if chunk_index in uploaded_chunks:
                    f.seek((chunk_index + 1) * chunk_size)
                    continue
                
                # Leer chunk
                f.seek(chunk_index * chunk_size)
                chunk_data = f.read(chunk_size)
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                
                # Subir chunk con reintentos
                max_retries = 3
                for retry in range(max_retries):
                    try:
                        response = self.session.post(
                            f"{self.server_url}/upload/{upload_id}/chunk/{chunk_index}",
                            files={"chunk": (f"chunk_{chunk_index}", chunk_data)},
                            data={"chunk_hash": chunk_hash},
                            timeout=120
                        )
                        response.raise_for_status()
                        
                        # Progreso
                        progress_pct = ((chunk_index + 1) / total_chunks) * 100
                        
                        # Mostrar progreso cada segundo o al completar
                        current_time = time.time()
                        if current_time - last_progress_time >= 1.0 or chunk_index == total_chunks - 1:
                            elapsed = current_time - start_time
                            speed = ((chunk_index + 1) * chunk_size) / elapsed / (1024 * 1024)  # MB/s
                            print(f"📤 Progreso: {chunk_index + 1}/{total_chunks} ({progress_pct:.1f}%) - {speed:.2f} MB/s")
                            last_progress_time = current_time
                        
                        break  # Chunk subido exitosamente
                    
                    except Exception as e:
                        if retry < max_retries - 1:
                            wait_time = 2 ** retry  # Backoff exponencial
                            print(f"⚠️  Error en chunk {chunk_index + 1}, reintentando en {wait_time}s... ({retry + 1}/{max_retries})")
                            time.sleep(wait_time)
                        else:
                            print(f"❌ Error definitivo en chunk {chunk_index + 1}: {e}")
                            print(f"💡 Puedes reanudar el upload ejecutando el comando nuevamente")
                            return False
        
        # Finalizar upload
        print("🔧 Ensamblando archivo y enviando a DataNodes...")
        try:
            response = self.session.post(
                f"{self.server_url}/upload/{upload_id}/finalize",
                timeout=300  # 5 minutos para ensamblado y envío
            )
            response.raise_for_status()
            data = response.json()
            
            elapsed_time = time.time() - start_time
            avg_speed = file_size / elapsed_time / (1024 * 1024)  # MB/s
            
            print(f"✅ ¡Upload completado!")
            print(f"📁 Archivo: {filename}")
            print(f"🔢 File ID: {data['file_id']}")
            print(f"🔐 Hash: {data['file_hash'][:40]}...")
            print(f"💾 Réplicas: {data['replicas_stored']}/{len(data['replicas'])}")
            print(f"⏱️  Tiempo total: {elapsed_time:.1f}s ({avg_speed:.2f} MB/s promedio)")
            
            return True
            
        except Exception as e:
            print(f"❌ Error finalizando upload: {e}")
            print(f"💡 El archivo se subió por chunks pero falló el ensamblado")
            return False
    
    def cancel_upload(self, upload_id: str) -> bool:
        """
        Cancela un upload en progreso
        
        Args:
            upload_id: ID del upload a cancelar
        
        Returns:
            True si se canceló exitosamente
        """
        try:
            response = self.session.delete(
                f"{self.server_url}/upload/{upload_id}",
                timeout=10
            )
            response.raise_for_status()
            print(f"✅ Upload cancelado: {upload_id}")
            return True
        except Exception as e:
            print(f"❌ Error cancelando upload: {e}")
            return False
    
    def list_active_uploads(self):
        """Lista uploads activos del usuario"""
        try:
            response = self.session.get(
                f"{self.server_url}/uploads/active",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            uploads = data["active_uploads"]
            if not uploads:
                print("📭 No hay uploads activos")
                return
            
            print(f"📋 Uploads activos ({len(uploads)}):")
            for upload in uploads:
                progress = upload["progress_percentage"]
                uploaded = len(upload["uploaded_chunks"])
                total = upload["total_chunks"]
                filename = upload["filename"]
                upload_id = upload["upload_id"]
                
                print(f"  • {filename}")
                print(f"    ID: {upload_id}")
                print(f"    Progreso: {uploaded}/{total} chunks ({progress:.1f}%)")
                print()
            
        except Exception as e:
            print(f"❌ Error listando uploads: {e}")
    
    def upload_file_legacy(self, file_path: str, tags: List[str]) -> bool:
        """
        Sube un archivo usando el método tradicional (sin chunks)
        Para archivos pequeños o compatibilidad con versiones antiguas
        
        Args:
            file_path: Ruta del archivo a subir
            tags: Lista de etiquetas
        
        Returns:
            True si el upload fue exitoso
        """
        if not os.path.exists(file_path):
            print(f"❌ Archivo no encontrado: {file_path}")
            return False
        
        filename = os.path.basename(file_path)
        
        print(f"📤 Subiendo (método legacy): {filename}")
        
        try:
            with open(file_path, 'rb') as f:
                response = self.session.post(
                    f"{self.server_url}/add",
                    files={"file": (filename, f)},
                    data={"tags": ",".join(tags)},
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()
                
                print(f"✅ Upload exitoso!")
                print(f"📁 File ID: {data['file_id']}")
                print(f"💾 Réplicas: {data['replicas_stored']}/{len(data['replicas'])}")
                
                return True
        except Exception as e:
            print(f"❌ Error subiendo archivo: {e}")
            return False


def main():
    """Función principal del cliente"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Cliente de chunked upload para TBFS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  # Login
  python chunked_client.py login --username admin --password admin123

  # Subir archivo con chunked upload
  python chunked_client.py upload archivo.mp4 --tags video,vacaciones --chunk-size 5242880

  # Subir archivo pequeño con método legacy
  python chunked_client.py upload-legacy documento.pdf --tags trabajo,importante

  # Listar uploads activos
  python chunked_client.py list-uploads

  # Cancelar upload
  python chunked_client.py cancel-upload <upload_id>
        """
    )
    
    parser.add_argument("--server", default="http://localhost:8010", help="URL del namenode")
    parser.add_argument("--token", help="Token JWT de autenticación")
    
    subparsers = parser.add_subparsers(dest="command", help="Comando a ejecutar")
    
    # Login
    login_parser = subparsers.add_parser("login", help="Iniciar sesión")
    login_parser.add_argument("--username", required=True, help="Nombre de usuario")
    login_parser.add_argument("--password", required=True, help="Contraseña")
    
    # Upload chunked
    upload_parser = subparsers.add_parser("upload", help="Subir archivo con chunked upload")
    upload_parser.add_argument("file", help="Ruta del archivo a subir")
    upload_parser.add_argument("--tags", required=True, help="Etiquetas separadas por comas")
    upload_parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, 
                              help="Tamaño de chunk en bytes (default: 10MB)")
    upload_parser.add_argument("--no-resume", action="store_true", 
                              help="No reanudar uploads interrumpidos")
    
    # Upload legacy
    upload_legacy_parser = subparsers.add_parser("upload-legacy", 
                                                  help="Subir archivo con método tradicional")
    upload_legacy_parser.add_argument("file", help="Ruta del archivo a subir")
    upload_legacy_parser.add_argument("--tags", required=True, help="Etiquetas separadas por comas")
    
    # List uploads
    subparsers.add_parser("list-uploads", help="Listar uploads activos")
    
    # Cancel upload
    cancel_parser = subparsers.add_parser("cancel-upload", help="Cancelar upload")
    cancel_parser.add_argument("upload_id", help="ID del upload a cancelar")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Crear cliente
    client = ChunkedUploadClient(args.server, args.token)
    
    # Ejecutar comando
    if args.command == "login":
        client.login(args.username, args.password)
        if client.auth_token:
            print(f"\n🔑 Token: {client.auth_token}")
            print("\n💡 Guarda este token para futuras operaciones:")
            print(f"   export TBFS_TOKEN='{client.auth_token}'")
            print(f"   python chunked_client.py --token $TBFS_TOKEN upload archivo.mp4 --tags video")
    
    elif args.command == "upload":
        if not client.auth_token:
            print("❌ Se requiere autenticación. Usa --token o ejecuta 'login' primero")
            return
        
        tags = [t.strip() for t in args.tags.split(",")]
        client.upload_file_chunked(
            args.file,
            tags,
            chunk_size=args.chunk_size,
            resume=not args.no_resume
        )
    
    elif args.command == "upload-legacy":
        if not client.auth_token:
            print("❌ Se requiere autenticación. Usa --token o ejecuta 'login' primero")
            return
        
        tags = [t.strip() for t in args.tags.split(",")]
        client.upload_file_legacy(args.file, tags)
    
    elif args.command == "list-uploads":
        if not client.auth_token:
            print("❌ Se requiere autenticación. Usa --token o ejecuta 'login' primero")
            return
        
        client.list_active_uploads()
    
    elif args.command == "cancel-upload":
        if not client.auth_token:
            print("❌ Se requiere autenticación. Usa --token o ejecuta 'login' primero")
            return
        
        client.cancel_upload(args.upload_id)


if __name__ == "__main__":
    main()

