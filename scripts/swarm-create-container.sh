#!/bin/bash
# Script para crear un contenedor en la red overlay del Swarm
# Uso: ./swarm-create-container.sh <tipo> <numero> [opciones adicionales]
# Tipos: namenode, datanode, frontend

set -e

if [ $# -lt 2 ]; then
    echo "❌ Error: Debes proporcionar tipo y número"
    echo ""
    echo "Uso: ./swarm-create-container.sh <tipo> <numero> [opciones]"
    echo ""
    echo "Tipos disponibles:"
    echo "  namenode  - MetaNameNode (1-N)"
    echo "  datanode  - DataNode (1-N)"
    echo "  frontend  - Frontend (solo 1)"
    echo ""
    echo "Ejemplos:"
    echo "  ./swarm-create-container.sh datanode 6"
    echo "  ./swarm-create-container.sh namenode 4"
    exit 1
fi

TYPE=$1
NUM=$2
shift 2
EXTRA_ARGS=("$@")

# Verificar que estamos en un swarm
if ! docker info | grep -q "Swarm: active"; then
    echo "❌ Error: Docker Swarm no está inicializado"
    echo "   Ejecuta primero: ./swarm-init.sh (en el manager)"
    echo "   O únete a un swarm: ./swarm-join.sh <MANAGER_IP> <TOKEN>"
    exit 1
fi

# Verificar que la red overlay existe
if ! docker network ls | grep -q "tbfs_net"; then
    echo "❌ Error: Red overlay tbfs_net no existe"
    echo "   Ejecuta en el manager: docker network create --driver overlay --attachable tbfs_net"
    exit 1
fi

CONTAINER_NAME="tbfs-${TYPE}-${NUM}"

# Obtener directorio raíz del proyecto (un nivel arriba de scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Verificar que los directorios necesarios existen y preparar volúmenes de código
# Esto permite cambios en el código sin reconstruir las imágenes Docker
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
    echo "   Los volúmenes de código no se montarán. Asegúrate de ejecutar desde el directorio raíz del proyecto."
fi

# Verificar si el contenedor ya existe
if docker ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
    if docker ps --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
        echo "⚠️  El contenedor $CONTAINER_NAME ya está corriendo"
        exit 0
    else
        echo "🔄 Contenedor $CONTAINER_NAME existe pero está detenido, iniciando..."
        docker start "$CONTAINER_NAME"
        exit 0
    fi
fi

echo "📦 Creando contenedor: $CONTAINER_NAME"
echo ""

case $TYPE in
    namenode)
        PORT=$((8010 + NUM - 1))
        # Los namenodes se descubren entre sí usando DNS de Docker (alias: namenode)
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --network-alias namenode \
            --hostname "$CONTAINER_NAME" \
            -p "${PORT}:8010" \
            -v "tbfs-namenode-${NUM}-data:/app/namenode/data" \
            "${NAMENODE_CODE_VOLUMES[@]}" \
            -e NODE_ID="namenode-${NUM}" \
            -e NAMENODE_PORT=8010 \
            -e NAMENODE_SERVICE=namenode \
            -e HEARTBEAT_TIMEOUT=15 \
            -e LEADER_HEARTBEAT_INTERVAL=5 \
            -e ELECTION_TIMEOUT=15 \
            -e HEARTBEAT_INTERVAL=10 \
            "${EXTRA_ARGS[@]}" \
            tbfs-namenode:latest
        ;;
    
    datanode)
        PORT=$((8000 + NUM))
        # Los datanodes se conectan a namenodes usando DNS de Docker (alias: namenode, datanode)
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --network-alias datanode \
            --hostname "$CONTAINER_NAME" \
            -p "${PORT}:8001" \
            -v "tbfs-datanode-${NUM}-storage:/app/storage" \
            "${DATANODE_CODE_VOLUMES[@]}" \
            -e DATANODE_ID="datanode-${NUM}" \
            -e NODE_ID="$CONTAINER_NAME" \
            -e DATANODE_PORT=8001 \
            -e NAMENODE_SERVICE=namenode \
            -e NAMENODE_PORT=8010 \
            -e HEARTBEAT_INTERVAL=10 \
            -e STORAGE_PATH=/app/storage \
            "${EXTRA_ARGS[@]}" \
            tbfs-datanode:latest
        ;;
    
    frontend)
        if [ "$NUM" -ne 1 ]; then
            echo "⚠️  Advertencia: Solo hay un frontend, usando número 1"
        fi
        # El frontend se conecta a namenodes usando DNS de Docker
        # Flask en puerto 5000 (sin límite de tamaño), Streamlit en 8501
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --network-alias frontend \
            --hostname "$CONTAINER_NAME" \
            -p 8501:8501 \
            "${FRONTEND_CODE_VOLUMES[@]}" \
            -e NAMENODE_SERVICE=namenode \
            -e NAMENODE_PORT=8010 \
            -e DOWNLOAD_DIR=downloads \
            -e FLASK_PORT=8501 \
            "${EXTRA_ARGS[@]}" \
            tbfs-frontend:latest
        ;;
    
    *)
        echo "❌ Error: Tipo desconocido: $TYPE"
        echo "   Tipos válidos: namenode, datanode, frontend"
        exit 1
        ;;
esac

echo ""
echo "✅ Contenedor $CONTAINER_NAME creado exitosamente"
echo ""
echo "📊 Para ver el estado:"
echo "   docker ps | grep $CONTAINER_NAME"
echo "   docker logs -f $CONTAINER_NAME"
echo ""
