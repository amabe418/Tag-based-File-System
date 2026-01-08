"""
Módulo de gestión de uploads por chunks
Maneja el estado de uploads en progreso y ensamblado de archivos
"""
import os
import hashlib
import shutil
import time
import threading
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path
import json

# Directorio temporal para uploads en progreso
UPLOAD_TEMP_DIR = os.getenv("UPLOAD_TEMP_DIR", "/tmp/tbfs_uploads")

@dataclass
class ChunkInfo:
    """Información de un chunk individual"""
    index: int
    hash: str
    size: int
    received: bool = False
    path: Optional[str] = None
    received_at: Optional[float] = None

@dataclass
class UploadSession:
    """Sesión de upload por chunks"""
    upload_id: str
    filename: str
    file_hash: str
    file_size: int
    tags: str
    chunk_size: int
    user_id: str
    started_at: float = field(default_factory=time.time)
    chunks: Dict[int, ChunkInfo] = field(default_factory=dict)
    
    @property
    def total_chunks(self) -> int:
        """Calcula el número total de chunks"""
        return (self.file_size + self.chunk_size - 1) // self.chunk_size
    
    @property
    def uploaded_chunks(self) -> Set[int]:
        """Retorna índices de chunks ya subidos"""
        return {idx for idx, chunk in self.chunks.items() if chunk.received}
    
    @property
    def progress_percentage(self) -> float:
        """Calcula el progreso en porcentaje"""
        if self.total_chunks == 0:
            return 0.0
        return (len(self.uploaded_chunks) / self.total_chunks) * 100
    
    @property
    def is_complete(self) -> bool:
        """Verifica si todos los chunks están subidos"""
        return len(self.uploaded_chunks) == self.total_chunks
    
    def get_temp_dir(self) -> str:
        """Obtiene el directorio temporal para este upload"""
        return os.path.join(UPLOAD_TEMP_DIR, self.upload_id)
    
    def get_assembled_path(self) -> str:
        """Obtiene la ruta del archivo ensamblado"""
        return os.path.join(self.get_temp_dir(), "assembled")
    
    def to_dict(self) -> dict:
        """Convierte a diccionario para serialización"""
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "file_hash": self.file_hash,
            "file_size": self.file_size,
            "tags": self.tags,
            "chunk_size": self.chunk_size,
            "user_id": self.user_id,
            "started_at": self.started_at,
            "total_chunks": self.total_chunks,
            "uploaded_chunks": list(self.uploaded_chunks),
            "progress_percentage": self.progress_percentage,
            "is_complete": self.is_complete
        }


class ChunkedUploadManager:
    """
    Gestiona sesiones de upload por chunks
    Thread-safe para operaciones concurrentes
    """
    
    def __init__(self):
        self.sessions: Dict[str, UploadSession] = {}
        self.lock = threading.Lock()
        self._ensure_temp_dir()
        self._cleanup_old_sessions_thread = None
        self._running = False
    
    def _ensure_temp_dir(self):
        """Asegura que el directorio temporal existe"""
        os.makedirs(UPLOAD_TEMP_DIR, exist_ok=True)
    
    def start_cleanup_thread(self, max_age_hours: int = 24):
        """Inicia hilo de limpieza de sesiones antiguas"""
        if self._running:
            return
        
        self._running = True
        
        def cleanup_loop():
            while self._running:
                try:
                    self.cleanup_old_sessions(max_age_hours * 3600)
                except Exception as e:
                    print(f"[CHUNKED_UPLOAD] Error en cleanup: {e}")
                
                # Dormir 1 hora
                for _ in range(3600):
                    if not self._running:
                        break
                    time.sleep(1)
        
        self._cleanup_old_sessions_thread = threading.Thread(
            target=cleanup_loop,
            daemon=True,
            name="chunked-upload-cleanup"
        )
        self._cleanup_old_sessions_thread.start()
        print(f"[CHUNKED_UPLOAD] Hilo de limpieza iniciado (max_age={max_age_hours}h)")
    
    def stop_cleanup_thread(self):
        """Detiene hilo de limpieza"""
        self._running = False
        if self._cleanup_old_sessions_thread:
            self._cleanup_old_sessions_thread.join(timeout=2)
    
    def create_session(
        self,
        upload_id: str,
        filename: str,
        file_hash: str,
        file_size: int,
        tags: str,
        chunk_size: int,
        user_id: str
    ) -> UploadSession:
        """Crea una nueva sesión de upload"""
        session = UploadSession(
            upload_id=upload_id,
            filename=filename,
            file_hash=file_hash,
            file_size=file_size,
            tags=tags,
            chunk_size=chunk_size,
            user_id=user_id
        )
        
        with self.lock:
            self.sessions[upload_id] = session
        
        # Crear directorio temporal
        os.makedirs(session.get_temp_dir(), exist_ok=True)
        
        print(f"[CHUNKED_UPLOAD] Sesión creada: {upload_id} - {filename} ({session.total_chunks} chunks)")
        
        return session
    
    def get_session(self, upload_id: str) -> Optional[UploadSession]:
        """Obtiene una sesión de upload"""
        with self.lock:
            return self.sessions.get(upload_id)
    
    def save_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        chunk_data: bytes,
        chunk_hash: str
    ) -> bool:
        """
        Guarda un chunk en disco
        
        Returns:
            True si se guardó correctamente, False en caso contrario
        """
        session = self.get_session(upload_id)
        if not session:
            print(f"[CHUNKED_UPLOAD] Sesión no encontrada: {upload_id}")
            return False
        
        # Verificar hash
        calculated_hash = hashlib.sha256(chunk_data).hexdigest()
        if calculated_hash != chunk_hash:
            print(f"[CHUNKED_UPLOAD] Hash inválido para chunk {chunk_index} de {upload_id}")
            return False
        
        # Guardar en disco
        chunk_path = os.path.join(session.get_temp_dir(), f"chunk_{chunk_index:06d}")
        
        try:
            with open(chunk_path, 'wb') as f:
                f.write(chunk_data)
        except Exception as e:
            print(f"[CHUNKED_UPLOAD] Error guardando chunk {chunk_index}: {e}")
            return False
        
        # Actualizar estado
        with self.lock:
            session.chunks[chunk_index] = ChunkInfo(
                index=chunk_index,
                hash=chunk_hash,
                size=len(chunk_data),
                received=True,
                path=chunk_path,
                received_at=time.time()
            )
        
        print(f"[CHUNKED_UPLOAD] Chunk {chunk_index + 1}/{session.total_chunks} guardado para {upload_id} ({session.progress_percentage:.1f}%)")
        
        return True
    
    def assemble_file(self, upload_id: str) -> Optional[bytes]:
        """
        Ensambla todos los chunks en un archivo completo
        
        Returns:
            Contenido del archivo si se ensambló correctamente, None en caso contrario
        """
        session = self.get_session(upload_id)
        if not session:
            print(f"[CHUNKED_UPLOAD] Sesión no encontrada: {upload_id}")
            return None
        
        if not session.is_complete:
            missing = session.total_chunks - len(session.uploaded_chunks)
            print(f"[CHUNKED_UPLOAD] Upload incompleto: faltan {missing} chunks")
            return None
        
        assembled_path = session.get_assembled_path()
        
        print(f"[CHUNKED_UPLOAD] Ensamblando {session.total_chunks} chunks para {session.filename}...")
        
        try:
            # Ensamblar archivo
            with open(assembled_path, 'wb') as outfile:
                for chunk_index in range(session.total_chunks):
                    chunk_info = session.chunks[chunk_index]
                    with open(chunk_info.path, 'rb') as infile:
                        outfile.write(infile.read())
            
            # Leer archivo completo
            with open(assembled_path, 'rb') as f:
                file_content = f.read()
            
            # Verificar hash del archivo completo
            calculated_hash = hashlib.sha256(file_content).hexdigest()
            expected_hash = session.file_hash.replace("sha256:", "")
            
            if calculated_hash != expected_hash:
                print(f"[CHUNKED_UPLOAD] ❌ Hash del archivo no coincide")
                print(f"  Esperado: {expected_hash}")
                print(f"  Calculado: {calculated_hash}")
                return None
            
            print(f"[CHUNKED_UPLOAD] ✅ Archivo ensamblado y verificado: {session.filename} ({len(file_content)} bytes)")
            
            return file_content
            
        except Exception as e:
            print(f"[CHUNKED_UPLOAD] Error ensamblando archivo: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def cleanup_session(self, upload_id: str):
        """Elimina una sesión y sus archivos temporales"""
        session = self.get_session(upload_id)
        if not session:
            return
        
        # Eliminar directorio temporal
        temp_dir = session.get_temp_dir()
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"[CHUNKED_UPLOAD] Directorio temporal eliminado: {temp_dir}")
            except Exception as e:
                print(f"[CHUNKED_UPLOAD] Error eliminando directorio temporal: {e}")
        
        # Eliminar de sesiones activas
        with self.lock:
            if upload_id in self.sessions:
                del self.sessions[upload_id]
        
        print(f"[CHUNKED_UPLOAD] Sesión limpiada: {upload_id}")
    
    def cleanup_old_sessions(self, max_age_seconds: float = 86400):
        """
        Limpia sesiones antiguas que no se han completado
        
        Args:
            max_age_seconds: Edad máxima en segundos (default: 24 horas)
        """
        current_time = time.time()
        to_cleanup = []
        
        with self.lock:
            for upload_id, session in self.sessions.items():
                age = current_time - session.started_at
                if age > max_age_seconds:
                    to_cleanup.append(upload_id)
        
        if to_cleanup:
            print(f"[CHUNKED_UPLOAD] Limpiando {len(to_cleanup)} sesiones antiguas...")
            for upload_id in to_cleanup:
                self.cleanup_session(upload_id)
    
    def get_all_sessions(self) -> List[dict]:
        """Obtiene información de todas las sesiones activas"""
        with self.lock:
            return [session.to_dict() for session in self.sessions.values()]


# Instancia global del manager
chunked_upload_manager = ChunkedUploadManager()

