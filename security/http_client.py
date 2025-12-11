"""
Helper para hacer peticiones HTTP con autenticación de servicio
"""
import requests
import os
from typing import Optional, Dict, Any
from security.service_auth import generate_service_token, get_service_token_for_service


def get_service_token(service_id: str) -> str:
    """
    Obtiene un token de servicio para un servicio específico
    Primero intenta generar un JWT, si falla usa token pre-compartido
    """
    try:
        return generate_service_token(service_id)
    except Exception:
        # Fallback a token pre-compartido
        token = get_service_token_for_service(service_id)
        if token:
            return token
        raise ValueError(f"No se pudo obtener token para servicio {service_id}")


def make_authenticated_request(
    method: str,
    url: str,
    service_id: str,
    **kwargs
) -> requests.Response:
    """
    Hace una petición HTTP autenticada con token de servicio
    
    Args:
        method: Método HTTP (get, post, put, delete, etc.)
        url: URL del endpoint
        service_id: ID del servicio que hace la petición
        **kwargs: Argumentos adicionales para requests
    
    Returns:
        Response de requests
    """
    token = get_service_token(service_id)
    
    headers = kwargs.get("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    kwargs["headers"] = headers
    
    return requests.request(method, url, **kwargs)


def get_authenticated(service_id: str, url: str, **kwargs) -> requests.Response:
    """Helper para GET autenticado"""
    return make_authenticated_request("get", url, service_id, **kwargs)


def post_authenticated(service_id: str, url: str, **kwargs) -> requests.Response:
    """Helper para POST autenticado"""
    return make_authenticated_request("post", url, service_id, **kwargs)


def put_authenticated(service_id: str, url: str, **kwargs) -> requests.Response:
    """Helper para PUT autenticado"""
    return make_authenticated_request("put", url, service_id, **kwargs)


def delete_authenticated(service_id: str, url: str, **kwargs) -> requests.Response:
    """Helper para DELETE autenticado"""
    return make_authenticated_request("delete", url, service_id, **kwargs)

