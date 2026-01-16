"""
Módulo de gestión de almacenamiento por chunks en DataNode
Permite recibir archivos grandes por bloques desde el NameNode
"""
import os
import hashlib
import shutil
import threading
from typing import Dict, Optional, Set
from dataclasses import dataclass, field
import time

# Directorio temporal para archivos en proceso de recepción
TEMP_STORAGE_DIR = os.getenv("TEMP_STORAGE_DIR", "/tmp/datanode_chunks")

@dataclass
class ChunkReceiveInfo:
    """Información de un chunk recibido"""
    index: int
    hash: str
    size: int
    path: str
    received_at: float = field(default_factory=time.time)

@dataclass
class FileReceiveSession:
    """Sesión de recepción de archivo por chunks"""
    session_id: str
    file_id: str  # Hash del archivo
    total_chunks: int
    chunk_size: int
    expected_file_size: int
    chunks: Dict[int, ChunkReceiveInfo] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    
    @property
    def received_chunks(self) -> Set[int]:
        """Retorna índices de chunks ya recibidos"""
        return set(self.chunks.keys())
    
    @property
    def is_complete(self) -> bool:
        """Verifica si todos los chunks están recibidos"""
        return len(self.received_chunks) == self.total_chunks
    
    @property
    def progress_percentage(self) -> float:
        """Calcula el progreso en porcentaje"""
        if self.total_chunks == 0:
            return 0.0
        return (len(self.received_chunks) / self.total_chunks) * 100
    
    def get_temp_dir(self) -> str:
        """Obtiene el directorio temporal para esta sesión"""
        return os.path.join(TEMP_STORAGE_DIR, self.session_id)
    
    def get_assembled_path(self) -> str:
        """Obtiene la ruta del archivo ensamblado"""
        return os.path.join(self.get_temp_dir(), "assembled")


class ChunkedStorageManager:
    """
    Gestiona la recepción de archivos por chunks en el DataNode
    Thread-safe para operaciones concurrentes
    """
    
    def __init__(self):
        self.sessions: Dict[str, FileReceiveSession] = {}
        self.lock = threading.Lock()
        self._ensure_temp_dir()
        self._cleanup_thread = None
        self._running = False
    
    def _ensure_temp_dir(self):
        """Asegura que el directorio temporal existe"""
        os.makedirs(TEMP_STORAGE_DIR, exist_ok=True)
    
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
                    print(f"[CHUNKED_STORAGE] Error en cleanup: {e}")
                
                # Dormir 1 hora
                for _ in range(3600):
                    if not self._running:
                        break
                    time.sleep(1)
        
        self._cleanup_thread = threading.Thread(
            target=cleanup_loop,
            daemon=True,
            name="chunked-storage-cleanup"
        )
        self._cleanup_thread.start()
        print(f"[CHUNKED_STORAGE] Hilo de limpieza iniciado (max_age={max_age_hours}h)")
    
    def stop_cleanup_thread(self):
        """Detiene hilo de limpieza"""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2)
    
    def create_session(
        self,
        session_id: str,
        file_id: str,
        total_chunks: int,
        chunk_size: int,
        expected_file_size: int
    ) -> FileReceiveSession:
        """Crea una nueva sesión de recepción"""
        session = FileReceiveSession(
            session_id=session_id,
            file_id=file_id,
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            expected_file_size=expected_file_size
        )
        
        with self.lock:
            self.sessions[session_id] = session
        
        # Crear directorio temporal
        os.makedirs(session.get_temp_dir(), exist_ok=True)
        
        file_size_mb = expected_file_size / (1024 * 1024)
        chunk_size_mb = chunk_size / (1024 * 1024)
        print(f"[CHUNKED_STORAGE] 📦 Sesión creada | session_id={session_id[:8]} | file_id={file_id[:16]}... | archivo={file_size_mb:.2f} MB | chunks={total_chunks} | chunk_size={chunk_size_mb:.2f} MB")
        
        return session
    
    def get_session(self, session_id: str) -> Optional[FileReceiveSession]:
        """Obtiene una sesión de recepción"""
        with self.lock:
            return self.sessions.get(session_id)
    
    def find_session_by_file_id(self, file_id: str) -> Optional[FileReceiveSession]:
        """Busca una sesión activa por file_id (hash del archivo)"""
        with self.lock:
            for session in self.sessions.values():
                if session.file_id == file_id and not session.is_complete:
                    return session
        return None
    
    def get_session_progress(self, file_id: str) -> Optional[dict]:
        """Obtiene el progreso de una sesión por file_id"""
        session = self.find_session_by_file_id(file_id)
        if not session:
            return None
        
        return {
            "session_id": session.session_id,
            "file_id": session.file_id,
            "total_chunks": session.total_chunks,
            "received_chunks": sorted(list(session.received_chunks)),
            "missing_chunks": sorted(list(set(range(session.total_chunks)) - session.received_chunks)),
            "progress_percentage": session.progress_percentage,
            "is_complete": session.is_complete
        }
    
    def save_chunk(
        self,
        session_id: str,
        chunk_index: int,
        chunk_data: bytes,
        chunk_hash: str
    ) -> bool:
        """
        Guarda un chunk en disco
        
        Returns:
            True si se guardó correctamente, False en caso contrario
        """
        session = self.get_session(session_id)
        if not session:
            print(f"[CHUNKED_STORAGE] Sesión no encontrada: {session_id}")
            return False
        
        # Verificar hash
        calculated_hash = hashlib.sha256(chunk_data).hexdigest()
        if calculated_hash != chunk_hash:
            print(f"[CHUNKED_STORAGE] Hash inválido para chunk {chunk_index} de {session_id}")
            return False
        
        # Guardar en disco
        chunk_path = os.path.join(session.get_temp_dir(), f"chunk_{chunk_index:06d}")
        
        try:
            with open(chunk_path, 'wb') as f:
                f.write(chunk_data)
        except Exception as e:
            print(f"[CHUNKED_STORAGE] Error guardando chunk {chunk_index}: {e}")
            return False
        
        # Actualizar estado
        with self.lock:
            session.chunks[chunk_index] = ChunkReceiveInfo(
                index=chunk_index,
                hash=chunk_hash,
                size=len(chunk_data),
                path=chunk_path
            )
        
        chunk_size_mb = len(chunk_data) / (1024 * 1024)
        print(f"[CHUNKED_STORAGE] ✅ Chunk {chunk_index + 1}/{session.total_chunks} recibido | file_id={session.file_id[:16]}... | tamaño={chunk_size_mb:.2f} MB | progreso={session.progress_percentage:.1f}% | session={session_id[:8]}")
        
        return True
    
    def assemble_and_store(self, session_id: str) -> bool:
        """
        Mueve los chunks directamente al almacenamiento final sin ensamblar.
        Los chunks se mantienen separados para evitar tener que particionar
        cada vez que se envía el archivo.
        
        Returns:
            True si se guardaron correctamente, False en caso contrario
        """
        session = self.get_session(session_id)
        if not session:
            print(f"[CHUNKED_STORAGE] Sesión no encontrada: {session_id}")
            return False
        
        if not session.is_complete:
            missing = session.total_chunks - len(session.received_chunks)
            received = sorted(session.received_chunks)
            print(f"[CHUNKED_STORAGE] ❌ Sesión incompleta: faltan {missing} chunks de {session.total_chunks}")
            print(f"[CHUNKED_STORAGE] Chunks recibidos: {received}")
            print(f"[CHUNKED_STORAGE] Chunks esperados: {list(range(session.total_chunks))}")
            return False
        
        file_size_mb = sum(chunk_info.size for chunk_info in session.chunks.values()) / (1024 * 1024)
        print(f"[CHUNKED_STORAGE] 🔄 Moviendo {session.total_chunks} chunks ({file_size_mb:.2f} MB total) a almacenamiento final | file_id={session.file_id[:16]}...")
        
        try:
            from datanode.storage import store_chunks
            
            # Preparar información de chunks para guardar
            chunks_info = {}
            for chunk_index in range(session.total_chunks):
                chunk_info = session.chunks[chunk_index]
                
                # Leer chunk desde ubicación temporal
                with open(chunk_info.path, 'rb') as f:
                    chunk_data = f.read()
                
                chunks_info[chunk_index] = (chunk_data, chunk_info.hash)
            
            # Guardar chunks directamente en storage
            if store_chunks(session.file_id, chunks_info):
                # Verificar integridad: ensamblar temporalmente para verificar hash
                # Ordenar por chunk_index (clave) para asegurar el orden correcto
                assembled_content = b''.join([chunks_info[i][0] for i in sorted(chunks_info.keys())])
                calculated_hash = hashlib.sha256(assembled_content).hexdigest()
                
                if calculated_hash != session.file_id:
                    print(f"[CHUNKED_STORAGE] ❌ Hash del archivo no coincide después de guardar chunks")
                    print(f"  Esperado: {session.file_id}")
                    print(f"  Calculado: {calculated_hash}")
                    # Eliminar chunks guardados
                    from datanode.storage import delete_file
                    delete_file(session.file_id)
                    return False
                
                file_size_mb = len(assembled_content) / (1024 * 1024)
                print(f"[CHUNKED_STORAGE] ✅ {len(chunks_info)} chunks guardados y verificados | file_id={session.file_id[:16]}... | tamaño={file_size_mb:.2f} MB | hash={session.file_id[:16]}...")
                return True
            else:
                print(f"[CHUNKED_STORAGE] ❌ Error guardando chunks en almacenamiento final")
                return False
            
        except Exception as e:
            print(f"[CHUNKED_STORAGE] Error guardando chunks: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def cleanup_session(self, session_id: str):
        """Elimina una sesión y sus archivos temporales"""
        session = self.get_session(session_id)
        if not session:
            return
        
        # Eliminar directorio temporal
        temp_dir = session.get_temp_dir()
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"[CHUNKED_STORAGE] Directorio temporal eliminado: {temp_dir}")
            except Exception as e:
                print(f"[CHUNKED_STORAGE] Error eliminando directorio temporal: {e}")
        
        # Eliminar de sesiones activas
        with self.lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
        
        print(f"[CHUNKED_STORAGE] Sesión limpiada: {session_id}")
    
    def cleanup_old_sessions(self, max_age_seconds: float = 86400):
        """
        Limpia sesiones antiguas que no se han completado
        
        Args:
            max_age_seconds: Edad máxima en segundos (default: 24 horas)
        """
        current_time = time.time()
        to_cleanup = []
        
        with self.lock:
            for session_id, session in self.sessions.items():
                age = current_time - session.started_at
                if age > max_age_seconds:
                    to_cleanup.append(session_id)
        
        if to_cleanup:
            print(f"[CHUNKED_STORAGE] Limpiando {len(to_cleanup)} sesiones antiguas...")
            for session_id in to_cleanup:
                self.cleanup_session(session_id)


# Instancia global del manager
chunked_storage_manager = ChunkedStorageManager()

