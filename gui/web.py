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

st.title("📂 Tag-based File System")

# --- Estado inicial ---
if "modal" not in st.session_state:
    st.session_state.modal = None
if "refresh_needed" not in st.session_state:
    st.session_state.refresh_needed = False  # fuerza recarga solo al confirmar una acción

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

# --- Mostrar lista ---
st.subheader("📖 Archivos disponibles")
tags_filter = st.text_input("Filtrar por etiquetas (separadas por comas):", key="tag_filter")

# Solo refrescamos la lista si se necesita
if st.session_state.refresh_needed:
    st.session_state.refresh_needed = False
    st.rerun()

# --- CSS para reducir el espacio entre columnas ---
st.markdown("""
    <style>
        /* General: menos espacio entre todas las columnas */
        [data-testid="column"] {
            padding-right: 0.15rem !important;
            padding-left: 0.15rem !important;
        }

        /* Reduce aún más el espacio antes de la última columna (botón Descargar) */
        [data-testid="column"]:last-child {
            padding-left: 0rem !important;
            margin-left: -0.3rem !important;
        }

        /* Reduce el espacio vertical entre filas */
        [data-testid="stVerticalBlock"] {
            gap: 0.25rem !important;
        }

        /* Opcional: reduce padding general del contenedor */
        .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
        }
    </style>
""", unsafe_allow_html=True)

files = refresh_list(tags_filter)

# --- Parámetros de paginación ---
ITEMS_PER_PAGE = 5
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

    df = pd.DataFrame([
        {"Nombre": f.get("name"), "Etiquetas": f.get("tags")}
        for f in visible_files
    ])

    # --- Encabezados ---
    col_widths = [7,5,3] 
    header_cols = st.columns(col_widths, gap="small")
    for i, col_name in enumerate(df.columns):
        header_cols[i].markdown(
            f"<div style='font-weight:bold; font-size:1.2rem; text-align:left;'>{col_name}</div>",
            unsafe_allow_html=True
        )
    header_cols[-1].markdown("")
    st.markdown("---")

    # --- Filas ---
    for idx, row in df.iterrows():
        row_cols = st.columns(col_widths)
        row_cols[0].markdown(row["Nombre"])
        row_cols[1].markdown(row["Etiquetas"])

        if row_cols[2].button("Descargar", key=f"dl_{row['Nombre']}"):
            try:
                r = requests.get(f"{API_URL}/download/{row['Nombre']}", stream=True)
                r.raise_for_status()
                download_path = os.path.join(DOWNLOAD_DIR, f"{row['Nombre']}")
                with open(download_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                st.success(f"Archivo descargado en {download_path}")
                st.re
            except requests.RequestException as e:
                st.error(f"No se pudo descargar {row['Nombre']}: {e}")
        st.markdown("---")

    # --- Controles de paginación ---
    st.markdown("<br>", unsafe_allow_html=True)
    col_empty, col_prev, col_page, col_next = st.columns([1, 0.3, 2, 1])
    st.markdown("<br>", unsafe_allow_html=True)


    with col_prev:
        if st.button("⬅️ Anterior", disabled=(st.session_state.current_page == 1)):
            st.session_state.current_page -= 1
            st.rerun()

    with col_page:
        st.markdown(
            f"<div style='text-align:center; font-weight:bold;'>Página {st.session_state.current_page} de {total_pages}</div>",
            unsafe_allow_html=True
        )

    with col_next:
        if st.button("Siguiente ➡️", disabled=(st.session_state.current_page == total_pages)):
            st.session_state.current_page += 1
            st.rerun()

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
                            st.rerun()
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