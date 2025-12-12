#!/bin/bash
# Script para inicializar Docker Swarm en el nodo manager
# Uso: ./swarm-init.sh [ADVERTISE_ADDR]

set -e

echo "🐳 Inicializando Docker Swarm..."

# Obtener IP de la interfaz principal si no se proporciona
if [ -z "$1" ]; then
    ADVERTISE_ADDR=$(ip route get 8.8.8.8 | grep -oP 'src \K\S+' | head -1)
    if [ -z "$ADVERTISE_ADDR" ]; then
        echo "❌ Error: No se pudo detectar la IP automáticamente"
        echo "   Por favor, proporciona la IP manualmente: ./swarm-init.sh <IP>"
        exit 1
    fi
    echo "📍 IP detectada automáticamente: $ADVERTISE_ADDR"
else
    ADVERTISE_ADDR=$1
fi

# Verificar si ya está inicializado
if docker info | grep -q "Swarm: active"; then
    echo "⚠️  Docker Swarm ya está inicializado"
    echo "   Para reiniciar, ejecuta: docker swarm leave --force"
    exit 1
fi

# Inicializar swarm
echo "🚀 Inicializando swarm con advertise-addr: $ADVERTISE_ADDR"
docker swarm init --advertise-addr "$ADVERTISE_ADDR"

# Crear red overlay
echo "🌐 Creando red overlay..."
docker network create --driver overlay --attachable tbfs_net 2>/dev/null || echo "   Red overlay ya existe"

# Obtener el token para unirse como worker
WORKER_TOKEN=$(docker swarm join-token worker -q)
MANAGER_TOKEN=$(docker swarm join-token manager -q)

echo ""
echo "✅ Docker Swarm inicializado correctamente"
echo "✅ Red overlay 'tbfs_net' creada"
echo ""
echo "📋 Información del Swarm:"
echo "   Manager IP: $ADVERTISE_ADDR"
echo ""
echo "🔑 Para unir un worker (segunda máquina), ejecuta:"
echo "   ./swarm-join.sh $ADVERTISE_ADDR $WORKER_TOKEN"
echo ""
echo "💾 Guarda estos tokens de forma segura:"
echo "   Worker Token:  $WORKER_TOKEN"
echo "   Manager Token: $MANAGER_TOKEN"
echo ""
