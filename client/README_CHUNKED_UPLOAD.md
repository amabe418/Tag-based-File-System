# 📦 Chunked Upload - Guía de Uso

## 🎯 ¿Qué es Chunked Upload?

**Chunked Upload** es un sistema de transferencia de archivos que divide archivos grandes en **bloques pequeños** (chunks) para:

✅ **Reanudar uploads interrumpidos** - Si se corta la conexión, continúa desde donde quedó  
✅ **Reducir uso de memoria** - Solo carga 10 MB a la vez, no todo el archivo  
✅ **Verificar integridad** - Cada chunk se verifica con hash SHA-256  
✅ **Ver progreso en tiempo real** - Sabes exactamente cuánto se ha subido  
✅ **Manejar archivos muy grandes** - GB o TB sin problemas  

---

## 🚀 Inicio Rápido

### 1. Instalar dependencias

```bash
pip install requests
```

### 2. Login

```bash
python client/chunked_client.py login --username admin --password admin123
```

Guarda el token que te devuelve:

```bash
export TBFS_TOKEN='eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...'
```

### 3. Subir archivo

```bash
# Archivo pequeño (< 50 MB)
python client/chunked_client.py --token $TBFS_TOKEN upload-legacy documento.pdf --tags trabajo,importante

# Archivo grande (> 50 MB) con chunked upload
python client/chunked_client.py --token $TBFS_TOKEN upload video.mp4 --tags video,vacaciones
```

---

## 📖 Comandos Disponibles

### `login` - Iniciar sesión

```bash
python client/chunked_client.py login --username <usuario> --password <contraseña>
```

**Ejemplo:**
```bash
python client/chunked_client.py login --username admin --password admin123
```

**Salida:**
```
✅ Login exitoso: admin
🔑 Token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

### `upload` - Subir con chunked upload

```bash
python client/chunked_client.py --token $TBFS_TOKEN upload <archivo> --tags <etiquetas> [opciones]
```

**Opciones:**
- `--chunk-size <bytes>`: Tamaño de chunk (default: 10 MB = 10485760 bytes)
- `--no-resume`: No reanudar uploads interrumpidos

**Ejemplos:**

```bash
# Subir video con chunks de 10 MB (default)
python client/chunked_client.py --token $TBFS_TOKEN upload video_4K.mp4 --tags video,vacaciones

# Subir archivo con chunks de 5 MB
python client/chunked_client.py --token $TBFS_TOKEN upload archivo_grande.zip --tags backup --chunk-size 5242880

# Subir ISO con chunks de 50 MB
python client/chunked_client.py --token $TBFS_TOKEN upload ubuntu.iso --tags sistema,linux --chunk-size 52428800
```

**Progreso en tiempo real:**
```
📤 Subiendo: video_4K.mp4 (2,147,483,648 bytes)
📦 Tamaño de chunk: 10,485,760 bytes
🔐 Calculando hash del archivo...
🔐 Hash: sha256:abc123def456...
🚀 Iniciando sesión de upload...
✅ Sesión creada: 550e8400-e29b-41d4-a716-446655440000
📊 Total de chunks: 205

📤 Progreso: 50/205 (24.4%) - 8.52 MB/s
📤 Progreso: 100/205 (48.8%) - 9.13 MB/s
📤 Progreso: 150/205 (73.2%) - 8.97 MB/s
📤 Progreso: 205/205 (100.0%) - 9.01 MB/s

🔧 Ensamblando archivo y enviando a DataNodes...
✅ ¡Upload completado!
📁 Archivo: video_4K.mp4
🔢 File ID: 42
🔐 Hash: abc123def456...
💾 Réplicas: 3/3
⏱️  Tiempo total: 238.5s (8.59 MB/s promedio)
```

---

### `upload-legacy` - Subir con método tradicional

Para archivos pequeños (< 50 MB) o compatibilidad:

```bash
python client/chunked_client.py --token $TBFS_TOKEN upload-legacy <archivo> --tags <etiquetas>
```

**Ejemplo:**
```bash
python client/chunked_client.py --token $TBFS_TOKEN upload-legacy documento.pdf --tags trabajo,importante
```

---

### `list-uploads` - Ver uploads activos

```bash
python client/chunked_client.py --token $TBFS_TOKEN list-uploads
```

**Salida:**
```
📋 Uploads activos (2):
  • video_4K.mp4
    ID: 550e8400-e29b-41d4-a716-446655440000
    Progreso: 120/205 chunks (58.5%)

  • backup.tar.gz
    ID: 660f9511-f30c-52e5-b827-557766551111
    Progreso: 45/89 chunks (50.6%)
```

---

### `cancel-upload` - Cancelar upload

```bash
python client/chunked_client.py --token $TBFS_TOKEN cancel-upload <upload_id>
```

**Ejemplo:**
```bash
python client/chunked_client.py --token $TBFS_TOKEN cancel-upload 550e8400-e29b-41d4-a716-446655440000
```

---

## 🔄 Reanudar Uploads Interrumpidos

Si el upload se interrumpe (red, apagón, Ctrl+C), **simplemente ejecuta el mismo comando de nuevo**:

```bash
# Primera vez (se interrumpe en 50%)
python client/chunked_client.py --token $TBFS_TOKEN upload video.mp4 --tags video
# ... se interrumpe ...

# Segunda vez (continúa desde 50%)
python client/chunked_client.py --token $TBFS_TOKEN upload video.mp4 --tags video
```

**Salida:**
```
🔄 Reanudando: 102/205 chunks ya subidos
📤 Progreso: 103/205 (50.2%) - 9.12 MB/s
```

El sistema **detecta automáticamente** qué chunks ya se subieron y solo envía los faltantes.

---

## 🎛️ Configuración Avanzada

### Tamaño de Chunk Óptimo

| Tipo de Archivo | Tamaño Recomendado | Bytes | Comando |
|-----------------|-------------------|-------|---------|
| Documentos pequeños | Legacy (sin chunks) | - | `upload-legacy` |
| Videos/Imágenes | 10 MB (default) | 10485760 | `--chunk-size 10485760` |
| Archivos grandes | 50 MB | 52428800 | `--chunk-size 52428800` |
| ISOs/Backups | 100 MB | 104857600 | `--chunk-size 104857600` |

**Factores a considerar:**
- ⚡ **Más pequeño** = Más rápido reanudar, más overhead
- 🚀 **Más grande** = Menos overhead, más memoria, más lento reanudar

---

## 🔒 Seguridad

### Verificación de Integridad

Cada chunk y el archivo completo se verifican con **SHA-256**:

1. ✅ Cliente calcula hash del chunk → Envía al servidor
2. ✅ Servidor verifica hash del chunk → Acepta solo si coincide
3. ✅ Servidor ensambla archivo → Verifica hash completo
4. ❌ Si cualquier hash no coincide → Rechaza el archivo

### Autenticación

Todas las operaciones requieren **token JWT**:

```bash
# Opción 1: Variable de entorno
export TBFS_TOKEN='...'
python client/chunked_client.py --token $TBFS_TOKEN upload ...

# Opción 2: Argumento directo
python client/chunked_client.py --token 'eyJhbGc...' upload ...
```

---

## 🐛 Troubleshooting

### Error: "Se requiere autenticación"

**Problema:** No se proporcionó token

**Solución:**
```bash
# 1. Hacer login
python client/chunked_client.py login --username admin --password admin123

# 2. Copiar el token y guardarlo
export TBFS_TOKEN='...'

# 3. Usar el token
python client/chunked_client.py --token $TBFS_TOKEN upload archivo.mp4 --tags video
```

---

### Error: "Hash del chunk no coincide"

**Problema:** El chunk se corrompió durante la transferencia

**Solución:** El sistema **reintenta automáticamente** (hasta 3 veces). Si falla:

```bash
# Reintentar el upload (se reanudará automáticamente)
python client/chunked_client.py --token $TBFS_TOKEN upload archivo.mp4 --tags video
```

---

### Error: "Upload incompleto: faltan X chunks"

**Problema:** No se subieron todos los chunks antes de finalizar

**Solución:**
```bash
# Reanudar el upload
python client/chunked_client.py --token $TBFS_TOKEN upload archivo.mp4 --tags video
```

---

### Upload muy lento

**Problema:** Tamaño de chunk muy pequeño o conexión lenta

**Soluciones:**

```bash
# 1. Aumentar tamaño de chunk
python client/chunked_client.py --token $TBFS_TOKEN upload archivo.mp4 --tags video --chunk-size 52428800

# 2. Verificar conexión al servidor
ping <servidor>
curl http://<servidor>:8010/

# 3. Verificar recursos del sistema
# - ¿Hay suficiente RAM?
# - ¿Hay suficiente espacio en disco?
```

---

## 📊 Comparación: Chunked vs Legacy

| Característica | Chunked Upload | Legacy Upload |
|---------------|---------------|---------------|
| **Tamaño máximo** | Ilimitado (GB/TB) | ~100 MB (depende RAM) |
| **Uso de RAM** | 10 MB (chunk) | Todo el archivo |
| **Reanudar** | ✅ Sí | ❌ No |
| **Progreso** | ✅ Tiempo real | ❌ Sin feedback |
| **Verificación** | ✅ Por chunk + total | ✅ Solo total |
| **Velocidad** | Similar | Similar |
| **Complejidad** | Media | Baja |
| **Mejor para** | Archivos > 50 MB | Archivos < 50 MB |

---

## 🎓 Ejemplos de Uso Real

### Subir backup de base de datos (500 MB)

```bash
python client/chunked_client.py --token $TBFS_TOKEN upload database_backup.sql.gz \
  --tags backup,database,postgres \
  --chunk-size 20971520  # 20 MB chunks
```

### Subir colección de videos (varios archivos)

```bash
#!/bin/bash
export TBFS_TOKEN='...'

for video in *.mp4; do
  echo "Subiendo: $video"
  python client/chunked_client.py --token $TBFS_TOKEN upload "$video" --tags video,vacaciones,2024
done
```

### Subir ISO de sistema operativo (4 GB)

```bash
python client/chunked_client.py --token $TBFS_TOKEN upload ubuntu-22.04-desktop-amd64.iso \
  --tags sistema,linux,ubuntu \
  --chunk-size 104857600  # 100 MB chunks
```

---

## 🔗 API Endpoints

Si quieres implementar tu propio cliente, los endpoints son:

### 1. Iniciar sesión de upload
```http
POST /upload/init
Content-Type: multipart/form-data

filename=video.mp4
file_hash=sha256:abc123...
file_size=1048576000
tags=video,vacaciones
chunk_size=10485760
```

**Respuesta:**
```json
{
  "upload_id": "550e8400-e29b-41d4-a716-446655440000",
  "total_chunks": 100,
  "chunk_size": 10485760
}
```

### 2. Subir chunk
```http
POST /upload/{upload_id}/chunk/{chunk_index}
Content-Type: multipart/form-data

chunk=<binary_data>
chunk_hash=def456...
```

**Respuesta:**
```json
{
  "success": true,
  "chunk_index": 42,
  "progress_percentage": 42.5
}
```

### 3. Finalizar upload
```http
POST /upload/{upload_id}/finalize
```

**Respuesta:**
```json
{
  "success": true,
  "file_id": 123,
  "file_hash": "abc123...",
  "replicas": ["datanode-1", "datanode-2", "datanode-3"],
  "replicas_stored": 3
}
```

### 4. Obtener estado
```http
GET /upload/{upload_id}/status
```

**Respuesta:**
```json
{
  "upload_id": "550e8400-e29b-41d4-a716-446655440000",
  "filename": "video.mp4",
  "total_chunks": 100,
  "uploaded_chunks": [0, 1, 2, ..., 42],
  "progress_percentage": 43.0,
  "is_complete": false
}
```

### 5. Cancelar upload
```http
DELETE /upload/{upload_id}
```

---

## 🤝 Contribuir

¿Encontraste un bug o quieres una nueva característica? Abre un issue o pull request!

---

## 📜 Licencia

Este proyecto es parte del sistema TBFS (Tag-Based File System).

