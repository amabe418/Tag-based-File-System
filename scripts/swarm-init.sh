#!/bin/bash
# Script para inicializar Docker Swarm en el nodo manager
# Uso: ./swarm-init.sh [ADVERTISE_ADDR]

set -e

echo "🐳 Inicializando Docker Swarm..."

# Función para detectar IP de la interfaz de red principal (sin depender de internet)
# detect_network_ip() {
#     # Obtener todas las IPs de interfaces físicas (excluyendo loopback, docker, virtuales)
#     # Priorizar interfaces ethernet y wifi
#     local ip_addresses=$(ip addr show | grep -E 'inet ' | grep -v '127.0.0.1' | grep -v 'docker' | \
#         awk '{print $2}' | cut -d'/' -f1 | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')
    
#     # Filtrar IPs privadas comunes (excluir 169.254.x.x que son link-local)
#     for ip in $ip_addresses; do
#         # Excluir IPs link-local (169.254.x.x)
#         if [[ ! "$ip" =~ ^169\.254\. ]]; then
#             echo "$ip"
#             return 0
#         fi
#     done
    
#     # Si no encontramos ninguna, intentar con hostname -I
#     local hostname_ips=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | grep -v '127.0.0.1' | grep -v '169.254.')
#     if [ -n "$hostname_ips" ]; then
#         echo "$hostname_ips" | head -1
#         return 0
#     fi
    
#     return 1
# }

detect_network_ip() {
    ip route get 1 | awk '{print $7; exit}'
}


# Obtener IP de la interfaz principal si no se proporciona
if [ -z "$1" ]; then
    echo "🔍 Detectando IP de la interfaz de red principal..."
    ADVERTISE_ADDR=$(detect_network_ip)
    
    if [ -z "$ADVERTISE_ADDR" ]; then
        echo "❌ Error: No se pudo detectar la IP automáticamente"
        echo ""
        echo "💡 Interfaces de red disponibles:"
        ip addr show | grep -E '^[0-9]+:|inet ' | grep -v '127.0.0.1' | head -10
        echo ""
        echo "   Por favor, proporciona la IP manualmente:"
        echo "   ./swarm-init.sh <IP>"
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
