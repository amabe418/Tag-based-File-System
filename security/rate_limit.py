"""
Rate limiting para prevenir abuso de APIs
"""
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Dict, Tuple
import time
from collections import defaultdict
import threading


class RateLimiter:
    """Rate limiter simple basado en memoria"""
    
    def __init__(self):
        self.requests: Dict[str, list] = defaultdict(list)
        self.lock = threading.Lock()
    
    def is_allowed(
        self,
        key: str,
        max_requests: int = 100,
        time_window: int = 60
    ) -> Tuple[bool, int]:
        """
        Verifica si una petición está permitida
        
        Args:
            key: Identificador único (IP, usuario, etc.)
            max_requests: Número máximo de peticiones
            time_window: Ventana de tiempo en segundos
        
        Returns:
            (allowed, remaining): Si está permitido y peticiones restantes
        """
        current_time = time.time()
        
        with self.lock:
            # Limpiar peticiones antiguas
            self.requests[key] = [
                req_time for req_time in self.requests[key]
                if current_time - req_time < time_window
            ]
            
            # Verificar límite
            if len(self.requests[key]) >= max_requests:
                remaining = 0
                return False, remaining
            
            # Registrar nueva petición
            self.requests[key].append(current_time)
            remaining = max_requests - len(self.requests[key])
            return True, remaining


# Instancia global del rate limiter
rate_limiter = RateLimiter()


def get_client_identifier(request: Request) -> str:
    """Obtiene un identificador único del cliente para rate limiting"""
    # Intentar obtener usuario autenticado
    # Si no hay usuario, usar IP
    client_ip = request.client.host if request.client else "unknown"
    
    # Intentar obtener token de autorización
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        # Usar hash del token como identificador (sin exponer el token completo)
        token_hash = str(hash(token))[:16]
        return f"user:{token_hash}"
    
    return f"ip:{client_ip}"


def rate_limit_middleware(
    max_requests: int = 100,
    time_window: int = 60
):
    """
    Middleware de rate limiting
    
    Args:
        max_requests: Número máximo de peticiones por ventana de tiempo
        time_window: Ventana de tiempo en segundos
    """
    async def middleware(request: Request, call_next):
        # Excluir endpoints de health check y root
        if request.url.path in ["/", "/health", "/docs", "/openapi.json", "/redoc"]:
            return await call_next(request)
        
        client_id = get_client_identifier(request)
        allowed, remaining = rate_limiter.is_allowed(
            client_id,
            max_requests=max_requests,
            time_window=time_window
        )
        
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit excedido. Máximo {max_requests} peticiones por {time_window} segundos",
                headers={
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + time_window)
                }
            )
        
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + time_window)
        
        return response
    
    return middleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware de rate limiting para FastAPI"""
    
    def __init__(self, app, max_requests: int = 100, time_window: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.time_window = time_window
    
    async def dispatch(self, request: Request, call_next):
        # Excluir endpoints de health check y documentación
        if request.url.path in ["/", "/health", "/docs", "/openapi.json", "/redoc"]:
            return await call_next(request)
        
        client_id = get_client_identifier(request)
        allowed, remaining = rate_limiter.is_allowed(
            client_id,
            max_requests=self.max_requests,
            time_window=self.time_window
        )
        
        if not allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": f"Rate limit excedido. Máximo {self.max_requests} peticiones por {self.time_window} segundos"},
                headers={
                    "X-RateLimit-Limit": str(self.max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + self.time_window)
                }
            )
        
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(time.time()) + self.time_window)
        
        return response

