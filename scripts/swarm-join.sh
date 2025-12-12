#!/bin/bash
# Script para unirse al Docker Swarm como worker
# Uso: ./swarm-join.sh <MANAGER_IP> [WORKER_TOKEN]

set -e

if [ -z "$1" ]; then
    echo "❌ Error: Debes proporcionar la IP del manager"
    echo "   Uso: ./swarm-join.sh <MANAGER_IP> [WORKER_TOKEN]"
    exit 1
fi

MANAGER_IP=$1
WORKER_TOKEN=$2

# Verificar si ya está en un swarm
if docker info | grep -q "Swarm: active"; then
    echo "⚠️  Este nodo ya está en un swarm"
    echo "   Para salir, ejecuta: docker swarm leave"
    exit 1
fi

# Si no se proporciona el token, solicitarlo
if [ -z "$WORKER_TOKEN" ]; then
    echo "🔍 Token de worker no proporcionado"
    echo "   Ejecuta en el manager: docker swarm join-token worker -q"
    read -p "   Ingresa el WORKER_TOKEN: " WORKER_TOKEN
fi

if [ -z "$WORKER_TOKEN" ]; then
    echo "❌ Error: No se pudo obtener el token"
    exit 1
fi

# Unirse al swarm
echo "🔗 Uniéndose al swarm en $MANAGER_IP..."
docker swarm join --token "$WORKER_TOKEN" "$MANAGER_IP:2377"

echo ""
echo "✅ Nodo unido al swarm correctamente"
echo ""
echo "📋 Para verificar el estado:"
echo "   docker node ls"
echo ""
