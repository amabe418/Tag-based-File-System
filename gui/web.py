import os
import requests
import streamlit as st
import pandas as pd
import math

API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__),"downloads/"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

print(f"Usando: {API_URL}")

st.set_page_config(page_title="Tag-based File System", layout="wide")
st.markdown("---")
st.title("📂 Tag-based File System")

# --- Estado inicial ---
if "modal" not in st.session_state:
    st.session_state.modal = None
if "refresh_needed" not in st.session_state:
    st.session_state.refresh_needed = False  # fuerza recarga solo al confirmar una acción
if "selected_files" not in st.session_state:
    st.session_state.selected_files = set()  # conjunto de nombres de archivos seleccionados
if "table_version" not in st.session_state:
    st.session_state.table_version = {}  # versión por página para forzar reset del widget

# --- Función para refrescar lista ---
def refresh_list(tags=None):
    try:
        params = {}
        if tags:
            params["tags"] = tags
        response = requests.get(f"{API_URL}/list", params=params)
        response.raise_for_status()
        data = response.json()
        return data.get("files", [])
    except requests.RequestException as e:
        st.error(f"No se pudo obtener la lista de archivos: {e}")
        return []

# --- Función para descargar archivo ---
def download_file(file_name):
    """Descarga un archivo individual"""
    try:
        r = requests.get(f"{API_URL}/download/{file_name}", stream=True)
        r.raise_for_status()
        download_path = os.path.join(DOWNLOAD_DIR, file_name)
        with open(download_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True, download_path
    except requests.RequestException as e:
        return False, str(e)

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
        select_all = col_select_all.form_submit_button("✅ Seleccionar todos", use_container_width=True)
        deselect_all = col_deselect_all.form_submit_button("❌ Deseleccionar todos", use_container_width=True)
        download_clicked = col_download_selected.form_submit_button("📥 Descargar seleccionados", use_container_width=True, type="primary")

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
                success_count = 0
                error_count = 0
                progress_bar = st.progress(0)
                status_text = st.empty()
                for idx, file_name in enumerate(selected_in_page):
                    status_text.text(f"Descargando {file_name}... ({idx + 1}/{len(selected_in_page)})")
                    success, result = download_file(file_name)
                    if success:
                        success_count += 1
                    else:
                        error_count += 1
                        st.error(f"Error al descargar '{file_name}': {result}")
                    progress_bar.progress((idx + 1) / len(selected_in_page))
                status_text.empty()
                progress_bar.empty()
                if success_count > 0:
                    st.success(f"✅ {success_count} archivo(s) descargado(s) correctamente en {DOWNLOAD_DIR}")
                if error_count > 0:
                    st.warning(f"⚠️ {error_count} archivo(s) fallaron al descargar")

    # --- Controles de paginación ---
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("<div class='pagination'>", unsafe_allow_html=True)
    col_prev, col_page, col_next = st.columns([1, 2, 1])
    
    with col_prev:
        if st.button("⬅️ Anterior", disabled=(st.session_state.current_page == 1), use_container_width=True):
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
        if st.button("Siguiente ➡️", disabled=(st.session_state.current_page == total_pages), use_container_width=True):
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
    if st.button("➕ Agregar archivo(s)", key="btn_add_file", use_container_width=True):
        st.session_state.modal = "add_file"
with col2:
    if st.button("🔖➕ Agregar etiqueta(s)", key="btn_add_tags", use_container_width=True):
        st.session_state.modal = "add_tags"
with col3:
    if st.button("🔖❌ Eliminar etiqueta(s)", key="btn_del_tags", use_container_width=True):
        st.session_state.modal = "del_tags"
with col4:
    if st.button("🗑️ Eliminar archivo(s)", key="btn_del_files", use_container_width=True):
        st.session_state.modal = "del_files"

# --- Modal: Agregar archivos ---
if st.session_state.modal == "add_file":
    with st.expander("📁 Subir nuevos archivos", expanded=True):
        uploaded_files = st.file_uploader("Selecciona archivos", accept_multiple_files=True, key="file_uploader")
        tags = st.text_input("Etiquetas (separadas por comas):", key="add_file_tags")

        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("Agregar Archivo(s)", key="upload_button"):
                if not uploaded_files:
                    st.warning("Selecciona al menos un archivo.")
                elif not tags.strip():
                    st.warning("Debes ingresar al menos una etiqueta.")
                else:
                    for file in uploaded_files:
                        files = {"file": (file.name, file.getvalue())}
                        data = {"tags": tags}
                        try:
                            response = requests.post(f"{API_URL}/add", files=files, data=data)
                            response.raise_for_status()
                            st.success(f"Archivo '{file.name}' subido correctamente.")
                            # st.rerun()
                        except requests.RequestException as e:
                            st.error(f"Error al subir '{file.name}': {e}")
                    st.session_state.modal = None
                    st.session_state.refresh_needed = True
                    st.rerun()

# --- Modal: Agregar etiquetas ---
elif st.session_state.modal == "add_tags":
    with st.expander("🔖 Agregar etiquetas a archivos", expanded=True):
        query_tags = st.text_input("Etiquetas a buscar:", key="query_tags_add")
        new_tags = st.text_input("Nuevas etiquetas a agregar:", key="new_tags_add")

        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("Agregar Etiqueta(s)", key="confirm_add_tags"):
                if not query_tags.strip():
                    st.warning("Debes ingresar las etiquetas a buscar.")
                elif not new_tags.strip():
                    st.warning("Debes ingresar al menos una nueva etiqueta.")
                else:
                    params = {"query": query_tags, "new_tags": new_tags}
                    try:
                        response = requests.post(f"{API_URL}/add-tags", params=params)
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
            if st.button("Eliminar Etiqueta(s)", key="confirm_del_tags"):
                if not query_tags.strip():
                    st.warning("Debes ingresar las etiquetas a buscar.")
                elif not del_tags.strip():
                    st.warning("Debes ingresar las etiquetas que deseas eliminar.")
                else:
                    params = {"query": query_tags, "del_tags": del_tags}
                    try:
                        response = requests.post(f"{API_URL}/delete-tags", params=params)
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
            if st.button("Eliminar Archivo(s)", key="confirm_del_files"):
                if not tags.strip():
                    st.warning("Debes ingresar las etiquetas de los archivos que deseas eliminar.")
                else:
                    params = {"tags": tags}
                    try:
                        response = requests.delete(f"{API_URL}/delete", params=params)
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