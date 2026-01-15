import os
import requests
import streamlit as st
import pandas as pd
import math
import random
import time
import hashlib
from typing import Optional, Tuple
from registry_client import registry_client

# Configuración de chunked upload
CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB por chunk
USE_CHUNKED_UPLOAD_THRESHOLD = 5 * 1024 * 1024  # Usar chunked para archivos > 5 MB
registry_client.print_registries()
registry_client.print_namenodes()

def get_server_url():
    """Obtiene la URL de un servidor desde el registry. Retorna (url, error_message)"""
    try:
        # Intentar obtener servidor del registry
        server_url = registry_client.get_server_url(strategy="random")
        if server_url:
            print(f"[CLIENT] URL obtenida del registry: {server_url}")
            # Verificar que la URL tenga el formato correcto
            if not server_url.startswith("http"):
                # Si el registry devuelve solo el hostname, agregar http:// y puerto
                if ":" not in server_url:
                    server_url = f"http://{server_url}:8010"
                else:
                    server_url = f"http://{server_url}"
            print(f"[CLIENT] URL final a usar: {server_url}")
            return server_url, None
        
        # Si no hay servidor, intentar obtener lista directamente
        print("[CLIENT] No se obtuvo URL directa, intentando obtener lista de servidores...")
        servers = registry_client.get_active_servers(use_cache=False)
        print(f"[CLIENT] Servidores obtenidos: {servers}")
        if servers and len(servers) > 0:
            server = random.choice(servers)
            server_url = server.get("url")
            print(f"[CLIENT] URL del servidor seleccionado: {server_url}")
            
            # Validar que server_url no sea None
            if not server_url:
                error_msg = f"El servidor seleccionado no tiene URL válida. Datos del servidor: {server}"
                print(f"[CLIENT] ERROR: {error_msg}")
                return None, error_msg
            
            # Verificar formato de URL
            if not server_url.startswith("http"):
                if ":" not in server_url:
                    server_url = f"http://{server_url}:8010"
                else:
                    server_url = f"http://{server_url}"
            print(f"[CLIENT] URL final a usar: {server_url}")
            return server_url, None
        
        # Si no hay servidores disponibles, retornar error
        print("[CLIENT] ERROR: No hay servidores disponibles en el registry")
        return None, "No hay servidores de datos disponibles en el registry. Por favor, verifica que el registry y los servidores backend estén funcionando."
        
    except Exception as e:
        print(f"[CLIENT] EXCEPCIÓN al obtener servidor: {e}")
        import traceback
        traceback.print_exc()
        return None, f"Error al consultar el registry: {e}"

def get_leader_url():
    """Obtiene la URL del líder del namenode consultando el endpoint / de cualquier namenode"""
    # Obtener todos los NameNodes disponibles del registry
    try:
        servers = registry_client.get_active_servers(use_cache=False)
        namenode_servers = [s for s in servers if "namenode" in s.get("server_id", "").lower()]
        
        if not namenode_servers:
            # Fallback: intentar obtener un servidor cualquiera
            server_url, error = get_server_url()
            if not server_url:
                return None, error or "No hay servidores disponibles"
            namenode_servers = [{"url": server_url}]
    except Exception as e:
        print(f"[CLIENT] Error obteniendo servidores del registry: {e}")
        # Fallback: intentar obtener un servidor cualquiera
        server_url, error = get_server_url()
        if not server_url:
            return None, error or "No hay servidores disponibles"
        namenode_servers = [{"url": server_url}]
    
    # Intentar consultar cada NameNode hasta encontrar el líder válido
    last_error = None
    for server in namenode_servers:
        server_url = server.get("url")
        if not server_url:
            continue
            
        # Asegurar formato correcto de URL
        if not server_url.startswith("http"):
            if ":" not in server_url:
                server_url = f"http://{server_url}:8010"
            else:
                server_url = f"http://{server_url}"
        
        try:
            print(f"[CLIENT] Consultando NameNode: {server_url}")
            # Consultar el endpoint / para obtener información del líder
            response = requests.get(f"{server_url}/", timeout=5)
            response.raise_for_status()
            data = response.json()
            
            # Prioridad 1: Si este namenode es el líder, verificar y usar su URL
            if data.get("is_leader"):
                print(f"[CLIENT] ✅ NameNode consultado es el líder: {server_url}")
                # Verificar que realmente es el líder
                verify_response = requests.get(f"{server_url}/", timeout=3)
                verify_response.raise_for_status()
                verify_data = verify_response.json()
                if verify_data.get("is_leader"):
                    return server_url, None
                else:
                    print(f"[CLIENT] ⚠️  {server_url} reporta que no es el líder en verificación")
                    continue
            
            # Prioridad 2: Usar leader_url si está disponible
            leader_url = data.get("leader_url")
            if leader_url:
                print(f"[CLIENT] Líder encontrado desde leader_url: {leader_url}")
                # Verificar que el líder realmente es el líder consultando su endpoint /
                try:
                    leader_response = requests.get(f"{leader_url}/", timeout=3)
                    leader_response.raise_for_status()
                    leader_data = leader_response.json()
                    if leader_data.get("is_leader"):
                        print(f"[CLIENT] ✅ Líder verificado: {leader_url}")
                        return leader_url, None
                    else:
                        # El líder reportado no es realmente el líder (información desactualizada)
                        # Continuar buscando en otros NameNodes
                        print(f"[CLIENT] ⚠️  {leader_url} reporta que no es el líder (información desactualizada), continuando búsqueda...")
                        # Guardar este líder como candidato pero continuar buscando
                        continue
                except requests.RequestException as e:
                    print(f"[CLIENT] ⚠️  Error verificando líder {leader_url}: {e}, continuando búsqueda...")
                    continue
            
            # Prioridad 3: Si hay leader_id pero no leader_url, construir la URL y verificar
            leader_id = data.get("leader_id")
            if leader_id:
                # Construir URL del líder basado en el leader_id
                # Si el leader_id es "namenode-1", la URL será "http://tbfs-namenode-1:8010"
                constructed_url = f"http://tbfs-{leader_id}:8010"
                print(f"[CLIENT] Líder construido desde leader_id: {constructed_url}")
                
                # VERIFICAR que realmente es el líder antes de retornarlo
                try:
                    verify_response = requests.get(f"{constructed_url}/", timeout=3)
                    verify_response.raise_for_status()
                    verify_data = verify_response.json()
                    if verify_data.get("is_leader") and (verify_data.get("leader_id") == leader_id or verify_data.get("node_id") == leader_id):
                        print(f"[CLIENT] ✅ Líder verificado desde leader_id: {constructed_url}")
                        return constructed_url, None
                    else:
                        # El líder reportado no es realmente el líder (información desactualizada)
                        print(f"[CLIENT] ⚠️  {constructed_url} no es el líder (is_leader={verify_data.get('is_leader')}, leader_id={verify_data.get('leader_id')}), continuando búsqueda...")
                        continue
                except requests.RequestException as e:
                    print(f"[CLIENT] ⚠️  Error verificando líder construido {constructed_url}: {e}, continuando búsqueda...")
                    continue
            
        except requests.RequestException as e:
            last_error = e
            print(f"[CLIENT] Error consultando {server_url}: {e}")
            continue
        except Exception as e:
            last_error = e
            print(f"[CLIENT] Error inesperado consultando {server_url}: {e}")
            continue
    
    # Si llegamos aquí, no se encontró un líder válido después de consultar todos los NameNodes
    # Esto puede pasar si:
    # 1. Hay una elección en curso
    # 2. Todos los NameNodes tienen información desactualizada
    # 3. No hay líder actualmente
    
    # Intentar una última vez consultando todos los NameNodes para ver si alguno es el líder
    print(f"[CLIENT] ⚠️  No se encontró líder válido después de consultar {len(namenode_servers)} NameNodes")
    print(f"[CLIENT] 🔄 Intentando búsqueda directa del líder consultando todos los NameNodes...")
    
    for server in namenode_servers:
        server_url = server.get("url")
        if not server_url:
            continue
        if not server_url.startswith("http"):
            if ":" not in server_url:
                server_url = f"http://{server_url}:8010"
            else:
                server_url = f"http://{server_url}"
        
        try:
            response = requests.get(f"{server_url}/", timeout=3)
            response.raise_for_status()
            data = response.json()
            if data.get("is_leader"):
                print(f"[CLIENT] ✅ Líder encontrado en búsqueda directa: {server_url}")
                return server_url, None
        except Exception:
            continue
    
    error_msg = f"No se pudo encontrar un líder válido después de consultar todos los NameNodes. Puede haber una elección en curso."
    print(f"[CLIENT] ❌ {error_msg}")
    return None, error_msg


def check_server_connection():
    """Verifica si hay conexión con el servidor. Retorna (connected, error_message)"""
    server_url, error = get_server_url()
    if not server_url:
        return False, error
    
    try:
        # Intentar hacer una petición simple al servidor para verificar conexión
        response = requests.get(f"{server_url}/", timeout=3)
        if response.status_code == 200:
            return True, None
        else:
            return False, f"El servidor respondió con código {response.status_code}"
    except requests.RequestException as e:
        return False, f"No se pudo conectar con el servidor: {e}"

# Inicializar URL del servidor en session state
if "server_url" not in st.session_state:
    server_url, _ = get_server_url()
    st.session_state.server_url = server_url
    if st.session_state.server_url:
        print(f"Usando servidor: {st.session_state.server_url}")
    else:
        print("⚠️ No se pudo obtener servidor del registry")

# Inicializar autenticación con persistencia usando cookies
COOKIE_TOKEN_KEY = "tbfs_auth_token"
COOKIE_USER_KEY = "tbfs_logged_in_user"

# Función para guardar en cookies (usando st.cookies y JavaScript como fallback)
def save_to_storage(key: str, value: str):
    """Guarda un valor en cookies del navegador"""
    try:
        # Método 1: Usar st.cookies (método nativo de Streamlit)
        if hasattr(st, 'cookies') and st.cookies is not None:
            if value:
                st.cookies[key] = value
                print(f"[CLIENT] Guardado en st.cookies: {key}")
            elif key in st.cookies:
                del st.cookies[key]
                print(f"[CLIENT] Eliminado de st.cookies: {key}")
    except Exception as e:
        print(f"[CLIENT] Error guardando en st.cookies {key}: {e}")
    
    # Método 2: También guardar con JavaScript para asegurar persistencia
    try:
        if value:
            # Escapar el valor para evitar problemas con caracteres especiales en JavaScript
            escaped_value = value.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
            cookie_script = f"""
            <script>
                (function() {{
                    try {{
                        // Guardar en cookies con JavaScript
                        document.cookie = '{key}=' + encodeURIComponent('{escaped_value}') + '; path=/; max-age=86400; SameSite=Lax';
                        // También guardar en localStorage como backup
                        localStorage.setItem('{key}', '{escaped_value}');
                    }} catch(e) {{
                        console.error('Error guardando cookie:', e);
                    }}
                }})();
            </script>
            """
        else:
            cookie_script = f"""
            <script>
                (function() {{
                    try {{
                        document.cookie = '{key}=; path=/; max-age=0; SameSite=Lax';
                        localStorage.removeItem('{key}');
                    }} catch(e) {{
                        console.error('Error eliminando cookie:', e);
                    }}
                }})();
            </script>
            """
        st.components.v1.html(cookie_script, height=0, width=0)
    except Exception as e:
        print(f"[CLIENT] Error guardando cookie con JavaScript {key}: {e}")

# Función para leer desde cookies
def read_from_storage(key: str) -> Optional[str]:
    """Lee un valor desde cookies del navegador"""
    try:
        # Método 1: Leer desde st.cookies (método nativo de Streamlit)
        if hasattr(st, 'cookies') and st.cookies is not None:
            value = st.cookies.get(key)
            if value:
                print(f"[CLIENT] Leído desde st.cookies: {key}")
                return value
    except Exception as e:
        print(f"[CLIENT] Error leyendo desde st.cookies {key}: {e}")
    
    # Método 2: Intentar leer desde localStorage usando JavaScript
    # (Streamlit no puede leer localStorage directamente, pero podemos sincronizarlo con cookies)
    try:
        sync_script = f"""
        <script>
            (function() {{
                try {{
                    // Leer desde localStorage y sincronizar con cookies
                    const value = localStorage.getItem('{key}');
                    if (value) {{
                        document.cookie = '{key}=' + encodeURIComponent(value) + '; path=/; max-age=86400; SameSite=Lax';
                    }}
                }} catch(e) {{
                    console.error('Error sincronizando localStorage:', e);
                }}
            }})();
        </script>
        """
        st.components.v1.html(sync_script, height=0, width=0)
        
        # Intentar leer desde cookies nuevamente después de la sincronización
        if hasattr(st, 'cookies') and st.cookies is not None:
            value = st.cookies.get(key)
            if value:
                print(f"[CLIENT] Leído desde cookies después de sincronización: {key}")
                return value
    except Exception as e:
        print(f"[CLIENT] Error sincronizando storage {key}: {e}")
    
    return None

# Inicializar desde storage si están disponibles
# IMPORTANTE: Sincronizar localStorage -> cookies PRIMERO antes de leer
if "auth_token" not in st.session_state:
    # Paso 1: Sincronizar localStorage -> cookies ANTES de intentar leer
    # Esto asegura que si hay datos en localStorage, estén disponibles en cookies
    sync_script = """
    <script>
        (function() {
            try {
                // Sincronizar localStorage -> cookies para ambos valores
                const token = localStorage.getItem('tbfs_auth_token');
                const user = localStorage.getItem('tbfs_logged_in_user');
                if (token) {
                    document.cookie = 'tbfs_auth_token=' + encodeURIComponent(token) + '; path=/; max-age=86400; SameSite=Lax';
                }
                if (user) {
                    document.cookie = 'tbfs_logged_in_user=' + encodeURIComponent(user) + '; path=/; max-age=86400; SameSite=Lax';
                }
            } catch(e) {
                console.error('Error sincronizando localStorage:', e);
            }
        })();
    </script>
    """
    st.components.v1.html(sync_script, height=0, width=0)
    
    # Paso 2: Intentar leer desde cookies (ahora deberían estar sincronizadas)
    token = None
    user = None
    
    try:
        # Leer desde st.cookies (método nativo de Streamlit)
        if hasattr(st, 'cookies') and st.cookies is not None:
            token = st.cookies.get(COOKIE_TOKEN_KEY)
            user = st.cookies.get(COOKIE_USER_KEY)
            if token and user:
                print(f"[CLIENT] Token y usuario leídos desde st.cookies: user={user}")
    except Exception as e:
        print(f"[CLIENT] Error leyendo desde st.cookies: {e}")
    
    # Paso 3: Si aún no se encontraron, usar read_from_storage como fallback
    # (que intentará sincronizar nuevamente y leer)
    if not token or not user:
        token_fallback = read_from_storage(COOKIE_TOKEN_KEY)
        user_fallback = read_from_storage(COOKIE_USER_KEY)
        if token_fallback:
            token = token_fallback
        if user_fallback:
            user = user_fallback
    
    # Paso 4: Asignar valores a session_state
    if token and user:
        st.session_state.auth_token = token
        st.session_state.logged_in_user = user
        print(f"[CLIENT] ✅ Sesión restaurada desde storage: user={user}")
    else:
        st.session_state.auth_token = None
        st.session_state.logged_in_user = None
        print(f"[CLIENT] ❌ No se encontró sesión guardada")

if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = None

def login(username: str, password: str):
    """Autentica al usuario y guarda el token - siempre usa el líder"""
    import time
    start_time = time.time()
    print(f"[CLIENT] [LOGIN] 🔐 Iniciando login para usuario: {username}")
    
    leader_url, error = get_leader_url()
    if not leader_url:
        print(f"[CLIENT] [LOGIN] ❌ No se pudo obtener líder: {error}")
        return False, error or "No hay líder disponible"
    
    print(f"[CLIENT] [LOGIN] 📍 Líder obtenido: {leader_url}")
    
    try:
        print(f"[CLIENT] [LOGIN] 📤 Enviando petición de login a {leader_url}/auth/login")
        response = requests.post(
            f"{leader_url}/auth/login",
            json={"username": username, "password": password},
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        user = data.get("user", {}).get("username")
        user_role = data.get("user", {}).get("role", "unknown")
        
        elapsed = time.time() - start_time
        
        # Validar que tanto token como user sean no-None
        if not token or not user:
            error_msg = "La respuesta del servidor no contiene token o usuario válido"
            print(f"[CLIENT] [LOGIN] ❌ {error_msg} (tiempo: {elapsed:.2f}s)")
            return False, error_msg
        
        # Guardar en session_state
        st.session_state.auth_token = token
        st.session_state.logged_in_user = user
        
        # Guardar en storage para persistencia (hacer esto ANTES del rerun)
        save_to_storage(COOKIE_TOKEN_KEY, token)
        save_to_storage(COOKIE_USER_KEY, user)
        print(f"[CLIENT] [LOGIN] ✅ Login exitoso: user={user}, role={user_role}, tiempo={elapsed:.2f}s")
        print(f"[CLIENT] [LOGIN] 💾 Token y usuario guardados en storage")
        
        return True, None
    except requests.HTTPError as e:
        elapsed = time.time() - start_time
        status_code = e.response.status_code if e.response else "unknown"
        print(f"[CLIENT] [LOGIN] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
        return False, f"Error de autenticación: {e}"
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [LOGIN] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
        return False, str(e)

def get_auth_headers():
    """Retorna los headers con el token de autenticación"""
    if st.session_state.auth_token:
        return {"Authorization": f"Bearer {st.session_state.auth_token}"}
    return {}

st.set_page_config(page_title="Tag-based File System", layout="wide")
st.markdown("---")
st.title("📂 Tag-based File System")

# --- Sistema de autenticación ---
if "auth_mode" not in st.session_state:
    st.session_state.auth_mode = "login"  # login | signup

if not st.session_state.auth_token:
    col_login, col_signup = st.columns(2)
    with col_login:
        if st.button("Iniciar sesión", use_container_width=True, type="primary"):
            st.session_state.auth_mode = "login"
    with col_signup:
        if st.button("Crear cuenta", use_container_width=True):
            st.session_state.auth_mode = "signup"

    if st.session_state.auth_mode == "login":
        st.subheader("Iniciar sesión")
        with st.form("login_form"):
            username = st.text_input("Usuario:", key="login_username")
            password = st.text_input("Contraseña:", type="password", key="login_password")
            login_button = st.form_submit_button("Iniciar Sesión", use_container_width=True)
            
            if login_button:
                if username and password:
                    success, error = login(username.strip().lower(), password)
                    if success:
                        st.success(f"✅ Bienvenido, {st.session_state.logged_in_user}!")
                        st.rerun()
                    else:
                        st.error(f"❌ Error al iniciar sesión: {error}")
                else:
                    st.warning("Por favor, ingresa usuario y contraseña")
        st.info("💡 Usuario por defecto: `admin` / Contraseña: `admin`")

    else:
        st.subheader("Crear cuenta")
        with st.form("signup_form"):
            su_username = st.text_input("Usuario:", key="signup_username")
            su_password = st.text_input("Contraseña:", type="password", key="signup_password")
            su_password2 = st.text_input("Confirmar contraseña:", type="password", key="signup_password2")
            signup_button = st.form_submit_button("Crear cuenta", use_container_width=True)

            if signup_button:
                if not su_username or not su_password or not su_password2:
                    st.warning("Por favor, completa todos los campos")
                elif su_password != su_password2:
                    st.error("Las contraseñas no coinciden")
                elif len(su_password) < 6:
                    st.warning("La contraseña debe tener al menos 6 caracteres")
                else:
                    import time
                    signup_start = time.time()
                    su_username = su_username.strip().lower()
                    print(f"[CLIENT] [SIGNUP] 📝 Iniciando registro de usuario: {su_username}")
                    
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        print(f"[CLIENT] [SIGNUP] ❌ No se pudo obtener líder: {error}")
                        st.error(error or "No hay líder disponible")
                    else:
                        try:
                            print(f"[CLIENT] [SIGNUP] 📍 Líder obtenido: {leader_url}")
                            print(f"[CLIENT] [SIGNUP] 📤 Enviando petición de signup a {leader_url}/auth/signup")
                            resp = requests.post(
                                f"{leader_url}/auth/signup",
                                json={"username": su_username, "password": su_password},
                                timeout=5,
                            )
                            if resp.status_code == 400:
                                elapsed = time.time() - signup_start
                                print(f"[CLIENT] [SIGNUP] ❌ Usuario ya existe (tiempo: {elapsed:.2f}s)")
                                st.error("El usuario ya existe, elige otro nombre de usuario.")
                            else:
                                resp.raise_for_status()
                                data = resp.json()
                                elapsed = time.time() - signup_start
                                token = data.get("access_token")
                                print(f"[CLIENT] [SIGNUP] ✅ Cuenta creada exitosamente (tiempo: {elapsed:.2f}s)")
                                if token:
                                    print(f"[CLIENT] [SIGNUP] 🎫 Token recibido, usuario puede iniciar sesión automáticamente")
                                st.success("✅ Cuenta creada. Inicia sesión con tus credenciales.")
                                # Limpiar campos y cambiar a login
                                st.session_state.auth_mode = "login"
                                st.session_state.login_username = su_username
                                st.session_state.login_password = ""
                                st.rerun()
                        except requests.HTTPError as e:
                            elapsed = time.time() - signup_start
                            status_code = e.response.status_code if e.response else "unknown"
                            print(f"[CLIENT] [SIGNUP] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error al crear cuenta: {e}")
                        except requests.RequestException as e:
                            elapsed = time.time() - signup_start
                            print(f"[CLIENT] [SIGNUP] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error al crear cuenta: {e}")
    st.stop()  # Detener la ejecución hasta que se autentique
else:
    # Mostrar información del usuario y opciones
    col_user, col_password, col_logout = st.columns([2, 1, 1])
    with col_user:
        st.info(f"👤 Usuario: **{st.session_state.logged_in_user}**")
    with col_password:
        if st.button("🔐 Cambiar Contraseña", use_container_width=True):
            st.session_state.show_change_password = True
            st.rerun()
    with col_logout:
        if st.button("🚪 Cerrar Sesión", use_container_width=True):
            # Limpiar session_state
            st.session_state.auth_token = None
            st.session_state.logged_in_user = None
            st.session_state.show_change_password = False
            
            # Eliminar storage
            save_to_storage(COOKIE_TOKEN_KEY, "")
            save_to_storage(COOKIE_USER_KEY, "")
            
            st.rerun()
    
    # Modal para cambiar contraseña
    if st.session_state.get("show_change_password", False):
        with st.expander("🔐 Cambiar Contraseña", expanded=True):
            old_password = st.text_input("Contraseña actual:", type="password", key="old_password")
            new_password = st.text_input("Nueva contraseña:", type="password", key="new_password")
            confirm_password = st.text_input("Confirmar nueva contraseña:", type="password", key="confirm_password")
            
            col_change, col_cancel = st.columns([1, 1])
            with col_change:
                if st.button("Cambiar Contraseña", key="confirm_change_password", use_container_width=True):
                    if not old_password or not new_password or not confirm_password:
                        st.warning("Por favor, completa todos los campos")
                    elif new_password != confirm_password:
                        st.error("Las contraseñas nuevas no coinciden")
                    elif len(new_password) < 6:
                        st.warning("La nueva contraseña debe tener al menos 6 caracteres")
                    else:
                        import time
                        change_pwd_start = time.time()
                        user = st.session_state.logged_in_user or "unknown"
                        print(f"[CLIENT] [CHANGE_PASSWORD] 🔐 Iniciando cambio de contraseña (usuario: {user})")
                        
                        leader_url, error = get_leader_url()
                        if not leader_url:
                            print(f"[CLIENT] [CHANGE_PASSWORD] ❌ No se pudo obtener líder: {error}")
                            st.error(error or "No hay líder disponible")
                        else:
                            try:
                                print(f"[CLIENT] [CHANGE_PASSWORD] 📍 Líder obtenido: {leader_url}")
                                print(f"[CLIENT] [CHANGE_PASSWORD] 📤 Enviando petición a {leader_url}/auth/change-password")
                                response = requests.post(
                                    f"{leader_url}/auth/change-password",
                                    json={
                                        "old_password": old_password,
                                        "new_password": new_password
                                    },
                                    headers=get_auth_headers(),
                                    timeout=5
                                )
                                response.raise_for_status()
                                data = response.json()
                                elapsed = time.time() - change_pwd_start
                                if data.get("success"):
                                    print(f"[CLIENT] [CHANGE_PASSWORD] ✅ Contraseña cambiada exitosamente (tiempo: {elapsed:.2f}s)")
                                    st.success("✅ Contraseña cambiada exitosamente")
                                    st.session_state.show_change_password = False
                                    st.rerun()
                                else:
                                    print(f"[CLIENT] [CHANGE_PASSWORD] ❌ Error: respuesta no exitosa (tiempo: {elapsed:.2f}s)")
                                    st.error("Error al cambiar la contraseña")
                            except requests.HTTPError as e:
                                elapsed = time.time() - change_pwd_start
                                status_code = e.response.status_code if e.response else "unknown"
                                if status_code == 400:
                                    print(f"[CLIENT] [CHANGE_PASSWORD] ❌ Contraseña actual incorrecta (tiempo: {elapsed:.2f}s)")
                                    st.error("❌ La contraseña actual es incorrecta")
                                else:
                                    print(f"[CLIENT] [CHANGE_PASSWORD] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
                                    st.error(f"Error: {e}")
                            except requests.RequestException as e:
                                elapsed = time.time() - change_pwd_start
                                print(f"[CLIENT] [CHANGE_PASSWORD] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
                                st.error(f"Error de conexión: {e}")
            with col_cancel:
                if st.button("Cancelar", key="cancel_change_password", use_container_width=True):
                    st.session_state.show_change_password = False
                    st.rerun()

# Verificar conexión con el servidor y mostrar errores en un expander
is_connected, connection_error = check_server_connection()
errors = []

if not is_connected:
    if connection_error:
        errors.append(connection_error)
    
    # Mostrar errores en un expander colapsable
    with st.expander("⚠️ Problemas de conexión (click para ver detalles)", expanded=True):
        if errors:
            for error in errors:
                st.error(f"❌ {error}")
        else:
            st.error("⚠️ No hay conexión con el servidor.")
        st.info("💡 Los botones estarán deshabilitados hasta que se restablezca la conexión. Asegúrate de que el Registry Service y los servidores backend estén funcionando.")

# --- Estado inicial ---
if "modal" not in st.session_state:
    st.session_state.modal = None
if "refresh_needed" not in st.session_state:
    st.session_state.refresh_needed = False  # fuerza recarga solo al confirmar una acción
if "selected_files" not in st.session_state:
    st.session_state.selected_files = set()  # conjunto de nombres de archivos seleccionados
if "table_version" not in st.session_state:
    st.session_state.table_version = {}
if "files_to_download" not in st.session_state:
    st.session_state.files_to_download = []  # lista de archivos para descargar  # versión por página para forzar reset del widget

# --- Función para refrescar lista ---
def refresh_list(tags=None):
    """Obtiene la lista de archivos desde el líder"""
    import time
    start_time = time.time()
    user = st.session_state.logged_in_user or "unknown"
    filter_info = f"tags={tags}" if tags else "sin filtros"
    print(f"[CLIENT] [LIST] 📋 Obteniendo lista de archivos (usuario: {user}, {filter_info})")
    
    leader_url, _ = get_leader_url()
    if not leader_url:
        print(f"[CLIENT] [LIST] ❌ No se pudo obtener líder")
        return []
    
    if not st.session_state.auth_token:
        print(f"[CLIENT] [LIST] ❌ No hay token de autenticación")
        return []
    
    try:
        params = {}
        if tags:
            params["tags"] = tags
        print(f"[CLIENT] [LIST] 📤 Enviando petición a {leader_url}/list con params={params}")
        response = requests.get(
            f"{leader_url}/list",
            params=params,
            headers=get_auth_headers(),
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        files = data.get("files", [])
        elapsed = time.time() - start_time
        print(f"[CLIENT] [LIST] ✅ Lista obtenida: {len(files)} archivos (tiempo: {elapsed:.2f}s)")
        return files
    except requests.HTTPError as e:
        elapsed = time.time() - start_time
        status_code = e.response.status_code if e.response else "unknown"
        print(f"[CLIENT] [LIST] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
        return []
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [LIST] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
        return []

# --- Función para obtener contenido de archivo en memoria ---
def get_file_content(file_name):
    """
    Obtiene el contenido de un archivo directamente en memoria.
    Retorna el contenido en bytes y None si hay error.
    """
    import time
    start_time = time.time()
    user = st.session_state.logged_in_user or "unknown"
    print(f"[CLIENT] [DOWNLOAD] ⬇️  Iniciando descarga: {file_name} (usuario: {user})")
    
    server_url, _ = get_server_url()
    if not server_url:
        print(f"[CLIENT] [DOWNLOAD] ❌ No hay servidor disponible")
        return None, "No hay servidor disponible"
    
    try:
        # Obtener la URL del líder para la descarga
        leader_url, leader_error = get_leader_url()
        if not leader_url:
            print(f"[CLIENT] [DOWNLOAD] ❌ No se pudo obtener líder: {leader_error}")
            return None, f"No se pudo obtener el líder: {leader_error}"
        
        print(f"[CLIENT] [DOWNLOAD] 📍 Líder obtenido: {leader_url}")
        print(f"[CLIENT] [DOWNLOAD] 📤 Enviando petición de descarga a {leader_url}/download/{file_name}")
        
        # Intentar descargar desde el líder, siguiendo redirecciones automáticamente
        r = requests.get(
            f"{leader_url}/download/{file_name}", 
            stream=True, 
            timeout=30,
            headers=get_auth_headers(),
            allow_redirects=True  # Seguir redirecciones HTTP 307 automáticamente
        )
        r.raise_for_status()
        
        # Obtener tamaño del archivo si está disponible
        content_length = r.headers.get('Content-Length')
        file_size = int(content_length) if content_length else "unknown"
        print(f"[CLIENT] [DOWNLOAD] 📦 Tamaño del archivo: {file_size} bytes")
        
        # Leer el contenido directamente en memoria
        chunks = []
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                chunks.append(chunk)
        file_content = b''.join(chunks)
        
        elapsed = time.time() - start_time
        print(f"[CLIENT] [DOWNLOAD] ✅ Archivo descargado en memoria: {file_name} ({len(file_content)} bytes, tiempo: {elapsed:.2f}s)")
        return file_content, None
        
    except requests.HTTPError as e:
        elapsed = time.time() - start_time
        status_code = e.response.status_code if e.response else "unknown"
        # Si es un error 503, puede ser que el namenode no sea el líder
        if status_code == 503:
            print(f"[CLIENT] [DOWNLOAD] ❌ Servicio no disponible (503) - El namenode puede no ser el líder (tiempo: {elapsed:.2f}s)")
            return None, f"Servicio no disponible. El namenode puede no ser el líder. Intenta de nuevo."
        print(f"[CLIENT] [DOWNLOAD] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
        return None, str(e)
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [DOWNLOAD] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
        return None, f"Error de conexión: {str(e)}"
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [DOWNLOAD] ❌ Error inesperado: {e} (tiempo: {elapsed:.2f}s)")
        return None, f"Error inesperado: {str(e)}"


# ========== FUNCIONES PARA CHUNKED UPLOAD ==========

def calculate_file_hash(file_bytes: bytes) -> str:
    """Calcula el hash SHA-256 de un archivo"""
    return hashlib.sha256(file_bytes).hexdigest()


def upload_file_chunked(
    leader_url: str,
    filename: str,
    file_bytes: bytes,
    tags: str,
    progress_callback=None
) -> Tuple[bool, str]:
    """
    Sube un archivo usando chunked upload con barra de progreso
    
    Args:
        leader_url: URL del namenode líder
        filename: Nombre del archivo
        file_bytes: Contenido del archivo en bytes
        tags: Etiquetas separadas por comas
        progress_callback: Función para actualizar progreso (recibe progress_pct, status_text)
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    import time
    start_time = time.time()
    file_size = len(file_bytes)
    file_hash = f"sha256:{calculate_file_hash(file_bytes)}"
    user = st.session_state.logged_in_user or "unknown"
    
    print(f"[CLIENT] [UPLOAD_CHUNKED] 📤 Iniciando upload chunked: {filename}")
    print(f"[CLIENT] [UPLOAD_CHUNKED] 📊 Tamaño: {file_size:,} bytes, tags: {tags}, usuario: {user}")
    print(f"[CLIENT] [UPLOAD_CHUNKED] 📍 Líder: {leader_url}")
    
    try:
        # 1. Iniciar sesión de upload
        if progress_callback:
            progress_callback(0, "Iniciando sesión de upload...")
        
        print(f"[CLIENT] [UPLOAD_CHUNKED] 🔄 Paso 1/4: Iniciando sesión de upload...")
        response = requests.post(
            f"{leader_url}/upload/init",
            data={
                "filename": filename,
                "file_hash": file_hash,
                "file_size": file_size,
                "tags": tags,
                "chunk_size": CHUNK_SIZE
            },
            headers=get_auth_headers(),
            timeout=2000  # Timeout aumentado a 2000 segundos para archivos grandes
        )
        response.raise_for_status()
        data = response.json()
        
        upload_id = data["upload_id"]
        total_chunks = data["total_chunks"]
        datanode_urls = data.get("datanode_urls", [])
        
        print(f"[CLIENT] [UPLOAD_CHUNKED] ✅ Sesión creada: upload_id={upload_id}, total_chunks={total_chunks}, datanodes={len(datanode_urls)}")
        
        if not datanode_urls:
            return False, "No se recibieron URLs de DataNodes para upload directo"
        
        # 2. Inicializar sesión en el DataNode primario (solo uno)
        print(f"[CLIENT] [UPLOAD_CHUNKED] 🔄 Paso 2/4: Inicializando sesión en DataNode primario...")
        dn_info = datanode_urls[0]  # Solo el primer DataNode
        dn_url = dn_info["url"]
        dn_token = dn_info["token"]
        dn_id = dn_info["datanode_id"]
        
        try:
            response = requests.post(
                f"{dn_url}/client/upload/init",
                data={
                    "file_id": file_hash.replace("sha256:", ""),
                    "total_chunks": total_chunks,
                    "chunk_size": CHUNK_SIZE,
                    "file_size": file_size
                },
                headers={"Authorization": f"Bearer {dn_token}"},
                timeout=2000
            )
            response.raise_for_status()
            session_data = response.json()
            datanode_session = {
                "session_id": session_data["session_id"],
                "url": dn_url,
                "token": dn_token,
                "datanode_id": dn_id
            }
            print(f"[CLIENT] [UPLOAD_CHUNKED] ✅ Sesión iniciada en {dn_id}: {session_data['session_id']}")
        except Exception as e:
            return False, f"No se pudo inicializar sesión en DataNode {dn_id}: {e}"
        
        # 3. Subir chunks directamente al DataNode primario
        print(f"[CLIENT] [UPLOAD_CHUNKED] 🔄 Paso 3/4: Subiendo chunks al DataNode primario ({total_chunks} total)...")
        chunks_uploaded = 0
        
        for chunk_index in range(total_chunks):
            # Calcular posición y tamaño del chunk
            start = chunk_index * CHUNK_SIZE
            end = min(start + CHUNK_SIZE, file_size)
            chunk_data = file_bytes[start:end]
            chunk_hash = calculate_file_hash(chunk_data)
            
            # Subir chunk al DataNode primario con reintentos
            max_retries = 3
            success = False
            
            for retry in range(max_retries):
                try:
                    response = requests.post(
                        f"{datanode_session['url']}/client/upload/session/{datanode_session['session_id']}/chunk/{chunk_index}",
                        files={"chunk": (f"chunk_{chunk_index}", chunk_data)},
                        data={"chunk_hash": chunk_hash},
                        headers={"Authorization": f"Bearer {datanode_session['token']}"},
                        timeout=2000
                    )
                    response.raise_for_status()
                    success = True
                    break
                except Exception as e:
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        time.sleep(wait_time)
                    else:
                        return False, f"Error subiendo chunk {chunk_index + 1}: {e}"
            
            if not success:
                return False, f"No se pudo subir chunk {chunk_index + 1}"
            
            # Actualizar progreso
            chunks_uploaded += 1
            progress_pct = ((chunk_index + 1) / total_chunks) * 100
            if progress_callback:
                progress_callback(
                    progress_pct,
                    f"Subiendo chunk {chunk_index + 1}/{total_chunks} ({progress_pct:.1f}%)"
                )
            if (chunk_index + 1) % 10 == 0 or chunk_index == total_chunks - 1:
                print(f"[CLIENT] [UPLOAD_CHUNKED] 📊 Progreso: {chunk_index + 1}/{total_chunks} chunks ({progress_pct:.1f}%)")
        
        print(f"[CLIENT] [UPLOAD_CHUNKED] ✅ Todos los chunks subidos: {chunks_uploaded}/{total_chunks}")
        
        # 4. Finalizar upload en el DataNode primario
        print(f"[CLIENT] [UPLOAD_CHUNKED] 🔄 Paso 4/4: Finalizando upload en DataNode primario...")
        if progress_callback:
            progress_callback(95, "Finalizando upload...")
        
        finalize_start = time.time()
        try:
            response = requests.post(
                f"{datanode_session['url']}/client/upload/session/{datanode_session['session_id']}/finalize",
                headers={"Authorization": f"Bearer {datanode_session['token']}"},
                timeout=2000
            )
            response.raise_for_status()
            result = response.json()
            print(f"[CLIENT] [UPLOAD_CHUNKED] ✅ Upload finalizado en {dn_id}: {result.get('message', 'OK')}")
        except requests.HTTPError as e:
            error_detail = "unknown"
            if e.response is not None:
                try:
                    error_data = e.response.json()
                    error_detail = error_data.get("detail", str(e))
                except:
                    error_detail = e.response.text or str(e)
            return False, f"Error finalizando upload en {dn_id}: {error_detail}"
        except Exception as e:
            return False, f"Error finalizando upload en {dn_id}: {e}"
        
        # 5. Notificar al NameNode que el upload está completo (replicación eventual se hará después)
        print(f"[CLIENT] [UPLOAD_CHUNKED] 🔄 Paso 5/5: Notificando al NameNode (replicación eventual después)...")
        if progress_callback:
            progress_callback(100, "Completando upload...")
        
        try:
            response = requests.post(
                f"{leader_url}/upload/{upload_id}/finalize",
                headers=get_auth_headers(),
                timeout=2000
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"[CLIENT] [UPLOAD_CHUNKED] ⚠️  Error notificando al NameNode: {e}, pero el archivo ya está en el DataNode primario")
            # El archivo ya está en el DataNode, así que consideramos el upload exitoso
            data = {"file_id": "unknown", "replicas_stored": 1}
        
        elapsed = time.time() - start_time
        finalize_time = time.time() - finalize_start
        file_id = data.get('file_id', 'unknown')
        replicas = data.get('replicas_stored', 1)
        
        print(f"[CLIENT] [UPLOAD_CHUNKED] ✅ Upload completado exitosamente")
        print(f"[CLIENT] [UPLOAD_CHUNKED] 📊 Resumen: file_id={file_id}, réplicas_iniciales={replicas}, tiempo_total={elapsed:.2f}s, finalización={finalize_time:.2f}s")
        print(f"[CLIENT] [UPLOAD_CHUNKED] ℹ️  La replicación eventual a otros DataNodes se realizará en background")
        
        return True, f"Archivo subido correctamente (ID: {file_id}, replicación eventual en progreso)"
        
    except requests.HTTPError as e:
        elapsed = time.time() - start_time
        status_code = e.response.status_code if e.response else "unknown"
        print(f"[CLIENT] [UPLOAD_CHUNKED] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
        return False, f"Error de red: {e}"
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [UPLOAD_CHUNKED] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
        return False, f"Error de red: {e}"
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [UPLOAD_CHUNKED] ❌ Error inesperado: {e} (tiempo: {elapsed:.2f}s)")
        import traceback
        traceback.print_exc()
        return False, f"Error inesperado: {e}"


def upload_file_legacy(
    leader_url: str,
    filename: str,
    file_bytes: bytes,
    tags: str
) -> Tuple[bool, str]:
    """
    Sube un archivo usando el método tradicional (sin chunks)
    
    Args:
        leader_url: URL del namenode líder
        filename: Nombre del archivo
        file_bytes: Contenido del archivo en bytes
        tags: Etiquetas separadas por comas
    
    Returns:
        (success, message): Tupla con éxito y mensaje
    """
    import time
    start_time = time.time()
    file_size = len(file_bytes)
    user = st.session_state.logged_in_user or "unknown"
    
    print(f"[CLIENT] [UPLOAD_LEGACY] 📤 Iniciando upload legacy: {filename}")
    print(f"[CLIENT] [UPLOAD_LEGACY] 📊 Tamaño: {file_size:,} bytes, tags: {tags}, usuario: {user}")
    print(f"[CLIENT] [UPLOAD_LEGACY] 📍 Líder: {leader_url}")
    
    try:
        print(f"[CLIENT] [UPLOAD_LEGACY] 📤 Enviando archivo completo a {leader_url}/add")
        response = requests.post(
            f"{leader_url}/add",
            files={"file": (filename, file_bytes)},
            data={"tags": tags},
            headers=get_auth_headers(),
            timeout=120
        )
        response.raise_for_status()
        data = response.json()
        
        elapsed = time.time() - start_time
        file_id = data.get('file_id', 'unknown')
        replicas = data.get('replicas_stored', 0)
        
        print(f"[CLIENT] [UPLOAD_LEGACY] ✅ Upload completado exitosamente")
        print(f"[CLIENT] [UPLOAD_LEGACY] 📊 Resumen: file_id={file_id}, réplicas={replicas}, tiempo={elapsed:.2f}s")
        
        return True, f"Archivo subido correctamente (ID: {file_id}, {replicas} réplicas)"
        
    except requests.HTTPError as e:
        elapsed = time.time() - start_time
        status_code = e.response.status_code if e.response else "unknown"
        print(f"[CLIENT] [UPLOAD_LEGACY] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
        return False, f"Error: {e}"
    except requests.RequestException as e:
        elapsed = time.time() - start_time
        print(f"[CLIENT] [UPLOAD_LEGACY] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
        return False, f"Error: {e}"

# --- Mostrar lista ---
st.subheader("📖 Archivos disponibles")
tags_filter = st.text_input("Filtrar por etiquetas (separadas por comas):", key="tag_filter")

# Reset de selección al cambiar filtro de búsqueda
if "prev_tag_filter" not in st.session_state:
    st.session_state.prev_tag_filter = tags_filter

if tags_filter != st.session_state.prev_tag_filter:
    # Limpiar selección y versiones de tabla y volver a página 1
    st.session_state.selected_files = set()
    st.session_state.table_version = {}
    st.session_state.current_page = 1
    st.session_state.prev_tag_filter = tags_filter

# Solo refrescamos la lista si se necesita
if st.session_state.refresh_needed:
    st.session_state.refresh_needed = False
    st.rerun()

# --- Auto-refresh de la lista de archivos ---
AUTO_REFRESH_INTERVAL = 5  # segundos
if "last_refresh_time" not in st.session_state:
    st.session_state.last_refresh_time = time.time()
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = True

# Verificar si es tiempo de refrescar (solo si el usuario está autenticado y no hay modales abiertos)
if (st.session_state.auto_refresh_enabled and 
    st.session_state.auth_token and 
    st.session_state.modal is None):
    current_time = time.time()
    time_since_refresh = current_time - st.session_state.last_refresh_time
    
    if time_since_refresh >= AUTO_REFRESH_INTERVAL:
        st.session_state.last_refresh_time = current_time
        # Usar st.rerun() para refrescar la página automáticamente
        st.rerun()

# --- CSS para mejorar la presentación ---
st.markdown("""
    <style>
        /* Reduce padding general del contenedor */
        .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
        }
        
        /* Mejora la tabla de datos */
        .stDataFrame {
            width: 100%;
        }
        
        /* Botones cuadrados (sin bordes redondeados) - aplica a todos */
        button,
        div.stButton > button,
        form button,
        [data-testid="baseButton-secondary"],
        [data-testid="baseButton-primary"],
        [data-testid="stFormSubmitButton"] button {
            border-radius: 0 !important;
        }
        
        /* Estilos de paginación */
        .pagination { margin-top: 0.3rem; }
        .pagination [data-testid="column"] { display: flex; align-items: center; justify-content: center; }
        .pagination button {
            width: 100%;
            border: 1px solid #BDBDBD !important;
            background: #FFFFFF !important;
            padding: 0.5rem 0.9rem !important;
        }
        .pagination button:hover { background: #F5F5F5 !important; }
        .pagination button:disabled { background: #F2F2F2 !important; color: #9E9E9E !important; border-color: #E0E0E0 !important; }
        .pagination .page-label { text-align: center; width: 100%; font-weight: 600; margin: 0 auto; }
    </style>
""", unsafe_allow_html=True)

files = refresh_list(tags_filter)

# --- Parámetros de paginación ---
ITEMS_PER_PAGE = 10
if "current_page" not in st.session_state:
    st.session_state.current_page = 1

if files:
    total_items = len(files)
    total_pages = math.ceil(total_items / ITEMS_PER_PAGE)

    # Aseguramos que la página actual esté dentro del rango
    st.session_state.current_page = max(1, min(st.session_state.current_page, total_pages))

    # Calculamos los índices de los archivos visibles en esta página
    start_idx = (st.session_state.current_page - 1) * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    visible_files = files[start_idx:end_idx]

    # Crear DataFrame con los archivos visibles
    df_data = []
    for f in visible_files:
        file_name = f.get("name", "")
        tags = f.get("tags", "")
        df_data.append({
            "Nombre": file_name,
            "Etiquetas": tags if tags else "(sin etiquetas)"
        })
    
    df = pd.DataFrame(df_data)
    
    # Mostrar etiqueta de paginación antes de la tabla
    st.markdown(
        f"<div class='page-label'>Página {st.session_state.current_page} de {total_pages} ({total_items} archivos totales)</div>",
        unsafe_allow_html=True
    )
    
    # Mostrar tabla con data_editor para permitir selección
    st.markdown("### 📋 Tabla de archivos")
    
    # Preparar DataFrame con columna de selección según estado actual
    df_with_selection = df.copy()
    selection_column = [f.get("name") in st.session_state.selected_files for f in visible_files]
    df_with_selection["Seleccionar"] = selection_column

    # Obtener versión de tabla para esta página (para forzar reinicialización de widget)
    page_idx = st.session_state.current_page
    page_ver = st.session_state.table_version.get(page_idx, 0)

    # Form: cambios dentro no provocan rerun hasta enviar
    with st.form(f"file_table_form_{page_idx}", clear_on_submit=False):
        edited_df = st.data_editor(
            df_with_selection,
            column_config={
                "Nombre": st.column_config.TextColumn(
                    "Nombre del archivo",
                    width="large",
                ),
                "Etiquetas": st.column_config.TextColumn(
                    "Etiquetas",
                    width="large",
                ),
                "Seleccionar": st.column_config.CheckboxColumn(
                    "Seleccionar",
                    help="Marca los archivos que deseas descargar",
                    default=False,
                ),
            },
            hide_index=True,
            use_container_width=True,
            key=f"file_table_{page_idx}_{page_ver}",
            num_rows="fixed"
        )

        col_select_all, col_deselect_all, col_download_selected = st.columns([1.5, 1.5, 2])
        select_all = col_select_all.form_submit_button("✅ Seleccionar todos", use_container_width=True, disabled=not is_connected)
        deselect_all = col_deselect_all.form_submit_button("❌ Deseleccionar todos", use_container_width=True, disabled=not is_connected)
        download_clicked = col_download_selected.form_submit_button("📥 Descargar seleccionados", use_container_width=True, type="primary", disabled=not is_connected)

        # Manejo de envíos del formulario
        if select_all:
            for f in visible_files:
                st.session_state.selected_files.add(f.get("name"))
            st.session_state.table_version[page_idx] = page_ver + 1  # forzar reinicio del widget
            st.rerun()

        if deselect_all:
            # Quitar de la selección todos los visibles y forzar reinicio del widget
            visible_names = [f.get("name") for f in visible_files]
            for name in visible_names:
                st.session_state.selected_files.discard(name)
            st.session_state.table_version[page_idx] = page_ver + 1  # forzar reinicio del widget
            st.rerun()

        if download_clicked:
            # Sincronizar selección desde el editor
            for _, row in edited_df.iterrows():
                file_name = row["Nombre"]
                is_selected = bool(row["Seleccionar"])
                if is_selected:
                    st.session_state.selected_files.add(file_name)
                else:
                    st.session_state.selected_files.discard(file_name)

            selected_in_page = [row["Nombre"] for _, row in edited_df.iterrows() if bool(row["Seleccionar"]) ]
            if not selected_in_page:
                st.warning("Selecciona al menos un archivo.")
            else:
                # Guardar archivos para descargar fuera del formulario
                user = st.session_state.logged_in_user or "unknown"
                print(f"[CLIENT] [DOWNLOAD] 📥 Usuario {user} inició descarga de {len(selected_in_page)} archivo(s)")
                print(f"[CLIENT] [DOWNLOAD] 📋 Archivos seleccionados: {', '.join(selected_in_page)}")
                st.session_state.files_to_download = selected_in_page.copy()
                st.rerun()

    # Mostrar botones de descarga fuera del formulario
    if st.session_state.files_to_download:
        st.markdown("### 📥 Descargar archivos seleccionados")
        files_to_remove = []
        for file_name in st.session_state.files_to_download:
            file_content, error = get_file_content(file_name)
            if file_content:
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.download_button(
                        label=f"📥 Descargar {file_name}",
                        data=file_content,
                        file_name=file_name,
                        mime="application/octet-stream",
                        key=f"download_{file_name}_{page_idx}",
                        use_container_width=True
                    )
                with col2:
                    if st.button("❌", key=f"remove_{file_name}_{page_idx}", help="Quitar de la lista"):
                        files_to_remove.append(file_name)
            else:
                st.error(f"❌ Error al obtener '{file_name}': {error}")
                files_to_remove.append(file_name)
        
        # Remover archivos de la lista
        for file_name in files_to_remove:
            if file_name in st.session_state.files_to_download:
                st.session_state.files_to_download.remove(file_name)
        
        if st.button("🗑️ Limpiar lista de descargas", key="clear_downloads"):
            st.session_state.files_to_download = []
            st.rerun()

    # --- Controles de paginación ---
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='pagination'>", unsafe_allow_html=True)
    col_prev, col_page, col_next = st.columns([1, 2, 1])
    
    with col_prev:
        if st.button("⬅️ Anterior", disabled=(st.session_state.current_page == 1 or not is_connected), use_container_width=True):
            # No persistir selección al cambiar de página
            st.session_state.selected_files = set()
            # Opcional: resetear versión de la tabla para la nueva página
            st.session_state.table_version[st.session_state.current_page - 1] = 0
            st.session_state.current_page -= 1
            st.rerun()

    with col_page:
        # etiqueta de paginación se muestra arriba de la tabla; dejamos la columna vacía
        st.markdown("&nbsp;", unsafe_allow_html=True)

    with col_next:
        if st.button("Siguiente ➡️", disabled=(st.session_state.current_page == total_pages or not is_connected), use_container_width=True):
            # No persistir selección al cambiar de página
            st.session_state.selected_files = set()
            # Opcional: resetear versión de la tabla para la nueva página
            st.session_state.table_version[st.session_state.current_page + 1] = 0
            st.session_state.current_page += 1
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

else:
    st.warning("No se encontraron archivos.")


# --- Botones principales centrados en la parte superior ---
col_empty_left, col1, col2, col3, col4, col_empty_right = st.columns([1, 2, 2, 2, 2, 1])
with col1:
    if st.button("➕ Agregar archivo(s)", key="btn_add_file", use_container_width=True, disabled=not is_connected):
        st.session_state.modal = "add_file"
with col2:
    if st.button("🔖➕ Agregar etiqueta(s)", key="btn_add_tags", use_container_width=True, disabled=not is_connected):
        st.session_state.modal = "add_tags"
with col3:
    if st.button("🔖❌ Eliminar etiqueta(s)", key="btn_del_tags", use_container_width=True, disabled=not is_connected):
        st.session_state.modal = "del_tags"
with col4:
    if st.button("🗑️ Eliminar archivo(s)", key="btn_del_files", use_container_width=True, disabled=not is_connected):
        st.session_state.modal = "del_files"

# --- Modal: Agregar archivos ---
if st.session_state.modal == "add_file":
    with st.expander("📁 Subir nuevos archivos", expanded=True):
        uploaded_files = st.file_uploader("Selecciona archivos", accept_multiple_files=True, key="file_uploader")
        tags = st.text_input("Etiquetas (separadas por comas):", key="add_file_tags")
        
        # Mostrar información sobre chunked upload
        if uploaded_files:
            total_size = sum(len(f.getvalue()) for f in uploaded_files)
            large_files = [f for f in uploaded_files if len(f.getvalue()) > USE_CHUNKED_UPLOAD_THRESHOLD]
            
            if large_files:
                st.info(f"📦 {len(large_files)} archivo(s) grande(s) detectado(s). Se usará **chunked upload** (resumible si se interrumpe).")

        colA, colB, colC = st.columns([2, 1, 1])
        with colA:
            if st.button("Agregar Archivo(s)", key="upload_button", disabled=not is_connected, use_container_width=True):
                print(f"[CLIENT] Botón 'Agregar Archivo(s)' presionado. uploaded_files={uploaded_files}, tags={tags}, is_connected={is_connected}")
                if not uploaded_files:
                    st.warning("Selecciona al menos un archivo.")
                elif not tags.strip():
                    st.warning("Debes ingresar al menos una etiqueta.")
                else:
                    server_url, _ = get_server_url()
                    if not server_url:
                        st.error("No hay servidor disponible para subir archivos.")
                    else:
                        # Obtener la URL del líder antes de subir archivos
                        leader_url, leader_error = get_leader_url()
                        if not leader_url:
                            st.error(f"No se pudo obtener el líder del namenode: {leader_error}")
                        else:
                            print(f"[CLIENT] Intentando subir archivo(s) al líder: {leader_url}")
                            success_count = 0
                            error_count = 0
                            
                            # Contenedor para barra de progreso general
                            progress_container = st.empty()
                            status_container = st.empty()
                            
                            for file_idx, file in enumerate(uploaded_files):
                                file_bytes = file.getvalue()
                                file_size = len(file_bytes)
                                
                                # Determinar si usar chunked upload
                                use_chunked = file_size > USE_CHUNKED_UPLOAD_THRESHOLD
                                
                                # Mostrar archivo actual
                                status_container.info(f"📤 Subiendo {file_idx + 1}/{len(uploaded_files)}: **{file.name}** ({file_size:,} bytes) {'[Chunked]' if use_chunked else '[Legacy]'}")
                                
                                try:
                                    if use_chunked:
                                        # Usar chunked upload con barra de progreso
                                        progress_bar = progress_container.progress(0)
                                        
                                        def update_progress(progress_pct, status_text):
                                            progress_bar.progress(int(progress_pct))
                                        
                                        success, message = upload_file_chunked(
                                            leader_url,
                                            file.name,
                                            file_bytes,
                                            tags,
                                            progress_callback=update_progress
                                        )
                                    else:
                                        # Usar método legacy para archivos pequeños
                                        progress_container.info("⏳ Subiendo...")
                                        success, message = upload_file_legacy(
                                            leader_url,
                                            file.name,
                                            file_bytes,
                                            tags
                                        )
                                    
                                    if success:
                                        st.success(f"✅ {file.name}: {message}")
                                        success_count += 1
                                    else:
                                        st.error(f"❌ {file.name}: {message}")
                                        error_count += 1
                                        
                                except Exception as e:
                                    print(f"[CLIENT] ERROR al subir '{file.name}': {e}")
                                    st.error(f"❌ Error al subir '{file.name}': {e}")
                                    error_count += 1
                            
                            # Limpiar contenedores de progreso
                            progress_container.empty()
                            status_container.empty()
                            
                            # Mostrar resumen
                            if success_count > 0:
                                if success_count == len(uploaded_files):
                                    st.success(f"✅ Todos los archivos ({success_count}) subidos correctamente.")
                                else:
                                    st.warning(f"⚠️ {success_count} de {len(uploaded_files)} archivos subidos correctamente. {error_count} fallaron.")
                                # Cerrar el modal y refrescar la lista de archivos
                                time.sleep(1)  # Dar tiempo para ver el mensaje
                                st.session_state.modal = None
                                st.session_state.refresh_needed = True
                                st.rerun()
                            # Si todos fallaron, mantener el modal abierto para que el usuario pueda intentar de nuevo
                            elif error_count > 0:
                                st.error(f"❌ No se pudo subir ningún archivo. Por favor, verifica la conexión e intenta de nuevo.")
        with colB:
            if st.button("🔄 Refrescar lista", key="refresh_list_button", use_container_width=True):
                st.session_state.refresh_needed = True
                st.rerun()
        with colC:
            if st.button("❌ Cerrar", key="close_modal_button", use_container_width=True):
                st.session_state.modal = None
                st.rerun()

# --- Modal: Agregar etiquetas ---
elif st.session_state.modal == "add_tags":
    with st.expander("🔖 Agregar etiquetas a archivos", expanded=True):
        query_tags = st.text_input("Etiquetas a buscar:", key="query_tags_add")
        new_tags = st.text_input("Nuevas etiquetas a agregar:", key="new_tags_add")

        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("Agregar Etiqueta(s)", key="confirm_add_tags", disabled=not is_connected):
                if not query_tags.strip():
                    st.warning("Debes ingresar las etiquetas a buscar.")
                elif not new_tags.strip():
                    st.warning("Debes ingresar al menos una nueva etiqueta.")
                else:
                    import time
                    add_tags_start = time.time()
                    user = st.session_state.logged_in_user or "unknown"
                    print(f"[CLIENT] [ADD_TAGS] 🔖 Agregando etiquetas (usuario: {user})")
                    print(f"[CLIENT] [ADD_TAGS] 📋 Query tags: {query_tags}, Nuevas tags: {new_tags}")
                    
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        print(f"[CLIENT] [ADD_TAGS] ❌ No se pudo obtener líder: {error}")
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"query": query_tags, "new_tags": new_tags}
                        try:
                            print(f"[CLIENT] [ADD_TAGS] 📍 Líder obtenido: {leader_url}")
                            print(f"[CLIENT] [ADD_TAGS] 📤 Enviando petición a {leader_url}/add-tags con params={params}")
                            response = requests.post(
                                f"{leader_url}/add-tags",
                                params=params,
                                headers=get_auth_headers(),
                                timeout=30
                            )
                            response.raise_for_status()
                            data = response.json()
                            elapsed = time.time() - add_tags_start
                            if data.get("success"):
                                files_affected = data.get("files_affected", 0)
                                print(f"[CLIENT] [ADD_TAGS] ✅ Etiquetas agregadas exitosamente: {files_affected} archivos afectados (tiempo: {elapsed:.2f}s)")
                                st.success("Etiquetas agregadas correctamente.")
                            else:
                                print(f"[CLIENT] [ADD_TAGS] ⚠️  No se encontraron archivos que coincidan (tiempo: {elapsed:.2f}s)")
                                st.warning("No se encontraron archivos que coincidan.")
                        except requests.HTTPError as e:
                            elapsed = time.time() - add_tags_start
                            status_code = e.response.status_code if e.response else "unknown"
                            print(f"[CLIENT] [ADD_TAGS] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                        except requests.RequestException as e:
                            elapsed = time.time() - add_tags_start
                            print(f"[CLIENT] [ADD_TAGS] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                    st.session_state.modal = None
                    st.session_state.refresh_needed = True
                    st.rerun()

# --- Modal: Eliminar etiquetas ---
elif st.session_state.modal == "del_tags":
    with st.expander("🔖 Eliminar etiquetas", expanded=True):
        query_tags = st.text_input("Etiquetas a buscar:", key="query_tags_del")
        del_tags = st.text_input("Etiquetas a eliminar:", key="del_tags_del")

        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("Eliminar Etiqueta(s)", key="confirm_del_tags", disabled=not is_connected):
                if not query_tags.strip():
                    st.warning("Debes ingresar las etiquetas a buscar.")
                elif not del_tags.strip():
                    st.warning("Debes ingresar las etiquetas que deseas eliminar.")
                else:
                    import time
                    del_tags_start = time.time()
                    user = st.session_state.logged_in_user or "unknown"
                    print(f"[CLIENT] [DELETE_TAGS] 🔖 Eliminando etiquetas (usuario: {user})")
                    print(f"[CLIENT] [DELETE_TAGS] 📋 Query tags: {query_tags}, Tags a eliminar: {del_tags}")
                    
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        print(f"[CLIENT] [DELETE_TAGS] ❌ No se pudo obtener líder: {error}")
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"query": query_tags, "del_tags": del_tags}
                        try:
                            print(f"[CLIENT] [DELETE_TAGS] 📍 Líder obtenido: {leader_url}")
                            print(f"[CLIENT] [DELETE_TAGS] 📤 Enviando petición a {leader_url}/delete-tags con params={params}")
                            response = requests.post(
                                f"{leader_url}/delete-tags",
                                params=params,
                                headers=get_auth_headers(),
                                timeout=30
                            )
                            response.raise_for_status()
                            data = response.json()
                            elapsed = time.time() - del_tags_start
                            if data.get("success"):
                                files_affected = data.get("files_affected", 0)
                                print(f"[CLIENT] [DELETE_TAGS] ✅ Etiquetas eliminadas exitosamente: {files_affected} archivos afectados (tiempo: {elapsed:.2f}s)")
                                st.success("Etiquetas eliminadas correctamente.")
                            else:
                                print(f"[CLIENT] [DELETE_TAGS] ⚠️  No se encontraron archivos que coincidan (tiempo: {elapsed:.2f}s)")
                                st.warning("No se encontraron archivos que coincidan.")
                        except requests.HTTPError as e:
                            elapsed = time.time() - del_tags_start
                            status_code = e.response.status_code if e.response else "unknown"
                            print(f"[CLIENT] [DELETE_TAGS] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                        except requests.RequestException as e:
                            elapsed = time.time() - del_tags_start
                            print(f"[CLIENT] [DELETE_TAGS] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                    st.session_state.modal = None
                    st.session_state.refresh_needed = True
                    st.rerun()

# --- Modal: Eliminar archivos ---
elif st.session_state.modal == "del_files":
    with st.expander("🗑️ Eliminar archivos", expanded=True):
        tags = st.text_input("Etiquetas de los archivos a eliminar:", key="del_files_tags")

        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("Eliminar Archivo(s)", key="confirm_del_files", disabled=not is_connected):
                if not tags.strip():
                    st.warning("Debes ingresar las etiquetas de los archivos que deseas eliminar.")
                else:
                    import time
                    del_files_start = time.time()
                    user = st.session_state.logged_in_user or "unknown"
                    print(f"[CLIENT] [DELETE_FILES] 🗑️  Eliminando archivos (usuario: {user})")
                    print(f"[CLIENT] [DELETE_FILES] 📋 Tags: {tags}")
                    
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        print(f"[CLIENT] [DELETE_FILES] ❌ No se pudo obtener líder: {error}")
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"tags": tags}
                        try:
                            print(f"[CLIENT] [DELETE_FILES] 📍 Líder obtenido: {leader_url}")
                            print(f"[CLIENT] [DELETE_FILES] 📤 Enviando petición DELETE a {leader_url}/delete con params={params}")
                            response = requests.delete(
                                f"{leader_url}/delete",
                                params=params,
                                headers=get_auth_headers(),
                                timeout=60
                            )
                            response.raise_for_status()
                            data = response.json()
                            elapsed = time.time() - del_files_start
                            if data.get("success"):
                                files_deleted = data.get("files_deleted", 0)
                                print(f"[CLIENT] [DELETE_FILES] ✅ Archivos eliminados exitosamente: {files_deleted} archivos (tiempo: {elapsed:.2f}s)")
                                st.success("Archivos eliminados correctamente.")
                            else:
                                print(f"[CLIENT] [DELETE_FILES] ⚠️  No se encontraron archivos con esas etiquetas (tiempo: {elapsed:.2f}s)")
                                st.warning("No se encontraron archivos con esas etiquetas.")
                        except requests.HTTPError as e:
                            elapsed = time.time() - del_files_start
                            status_code = e.response.status_code if e.response else "unknown"
                            print(f"[CLIENT] [DELETE_FILES] ❌ Error HTTP {status_code}: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                        except requests.RequestException as e:
                            elapsed = time.time() - del_files_start
                            print(f"[CLIENT] [DELETE_FILES] ❌ Error de conexión: {e} (tiempo: {elapsed:.2f}s)")
                            st.error(f"Error: {e}")
                    st.session_state.modal = None
                    st.session_state.refresh_needed = True
                    st.rerun()