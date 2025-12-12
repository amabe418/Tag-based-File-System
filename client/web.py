import os
import requests
import streamlit as st
import pandas as pd
import math
import random
import time
from typing import Optional
from registry_client import registry_client

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__),"downloads/"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
            # Verificar formato de URL
            if server_url and not server_url.startswith("http"):
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
    server_url, error = get_server_url()
    if not server_url:
        return None, error
    
    try:
        # Consultar el endpoint / para obtener información del líder
        response = requests.get(f"{server_url}/", timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # Prioridad 1: Usar leader_url si está disponible (siempre debe estar si hay líder)
        leader_url = data.get("leader_url")
        if leader_url:
            print(f"[CLIENT] Líder encontrado desde leader_url: {leader_url}")
            # Verificar que el líder realmente es el líder consultando su endpoint /
            try:
                leader_response = requests.get(f"{leader_url}/", timeout=3)
                leader_response.raise_for_status()
                leader_data = leader_response.json()
                if leader_data.get("is_leader"):
                    return leader_url, None
                else:
                    print(f"[CLIENT] WARNING: {leader_url} reporta que no es el líder")
            except:
                pass  # Si falla la verificación, usar la URL de todos modos
            return leader_url, None
        
        # Prioridad 2: Si este namenode es el líder, usar su URL
        if data.get("is_leader"):
            print(f"[CLIENT] Namenode consultado es el líder: {server_url}")
            return server_url, None
        
        # Prioridad 3: Si hay leader_id pero no leader_url, construir la URL
        leader_id = data.get("leader_id")
        if leader_id:
            # Construir URL del líder basado en el leader_id
            # Si el leader_id es "namenode-1", la URL será "http://tbfs-namenode-1:8010"
            leader_url = f"http://tbfs-{leader_id}:8010"
            print(f"[CLIENT] Líder construido desde leader_id: {leader_url}")
            return leader_url, None
        
        # Si no hay líder disponible, retornar el servidor actual como fallback
        print(f"[CLIENT] No se pudo obtener líder, usando servidor actual: {server_url}")
        return server_url, None
        
    except requests.RequestException as e:
        print(f"[CLIENT] Error al consultar líder: {e}")
        return None, f"Error al consultar el líder: {e}"


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
    leader_url, error = get_leader_url()
    if not leader_url:
        return False, error or "No hay líder disponible"
    
    try:
        response = requests.post(
            f"{leader_url}/auth/login",
            json={"username": username, "password": password},
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        user = data.get("user", {}).get("username")
        
        # Guardar en session_state
        st.session_state.auth_token = token
        st.session_state.logged_in_user = user
        
        # Guardar en storage para persistencia (hacer esto ANTES del rerun)
        if token and user:
            save_to_storage(COOKIE_TOKEN_KEY, token)
            save_to_storage(COOKIE_USER_KEY, user)
            print(f"[CLIENT] Token y usuario guardados en storage: user={user}")
        
        return True, None
    except requests.RequestException as e:
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
                    su_username = su_username.strip().lower()
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        st.error(error or "No hay líder disponible")
                    else:
                        try:
                            resp = requests.post(
                                f"{leader_url}/auth/signup",
                                json={"username": su_username, "password": su_password},
                                timeout=5,
                            )
                            if resp.status_code == 400:
                                st.error("El usuario ya existe, elige otro nombre de usuario.")
                            resp.raise_for_status()
                            data = resp.json()
                            st.success("✅ Cuenta creada. Inicia sesión con tus credenciales.")
                            # Limpiar campos y cambiar a login
                            st.session_state.auth_mode = "login"
                            st.session_state.login_username = su_username
                            st.session_state.login_password = ""
                            st.rerun()
                        except requests.RequestException as e:
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
                        leader_url, error = get_leader_url()
                        if not leader_url:
                            st.error(error or "No hay líder disponible")
                        else:
                            try:
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
                                if data.get("success"):
                                    st.success("✅ Contraseña cambiada exitosamente")
                                    st.session_state.show_change_password = False
                                    st.rerun()
                                else:
                                    st.error("Error al cambiar la contraseña")
                            except requests.HTTPError as e:
                                if e.response and e.response.status_code == 400:
                                    st.error("❌ La contraseña actual es incorrecta")
                                else:
                                    st.error(f"Error: {e}")
                            except requests.RequestException as e:
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
    leader_url, _ = get_leader_url()
    if not leader_url:
        return []
    
    if not st.session_state.auth_token:
        return []
    
    try:
        params = {}
        if tags:
            params["tags"] = tags
        response = requests.get(
            f"{leader_url}/list",
            params=params,
            headers=get_auth_headers(),
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        return data.get("files", [])
    except requests.RequestException as e:
        # No mostrar error aquí, ya se muestra en el expander de conexión
        return []

# --- Función para obtener contenido de archivo ---
def get_file_content(file_name):
    """Obtiene el contenido de un archivo para descarga"""
    server_url, _ = get_server_url()
    if not server_url:
        return None, "No hay servidor disponible"
    
    try:
        # Obtener la URL del líder para la descarga
        leader_url, leader_error = get_leader_url()
        if not leader_url:
            return None, f"No se pudo obtener el líder: {leader_error}"
        
        # Intentar descargar desde el líder, siguiendo redirecciones automáticamente
        r = requests.get(
            f"{leader_url}/download/{file_name}", 
            stream=True, 
            timeout=30,
            headers=get_auth_headers(),
            allow_redirects=True  # Seguir redirecciones HTTP 307 automáticamente
        )
        r.raise_for_status()
        return r.content, None
    except requests.HTTPError as e:
        # Si es un error 503, puede ser que el namenode no sea el líder
        if e.response and e.response.status_code == 503:
            return None, f"Servicio no disponible. El namenode puede no ser el líder. Intenta de nuevo."
        return None, str(e)
    except requests.RequestException as e:
        return None, str(e)

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
                            print(f"[CLIENT] Intentando subir archivo(s) al líder: {leader_url}/add")
                            success_count = 0
                            error_count = 0
                            for file in uploaded_files:
                                files = {"file": (file.name, file.getvalue())}
                                data = {"tags": tags}
                                try:
                                    print(f"[CLIENT] Enviando POST a {leader_url}/add con archivo: {file.name}, tags: {tags}")
                                    response = requests.post(
                                        f"{leader_url}/add",
                                        files=files,
                                        data=data,
                                        headers=get_auth_headers(),
                                        timeout=30
                                    )
                                    print(f"[CLIENT] Respuesta recibida: status={response.status_code}, body={response.text[:200]}")
                                    response.raise_for_status()
                                    result = response.json()
                                    print(f"[CLIENT] Archivo subido exitosamente: {result}")
                                    st.success(f"Archivo '{file.name}' subido correctamente.")
                                    success_count += 1
                                except requests.RequestException as e:
                                    print(f"[CLIENT] ERROR al subir '{file.name}': {e}")
                                    print(f"[CLIENT] Tipo de error: {type(e)}")
                                    if hasattr(e, 'response') and e.response is not None:
                                        print(f"[CLIENT] Status code: {e.response.status_code}")
                                        print(f"[CLIENT] Response body: {e.response.text[:500]}")
                                    st.error(f"Error al subir '{file.name}': {e}")
                                    error_count += 1
                            
                            # Mostrar resumen
                            if success_count > 0:
                                if success_count == len(uploaded_files):
                                    st.success(f"✅ Todos los archivos ({success_count}) subidos correctamente.")
                                else:
                                    st.warning(f"⚠️ {success_count} de {len(uploaded_files)} archivos subidos correctamente. {error_count} fallaron.")
                                # Cerrar el modal y refrescar la lista de archivos
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
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"query": query_tags, "new_tags": new_tags}
                        try:
                            response = requests.post(
                                f"{leader_url}/add-tags",
                                params=params,
                                headers=get_auth_headers()
                            )
                            response.raise_for_status()
                            data = response.json()
                            if data.get("success"):
                                st.success("Etiquetas agregadas correctamente.")
                            else:
                                st.warning("No se encontraron archivos que coincidan.")
                        except requests.RequestException as e:
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
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"query": query_tags, "del_tags": del_tags}
                        try:
                            response = requests.post(
                                f"{leader_url}/delete-tags",
                                params=params,
                                headers=get_auth_headers()
                            )
                            response.raise_for_status()
                            data = response.json()
                            if data.get("success"):
                                st.success("Etiquetas eliminadas correctamente.")
                            else:
                                st.warning("No se encontraron archivos que coincidan.")
                        except requests.RequestException as e:
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
                    leader_url, error = get_leader_url()
                    if not leader_url:
                        st.error(error or "No hay líder disponible")
                    else:
                        params = {"tags": tags}
                        try:
                            response = requests.delete(
                                f"{leader_url}/delete",
                                params=params,
                                headers=get_auth_headers()
                            )
                            response.raise_for_status()
                            data = response.json()
                            if data.get("success"):
                                st.success("Archivos eliminados correctamente.")
                            else:
                                st.warning("No se encontraron archivos con esas etiquetas.")
                        except requests.RequestException as e:
                            st.error(f"Error: {e}")
                    st.session_state.modal = None
                    st.session_state.refresh_needed = True
                    st.rerun()