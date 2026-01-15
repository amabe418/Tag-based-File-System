#!/bin/bash
# Obtener el directorio del script y cambiar al directorio raíz del proyecto
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# Cambiar al directorio raíz del proyecto
cd "$PROJECT_ROOT"

echo "📁 Directorio de trabajo: $(pwd)"
echo ""

# Verificar que estamos en el directorio correcto
if [ ! -d "namenode" ] || [ ! -d "datanode" ] || [ ! -d "client" ] || [ ! -d "security" ]; then
    echo "❌ Error: No se encontraron los directorios necesarios (namenode, datanode, client, security)"
    echo "   Asegúrate de ejecutar este script desde el directorio raíz del proyecto"
    exit 1
fi

# Mostrar ayuda
show_help() {
    echo "Uso: $0 [opciones]"
    echo ""
    echo "Opciones:"
    echo "  -r, --rebuild    Forzar reconstrucción de todas las imágenes"
    echo "  -c, --code       Solo reiniciar contenedores (para cambios en código)"
    echo "  -h, --help       Mostrar esta ayuda"
    echo ""
    echo "Ejemplos:"
    echo "  $0              # Construir imágenes si no existen y levantar contenedores"
    echo "  $0 --rebuild    # Forzar reconstrucción de imágenes"
    echo "  $0 --code       # Solo reiniciar contenedores (cambios en código montado)"
}

# Crear red si no existe
docker network create tbfs_net 2>/dev/null || true

# Verificar opciones
FORCE_REBUILD=false
CODE_ONLY=false

case "$1" in
    -r|--rebuild)
        FORCE_REBUILD=true
        echo "⚠️  Modo de reconstrucción forzada activado"
        ;;
    -c|--code)
        CODE_ONLY=true
        echo "🔄 Modo cambios de código: solo reiniciando contenedores"
        ;;
    -h|--help)
        show_help
        exit 0
        ;;
    "")
        # Sin argumentos, comportamiento normal
        ;;
    *)
        echo "❌ Opción desconocida: $1"
        show_help
        exit 1
        ;;
esac

# Función para construir imagen solo si no existe o si se fuerza
build_if_needed() {
    local image_name=$1
    local dockerfile_path=$2
    local service_name=$3
    
    if [ "$CODE_ONLY" = true ]; then
        echo "⏭️  Saltando construcción de $service_name (modo --code)"
        return 0
    fi
    
    if [ "$FORCE_REBUILD" = true ] || ! docker image inspect "$image_name" >/dev/null 2>&1; then
        echo "🔨 Construyendo imagen de $service_name..."
        echo "   Dockerfile: $dockerfile_path"
        echo "   Contexto: $(pwd)"
        docker build -t "$image_name" -f "$dockerfile_path" .
        echo "✅ Imagen $image_name construida"
    else
        echo "✅ Imagen $service_name ya existe, omitiendo construcción (usa --rebuild para forzar)"
    fi
}

# Construir imágenes solo si no existen (sin Registry)
build_if_needed "tbfs-namenode" "namenode/dockerfile.yml" "MetaNameNode"
build_if_needed "tbfs-datanode" "datanode/dockerfile.yml" "DataNode"
build_if_needed "tbfs-frontend" "client/dockerfile.yml" "Frontend"

# Detener contenedores existentes si existen
echo ""
echo "🛑 Deteniendo contenedores existentes..."
docker stop tbfs-namenode-1 tbfs-namenode-2 tbfs-namenode-3 \
  tbfs-datanode-1 tbfs-datanode-2 tbfs-datanode-3 tbfs-datanode-4 tbfs-datanode-5 \
  tbfs-frontend 2>/dev/null || true
docker rm tbfs-namenode-1 tbfs-namenode-2 tbfs-namenode-3 \
  tbfs-datanode-1 tbfs-datanode-2 tbfs-datanode-3 tbfs-datanode-4 tbfs-datanode-5 \
  tbfs-frontend 2>/dev/null || true

# Obtener directorio raíz del proyecto para montar código como volumen
# Esto permite cambios en el código sin reconstruir las imágenes Docker
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Preparar volúmenes de código para cada servicio
# Los cambios en el código se reflejarán inmediatamente sin reconstruir imágenes
NAMENODE_CODE_VOLUMES=()
DATANODE_CODE_VOLUMES=()
FRONTEND_CODE_VOLUMES=()

if [ -d "$PROJECT_ROOT/namenode" ] && [ -d "$PROJECT_ROOT/security" ]; then
    NAMENODE_CODE_VOLUMES=(-v "$PROJECT_ROOT/namenode:/app/namenode" -v "$PROJECT_ROOT/security:/app/security")
    DATANODE_CODE_VOLUMES=(-v "$PROJECT_ROOT/datanode:/app/datanode" -v "$PROJECT_ROOT/security:/app/security")
    # El frontend copia archivos directamente a /app
    FRONTEND_CODE_VOLUMES=(-v "$PROJECT_ROOT/client:/app")
    echo "📁 Modo desarrollo: Montando código desde: $PROJECT_ROOT"
    echo "   Los cambios en el código se reflejarán sin reconstruir imágenes"
else
    echo "⚠️  Advertencia: No se encontraron directorios necesarios"
    echo "   Los volúmenes de código no se montarán"
fi

echo ""
echo "🚀 Iniciando contenedores..."
echo ""

# Ejecutar 3 nodos del MetaNameNode
# Los namenodes se descubren entre sí usando DNS de Docker (alias: namenode)
echo "Iniciando MetaNameNode - Nodo 1..."
docker run -d --name tbfs-namenode-1 --network tbfs_net --network-alias namenode -p 8010:8010 \
  -e NODE_ID=namenode-1 \
  -e NAMENODE_PORT=8010 \
  -e NAMENODE_SERVICE=namenode \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e HEARTBEAT_INTERVAL=10 \
  -v tbfs-namenode-1-data:/app/namenode/data \
  "${NAMENODE_CODE_VOLUMES[@]}" \
  tbfs-namenode

echo "Iniciando MetaNameNode - Nodo 2..."
docker run -d --name tbfs-namenode-2 --network tbfs_net --network-alias namenode -p 8011:8010 \
  -e NODE_ID=namenode-2 \
  -e NAMENODE_PORT=8010 \
  -e NAMENODE_SERVICE=namenode \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e HEARTBEAT_INTERVAL=10 \
  -v tbfs-namenode-2-data:/app/namenode/data \
  "${NAMENODE_CODE_VOLUMES[@]}" \
  tbfs-namenode

echo "Iniciando MetaNameNode - Nodo 3..."
docker run -d --name tbfs-namenode-3 --network tbfs_net --network-alias namenode -p 8012:8010 \
  -e NODE_ID=namenode-3 \
  -e NAMENODE_PORT=8010 \
  -e NAMENODE_SERVICE=namenode \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e HEARTBEAT_INTERVAL=10 \
  -v tbfs-namenode-3-data:/app/namenode/data \
  "${NAMENODE_CODE_VOLUMES[@]}" \
  tbfs-namenode

# Esperar un momento para que los MetaNameNodes se estabilicen
echo "Esperando que los MetaNameNodes se estabilicen..."
sleep 5

# Ejecutar 5 DataNodes
# Los datanodes se conectan a namenodes usando DNS de Docker
echo "Iniciando DataNode - Nodo 1..."
docker run -d --name tbfs-datanode-1 --network tbfs_net --network-alias datanode -p 8001:8001 \
  -e DATANODE_ID=datanode-1 \
  -e NODE_ID=tbfs-datanode-1 \
  -e DATANODE_PORT=8001 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_INTERVAL=10 \
  -e STORAGE_PATH=/app/storage \
  -v tbfs-datanode-1-storage:/app/storage \
  "${DATANODE_CODE_VOLUMES[@]}" \
  tbfs-datanode

echo "Iniciando DataNode - Nodo 2..."
docker run -d --name tbfs-datanode-2 --network tbfs_net --network-alias datanode -p 8002:8001 \
  -e DATANODE_ID=datanode-2 \
  -e NODE_ID=tbfs-datanode-2 \
  -e DATANODE_PORT=8001 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_INTERVAL=10 \
  -e STORAGE_PATH=/app/storage \
  -v tbfs-datanode-2-storage:/app/storage \
  "${DATANODE_CODE_VOLUMES[@]}" \
  tbfs-datanode

echo "Iniciando DataNode - Nodo 3..."
docker run -d --name tbfs-datanode-3 --network tbfs_net --network-alias datanode -p 8003:8001 \
  -e DATANODE_ID=datanode-3 \
  -e NODE_ID=tbfs-datanode-3 \
  -e DATANODE_PORT=8001 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_INTERVAL=10 \
  -e STORAGE_PATH=/app/storage \
  -v tbfs-datanode-3-storage:/app/storage \
  "${DATANODE_CODE_VOLUMES[@]}" \
  tbfs-datanode

echo "Iniciando DataNode - Nodo 4..."
docker run -d --name tbfs-datanode-4 --network tbfs_net --network-alias datanode -p 8004:8001 \
  -e DATANODE_ID=datanode-4 \
  -e NODE_ID=tbfs-datanode-4 \
  -e DATANODE_PORT=8001 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_INTERVAL=10 \
  -e STORAGE_PATH=/app/storage \
  -v tbfs-datanode-4-storage:/app/storage \
  "${DATANODE_CODE_VOLUMES[@]}" \
  tbfs-datanode

echo "Iniciando DataNode - Nodo 5..."
docker run -d --name tbfs-datanode-5 --network tbfs_net --network-alias datanode -p 8005:8001 \
  -e DATANODE_ID=datanode-5 \
  -e NODE_ID=tbfs-datanode-5 \
  -e DATANODE_PORT=8001 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_INTERVAL=10 \
  -e STORAGE_PATH=/app/storage \
  -v tbfs-datanode-5-storage:/app/storage \
  "${DATANODE_CODE_VOLUMES[@]}" \
  tbfs-datanode

# Ejecutar Frontend Service
echo "Iniciando Frontend Service..."
docker run -d --name tbfs-frontend --network tbfs_net --network-alias frontend -p 8501:8501 \
  -e NAMENODE_SERVICE=namenode \
  -e NAMENODE_PORT=8010 \
  -e DOWNLOAD_DIR=downloads \
  "${FRONTEND_CODE_VOLUMES[@]}" \
  tbfs-frontend

echo ""
echo "✅ Servicios iniciados:"
echo "  - MetaNameNode Nodo 1: http://localhost:8010"
echo "  - MetaNameNode Nodo 2: http://localhost:8011"
echo "  - MetaNameNode Nodo 3: http://localhost:8012"
echo "  - DataNode Nodo 1: http://localhost:8001"
echo "  - DataNode Nodo 2: http://localhost:8002"
echo "  - DataNode Nodo 3: http://localhost:8003"
echo "  - DataNode Nodo 4: http://localhost:8004"
echo "  - DataNode Nodo 5: http://localhost:8005"
echo "  - Frontend: http://localhost:8501"
echo ""
echo "Para ver los logs:"
echo "  docker logs -f tbfs-namenode-1"
echo "  docker logs -f tbfs-datanode-1"
echo "  docker logs -f tbfs-frontend"
echo ""
if [ "$CODE_ONLY" = true ]; then
    echo "💡 Contenedores reiniciados con los cambios de código aplicados"
fi
echo ""
