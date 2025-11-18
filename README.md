# 📂 Tag-Based File System (TBFS) - Versión Centralizada

Sistema de gestión de archivos basado en etiquetas que permite organizar, buscar y administrar archivos mediante un sistema de etiquetado flexible. Esta es la versión **centralizada** del proyecto, donde todos los archivos y metadatos se almacenan en un único servidor.

## 🎯 Características Principales

- **Gestión de archivos por etiquetas**: Organiza tus archivos mediante etiquetas en lugar de carpetas jerárquicas
- **Búsqueda avanzada**: Encuentra archivos usando consultas por etiquetas (operador AND)
- **Múltiples interfaces**:
  - 🌐 Interfaz web moderna con Streamlit
  - 💻 Interfaz de línea de comandos (CLI)
  - 🔌 API REST completa con FastAPI
- **Gestión flexible de etiquetas**: Agrega o elimina etiquetas a archivos existentes
- **Almacenamiento centralizado**: Todos los archivos se almacenan en un servidor único
- **Despliegue con Docker**: Fácil despliegue usando Docker Swarm

## 🏗️ Arquitectura

El sistema está compuesto por tres componentes principales:

```
┌─────────────┐
│   Cliente   │ (CLI o Web)
└──────┬──────┘
       │ HTTP/REST
       ▼
┌─────────────┐
│  API Server │ (FastAPI)
└──────┬──────┘
       │
       ├──► SQLite Database (Metadatos)
       └──► Storage/ (Archivos físicos)
```

### Componentes

- **Backend (API)**: Servidor FastAPI que expone endpoints REST para gestionar archivos y etiquetas
- **Base de datos**: SQLite que almacena metadatos de archivos, etiquetas y sus relaciones
- **Almacenamiento**: Directorio `storage/` donde se guardan físicamente los archivos
- **Frontend Web**: Interfaz gráfica desarrollada con Streamlit
- **CLI**: Script Python para interacción desde terminal

## 📁 Estructura del Proyecto

```
Tag-based-File-System/
├── server/                 # Backend API
│   ├── api.py             # Endpoints FastAPI
│   ├── core/              # Lógica de negocio
│   │   ├── database.py    # Gestión de base de datos
│   │   ├── manager.py     # Operaciones con archivos y etiquetas
│   │   └── utils.py       # Utilidades
│   └── dockerfile.yml     # Dockerfile del backend
├── Client/                # Cliente (CLI y Web)
│   ├── main.py           # CLI
│   ├── web.py            # Interfaz web Streamlit
│   └── dockerfile.yml    # Dockerfile del frontend
├── database/             # Base de datos SQLite
│   └── db.db
├── storage/              # Almacenamiento de archivos
├── uploads/              # Archivos temporales
├── documentation/        # Documentación del proyecto
├── run_local.sh         # Script para ejecutar localmente
└── run_swarm.sh         # Script para desplegar con Docker Swarm
```

## 📋 Requisitos

- Python 3.12 o superior
- pip (gestor de paquetes de Python)
- Docker y Docker Swarm (opcional, para despliegue en contenedores)

### Dependencias Python

- **Backend**: `fastapi`, `uvicorn`, `python-multipart`, `requests`
- **Frontend**: `streamlit`, `requests`, `pandas`
- **CLI**: `requests`

## 🚀 Instalación

### Instalación Local

1. **Clonar el repositorio**:
```bash
git clone <url-del-repositorio>
cd Tag-based-File-System
```

2. **Crear entorno virtual** (recomendado):
```bash
python3 -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
```

3. **Instalar dependencias**:
```bash
pip install fastapi uvicorn streamlit requests pandas python-multipart
```

4. **Inicializar la base de datos**:
La base de datos se inicializa automáticamente al ejecutar el servidor por primera vez.

## 💻 Uso

### Ejecución Local

Para ejecutar el sistema localmente (servidor API + interfaz web):

```bash
chmod +x run_local.sh
./run_local.sh
```

Esto iniciará:
- **API Server**: `http://127.0.0.1:8000`
- **Interfaz Web**: `http://localhost:8501`

### Interfaz de Línea de Comandos (CLI)

El CLI permite interactuar con el sistema desde la terminal:

#### Agregar archivos con etiquetas
```bash
python Client/main.py add archivo1.txt,archivo2.pdf etiqueta1,etiqueta2,importante
```

#### Listar archivos
```bash
# Listar todos los archivos
python Client/main.py list

# Filtrar por etiquetas
python Client/main.py list etiqueta1 etiqueta2
```

#### Eliminar archivos
```bash
python Client/main.py delete etiqueta1,etiqueta2
```

#### Agregar etiquetas a archivos existentes
```bash
python Client/main.py add-tags etiqueta1 nueva_etiqueta,otra_etiqueta
```

#### Eliminar etiquetas de archivos
```bash
python Client/main.py delete-tags etiqueta1 etiqueta_a_eliminar
```

#### Descargar archivos
```bash
python Client/main.py download nombre_archivo.txt carpeta_destino/
```

### Interfaz Web

Accede a `http://localhost:8501` para usar la interfaz gráfica. La interfaz web ofrece:

- 📤 **Subir archivos** con etiquetas
- 📋 **Listar y buscar** archivos por etiquetas
- 🔖 **Gestionar etiquetas** (agregar/eliminar)
- 🗑️ **Eliminar archivos**
- 📥 **Descargar archivos** (individual o múltiple)
- 📄 **Paginación** para grandes volúmenes de archivos

### API REST

El servidor expone los siguientes endpoints:

#### `GET /`
Verifica el estado del servidor.

#### `POST /add`
Sube un archivo con etiquetas.
```bash
curl -X POST "http://127.0.0.1:8000/add" \
  -F "file=@archivo.txt" \
  -F "tags=etiqueta1,etiqueta2"
```

#### `GET /list`
Lista archivos, opcionalmente filtrados por etiquetas.
```bash
# Todos los archivos
curl "http://127.0.0.1:8000/list"

# Filtrar por etiquetas
curl "http://127.0.0.1:8000/list?tags=etiqueta1&tags=etiqueta2"
```

#### `DELETE /delete`
Elimina archivos que coincidan con las etiquetas especificadas.
```bash
curl -X DELETE "http://127.0.0.1:8000/delete?tags=etiqueta1,etiqueta2"
```

#### `POST /add-tags`
Agrega etiquetas a archivos existentes.
```bash
curl -X POST "http://127.0.0.1:8000/add-tags?query=etiqueta1&new_tags=nueva1,nueva2"
```

#### `POST /delete-tags`
Elimina etiquetas de archivos.
```bash
curl -X POST "http://127.0.0.1:8000/delete-tags?query=etiqueta1&del_tags=etiqueta_a_eliminar"
```

#### `GET /download/{file_name}`
Descarga un archivo por nombre.
```bash
curl "http://127.0.0.1:8000/download/archivo.txt" -o archivo.txt
```

**Documentación interactiva**: Accede a `http://127.0.0.1:8000/docs` para ver la documentación interactiva de la API (Swagger UI).

## 🐳 Despliegue con Docker

### Docker Swarm

Para desplegar el sistema usando Docker Swarm:

```bash
chmod +x run_swarm.sh
./run_swarm.sh
```

Este script:
1. Construye las imágenes Docker del backend y frontend
2. Inicializa Docker Swarm
3. Despliega los servicios en un stack

**Nota**: Asegúrate de tener un archivo `docker-compose.yml` configurado para el despliegue.

### Variables de Entorno

- `API_URL`: URL del servidor API (por defecto: `http://127.0.0.1:8000`)
- `DOWNLOAD_DIR`: Directorio para descargas en el cliente (por defecto: `downloads/`)

## 🗄️ Base de Datos

El sistema utiliza SQLite con el siguiente esquema:

### Tablas

- **`files`**: Almacena información de archivos (id, name, path)
- **`tags`**: Almacena etiquetas únicas (id, tag)
- **`file_tags`**: Tabla de relación muchos-a-muchos entre archivos y etiquetas

### Relaciones

- Un archivo puede tener múltiples etiquetas
- Una etiqueta puede pertenecer a múltiples archivos
- La relación se modela mediante `file_tags`

## 🔍 Búsqueda por Etiquetas

El sistema utiliza operador **AND** para las búsquedas:
- Si especificas `etiqueta1` y `etiqueta2`, solo se mostrarán archivos que tengan **ambas** etiquetas.
- Si no especificas etiquetas, se mostrarán todos los archivos.

## 📝 Notas Importantes

- **Espacios en etiquetas**: Las etiquetas no pueden contener espacios. Usa guiones bajos (`_`) o guiones (`-`) en su lugar.
- **Nombres de archivos únicos**: Cada archivo debe tener un nombre único en el sistema.
- **Almacenamiento**: Los archivos se almacenan en `storage/` con el formato `{id}_{nombre_original}`.
- **Eliminación de etiquetas**: No se puede eliminar la última etiqueta de un archivo (cada archivo debe tener al menos una etiqueta).

## 🛠️ Desarrollo

### Estructura de Módulos

- **`server/core/database.py`**: Gestión de conexiones y esquema de base de datos
- **`server/core/manager.py`**: Lógica de negocio para operaciones con archivos
- **`server/api.py`**: Endpoints REST de la API
- **`Client/main.py`**: CLI del cliente
- **`Client/web.py`**: Interfaz web Streamlit

### Ejecutar en Modo Desarrollo

```bash
# Terminal 1: Servidor API
cd server
uvicorn api:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: Interfaz Web
cd Client
streamlit run web.py
```

## 📚 Documentación Adicional

Consulta la carpeta `documentation/` para documentación técnica detallada:
- `Documentacion.md`: Documentación técnica completa
- `documentacion TBFS.pdf`: Documentación de parte del proceso de la construcción del proyecto.
- `tag-based-file-system.pdf`: Documentación de la orden del proyecto.

## 🤝 Contribución

Las contribuciones son bienvenidas. Por favor:

1. Crea un fork del proyecto
2. Crea una rama para tu feature (`git checkout -b feature/AmazingFeature`)
3. Commit tus cambios (`git commit -m 'Add some AmazingFeature'`)
4. Push a la rama (`git push origin feature/AmazingFeature`)
5. Abre un Pull Request


## 👥 Autores

-  Amalia Beatriz Valiente Hinojosa.
- Jorge Alejandro Echevarría Brunet.

---

**Versión**: Centralizada  
**Rama**: `centralized`  
**Última actualización**: 2025

