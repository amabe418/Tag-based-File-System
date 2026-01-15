"""
Read-Write Lock para permitir múltiples lecturas simultáneas
y solo una escritura a la vez
"""
import threading


class ReadWriteLock:
    """
    Lock que permite múltiples lecturas simultáneas o una escritura exclusiva
    """
    
    def __init__(self):
        self._read_ready = threading.Condition(threading.Lock())
        self._readers = 0
    
    def acquire_read(self):
        """Adquiere el lock para lectura (múltiples lectores permitidos)"""
        self._read_ready.acquire()
        try:
            self._readers += 1
        finally:
            self._read_ready.release()
    
    def release_read(self):
        """Libera el lock de lectura"""
        self._read_ready.acquire()
        try:
            self._readers -= 1
            if self._readers == 0:
                self._read_ready.notify_all()
        finally:
            self._read_ready.release()
    
    def acquire_write(self):
        """Adquiere el lock para escritura (exclusivo)"""
        self._read_ready.acquire()
        while self._readers > 0:
            self._read_ready.wait()
    
    def release_write(self):
        """Libera el lock de escritura"""
        self._read_ready.release()
    
    def __enter__(self):
        """Context manager para lectura (por defecto)"""
        self.acquire_read()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release_read()


class ReadLock:
    """Context manager para lectura"""
    def __init__(self, rw_lock: ReadWriteLock):
        self.rw_lock = rw_lock
    
    def __enter__(self):
        self.rw_lock.acquire_read()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.rw_lock.release_read()


class WriteLock:
    """Context manager para escritura"""
    def __init__(self, rw_lock: ReadWriteLock):
        self.rw_lock = rw_lock
    
    def __enter__(self):
        self.rw_lock.acquire_write()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.rw_lock.release_write()
