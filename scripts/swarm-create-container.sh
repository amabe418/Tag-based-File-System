#!/bin/bash
# Script para crear un contenedor en la red overlay del Swarm
# Uso: ./swarm-create-container.sh <tipo> <numero> [opciones adicionales]
# Tipos: registry, namenode, datanode, frontend

set -e

if [ $# -lt 2 ]; then
    echo "❌ Error: Debes proporcionar tipo y número"
    echo ""
    echo "Uso: ./swarm-create-container.sh <tipo> <numero> [opciones]"
    echo ""
    echo "Tipos disponibles:"
    echo "  registry  - Registry Service (1-N)"
    echo "  namenode  - MetaNameNode (1-N)"
    echo "  datanode  - DataNode (1-N)"
    echo "  frontend  - Frontend (solo 1)"
    echo ""
    echo "Ejemplos:"
    echo "  ./swarm-create-container.sh registry 1"
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
REGISTRY_URLS="http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000"

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
    registry)
        PORT=$((9000 + NUM - 1))
        # Construir lista de peers: incluir todos los nodos desde 1 hasta NUM-1
        PEERS=""
        for i in $(seq 1 $((NUM - 1))); do
            if [ -n "$PEERS" ]; then
                PEERS="${PEERS},"
            fi
            PEERS="${PEERS}tbfs-registry-${i}"
        done
        # Si NUM es 1, PEERS estará vacío (nodo único), lo cual es válido
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --hostname "$CONTAINER_NAME" \
            -p "${PORT}:9000" \
            -e NODE_ID="tbfs-registry-${NUM}" \
            -e PEERS="$PEERS" \
            -e REGISTRY_PORT=9000 \
            -e HEARTBEAT_TIMEOUT=30 \
            -e CLEANUP_INTERVAL=10 \
            -e GOSSIP_INTERVAL=3 \
            -e GOSSIP_FANOUT=2 \
            -e PEER_FAILURE_TIMEOUT=30 \
            "${EXTRA_ARGS[@]}" \
            tbfs-registry:latest
        ;;
    
    namenode)
        PORT=$((8010 + NUM - 1))
        # Obtener peers (todos los namenodes existentes excepto este)
        PEERS=""
        for i in {1..10}; do
            if [ "$i" -ne "$NUM" ]; then
                if docker ps -a --format "{{.Names}}" | grep -q "tbfs-namenode-${i}"; then
                    if [ -n "$PEERS" ]; then
                        PEERS="${PEERS},"
                    fi
                    PEERS="${PEERS}tbfs-namenode-${i}"
                fi
            fi
        done
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --hostname "$CONTAINER_NAME" \
            -p "${PORT}:8010" \
            -v "tbfs-namenode-${NUM}-data:/app/namenode/data" \
            -e NODE_ID="tbfs-namenode-${NUM}" \
            -e PEERS="$PEERS" \
            -e NAMENODE_PORT=8010 \
            -e HEARTBEAT_TIMEOUT=15 \
            -e LEADER_HEARTBEAT_INTERVAL=5 \
            -e ELECTION_TIMEOUT=15 \
            -e REGISTRY_URL="$REGISTRY_URLS" \
            -e HEARTBEAT_INTERVAL=10 \
            "${EXTRA_ARGS[@]}" \
            tbfs-namenode:latest
        ;;
    
    datanode)
        PORT=$((8000 + NUM))
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --hostname "$CONTAINER_NAME" \
            -p "${PORT}:${PORT}" \
            -v "tbfs-datanode-${NUM}-storage:/app/storage" \
            -e DATANODE_ID="tbfs-datanode-${NUM}" \
            -e NODE_ID="$CONTAINER_NAME" \
            -e DATANODE_PORT="$PORT" \
            -e REGISTRY_URL="$REGISTRY_URLS" \
            -e HEARTBEAT_INTERVAL=10 \
            -e STORAGE_PATH=/app/storage \
            "${EXTRA_ARGS[@]}" \
            tbfs-datanode:latest
        ;;
    
    frontend)
        if [ "$NUM" -ne 1 ]; then
            echo "⚠️  Advertencia: Solo hay un frontend, usando número 1"
        fi
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network tbfs_net \
            --hostname "$CONTAINER_NAME" \
            -p 8501:8501 \
            -e REGISTRY_URL="$REGISTRY_URLS" \
            -e DOWNLOAD_DIR=downloads \
            "${EXTRA_ARGS[@]}" \
            tbfs-frontend:latest
        ;;
    
    *)
        echo "❌ Error: Tipo desconocido: $TYPE"
        echo "   Tipos válidos: registry, namenode, datanode, frontend"
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
