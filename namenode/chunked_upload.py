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
    Con persistencia de estado para recuperación después de reinicios
    """
    
    def __init__(self, node_id: str = None):
        self.sessions: Dict[str, UploadSession] = {}
        self.lock = threading.Lock()
        self._ensure_temp_dir()
        self._cleanup_old_sessions_thread = None
        self._running = False
        self.node_id = node_id
        self._operation_manager = None
    
    def _ensure_temp_dir(self):
        """Asegura que el directorio temporal existe"""
        os.makedirs(UPLOAD_TEMP_DIR, exist_ok=True)
    
    def set_operation_manager(self, manager):
        """Establece el manager de estado de operaciones"""
        self._operation_manager = manager
    
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
        
        # Persistir estado de la operación
        if self._operation_manager:
            self._operation_manager.create_operation(
                operation_id=upload_id,
                operation_type='upload',
                user_id=user_id,
                metadata={
                    'filename': filename,
                    'file_hash': file_hash,
                    'file_size': file_size,
                    'tags': tags,
                    'chunk_size': chunk_size,
                    'total_chunks': session.total_chunks
                },
                progress_data={
                    'total_chunks': session.total_chunks,
                    'uploaded_chunks': [],
                    'progress_percentage': 0.0
                }
            )
        
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
        
        # Actualizar progreso persistente (cada 5 chunks o siempre para chunks importantes)
        if self._operation_manager and (chunk_index % 5 == 0 or chunk_index == session.total_chunks - 1):
            uploaded_chunks = list(session.uploaded_chunks)
            self._operation_manager.update_progress(
                upload_id,
                {
                    'uploaded_chunks': uploaded_chunks,
                    'progress_percentage': session.progress_percentage,
                    'current_chunk': chunk_index,
                    'last_chunk_received_at': time.time()
                },
                force_log=(chunk_index == session.total_chunks - 1)  # Log al último chunk
            )
        
        # Actualizar estado si es el primer chunk o el último
        if self._operation_manager:
            if chunk_index == 0:
                self._operation_manager.update_operation_state(upload_id, 'in_progress')
            elif session.is_complete:
                self._operation_manager.update_operation_state(
                    upload_id,
                    'in_progress',  # Aún no completado, solo chunks recibidos
                    progress_data={
                        'all_chunks_received': True,
                        'ready_for_assembly': True
                    }
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
    
    def cleanup_session(self, upload_id: str, mark_completed: bool = True):
        """
        Elimina una sesión y sus archivos temporales
        
        Args:
            upload_id: ID de la sesión
            mark_completed: Si True, marca la operación como completada antes de limpiar
        """
        session = self.get_session(upload_id)
        if not session:
            return
        
        # Marcar como completada en el estado persistente
        if self._operation_manager and mark_completed:
            self._operation_manager.complete_operation(upload_id, success=True)
        
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
    
    def recover_session(self, upload_id: str) -> Optional[UploadSession]:
        """
        Recupera una sesión desde el estado persistente
        
        Args:
            upload_id: ID de la sesión a recuperar
        
        Returns:
            UploadSession recuperada o None si no existe o no se puede recuperar
        """
        if not self._operation_manager:
            return None
        
        # Obtener estado persistente
        op_state = self._operation_manager.get_operation(upload_id)
        if not op_state:
            return None
        
        # Verificar que los chunks aún existen en disco
        temp_dir = os.path.join(UPLOAD_TEMP_DIR, upload_id)
        if not os.path.exists(temp_dir):
            print(f"[CHUNKED_UPLOAD] ⚠️  No se puede recuperar {upload_id}: directorio temporal no existe")
            return None
        
        # Reconstruir sesión desde metadata
        metadata = op_state.metadata
        progress = op_state.progress_data
        
        session = UploadSession(
            upload_id=upload_id,
            filename=metadata.get('filename', 'unknown'),
            file_hash=metadata.get('file_hash', ''),
            file_size=metadata.get('file_size', 0),
            tags=metadata.get('tags', ''),
            chunk_size=metadata.get('chunk_size', 5 * 1024 * 1024),
            user_id=op_state.user_id or 'system',
            started_at=op_state.created_at
        )
        
        # Reconstruir información de chunks recibidos
        uploaded_chunks = progress.get('uploaded_chunks', [])
        for chunk_idx in uploaded_chunks:
            chunk_path = os.path.join(temp_dir, f"chunk_{chunk_idx:06d}")
            if os.path.exists(chunk_path):
                chunk_size = os.path.getsize(chunk_path)
                session.chunks[chunk_idx] = ChunkInfo(
                    index=chunk_idx,
                    hash='',  # Se recalculará si es necesario
                    size=chunk_size,
                    received=True,
                    path=chunk_path,
                    received_at=op_state.last_updated
                )
        
        # Agregar a sesiones activas
        with self.lock:
            self.sessions[upload_id] = session
        
        print(f"[CHUNKED_UPLOAD] ✅ Sesión recuperada: {upload_id} | {len(uploaded_chunks)}/{session.total_chunks} chunks")
        
        return session
    
    def recover_all_sessions(self) -> List[UploadSession]:
        """
        Recupera todas las sesiones incompletas desde el estado persistente
        
        Returns:
            Lista de UploadSession recuperadas
        """
        if not self._operation_manager:
            return []
        
        # Obtener todas las operaciones de upload incompletas
        incomplete_ops = self._operation_manager.get_operations_by_state(
            operation_type='upload',
            state=None  # Obtener todas
        )
        
        incomplete_ops = [op for op in incomplete_ops if op.state not in ('completed', 'failed')]
        
        recovered = []
        for op in incomplete_ops:
            session = self.recover_session(op.operation_id)
            if session:
                recovered.append(session)
        
        return recovered
    
    def cleanup_old_sessions(self, max_age_seconds: float = 86400, inactivity_timeout: float = 300.0):
        """
        Limpia sesiones antiguas que no se han completado o están inactivas
        
        Args:
            max_age_seconds: Edad máxima en segundos (default: 24 horas)
            inactivity_timeout: Tiempo sin actividad para considerar huérfana (default: 5 minutos)
        """
        current_time = time.time()
        to_cleanup = []
        
        with self.lock:
            for upload_id, session in self.sessions.items():
                age = current_time - session.started_at
                
                # Calcular tiempo desde último chunk recibido
                last_chunk_time = max(
                    (chunk.received_at for chunk in session.chunks.values() if chunk.received_at),
                    default=session.started_at
                )
                inactivity = current_time - last_chunk_time
                
                # Limpiar si:
                # 1. Es muy antigua (más de max_age_seconds)
                # 2. Está inactiva por más de inactivity_timeout y no está completa
                if age > max_age_seconds:
                    to_cleanup.append((upload_id, "antigua"))
                elif inactivity > inactivity_timeout and not session.is_complete:
                    to_cleanup.append((upload_id, "inactiva"))
        
        if to_cleanup:
            print(f"[CHUNKED_UPLOAD] 🧹 Limpiando {len(to_cleanup)} sesiones huérfanas...")
            for upload_id, reason in to_cleanup:
                print(f"  - {upload_id[:16]}... ({reason})")
                self.cleanup_session(upload_id, mark_completed=False)
    
    def get_all_sessions(self) -> List[dict]:
        """Obtiene información de todas las sesiones activas"""
        with self.lock:
            return [session.to_dict() for session in self.sessions.values()]


# Instancia global del manager
chunked_upload_manager = ChunkedUploadManager()

