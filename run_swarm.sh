#!/bin/bash
# Crear red si no existe
docker network create tbfs_net 2>/dev/null || true

# Verificar si se debe forzar la reconstrucción
FORCE_REBUILD=false
if [ "$1" == "--rebuild" ] || [ "$1" == "-r" ]; then
    FORCE_REBUILD=true
    echo "⚠️  Modo de reconstrucción forzada activado"
fi

# Función para construir imagen solo si no existe o si se fuerza
build_if_needed() {
    local image_name=$1
    local dockerfile_path=$2
    local service_name=$3
    
    if [ "$FORCE_REBUILD" = true ] || ! docker image inspect "$image_name" >/dev/null 2>&1; then
        echo "Construyendo imagen de $service_name..."
        docker build -t "$image_name" -f "$dockerfile_path" .
    else
        echo "✅ Imagen $service_name ya existe, omitiendo construcción (usa --rebuild para forzar)"
    fi
}

# Construir imágenes solo si no existen
build_if_needed "tbfs-registry" "registry/dockerfile.yml" "Registry"
build_if_needed "tbfs-namenode" "namenode/dockerfile.yml" "MetaNameNode"
build_if_needed "tbfs-frontend" "client/dockerfile.yml" "Frontend"

# Detener contenedores existentes si existen
echo "Deteniendo contenedores existentes..."
docker stop tbfs-registry-1 tbfs-registry-2 tbfs-registry-3 tbfs-namenode-1 tbfs-namenode-2 tbfs-namenode-3 tbfs-frontend 2>/dev/null || true
docker rm tbfs-registry-1 tbfs-registry-2 tbfs-registry-3 tbfs-namenode-1 tbfs-namenode-2 tbfs-namenode-3 tbfs-frontend 2>/dev/null || true

# Ejecutar 3 nodos del Registry Service
echo "Iniciando Registry Service - Nodo 1..."
docker run -d --name tbfs-registry-1 --network tbfs_net -p 9000:9000 \
  -e NODE_ID=registry-1 \
  -e PEERS=tbfs-registry-2,tbfs-registry-3 \
  -e REGISTRY_PORT=9000 \
  -e HEARTBEAT_TIMEOUT=30 \
  -e CLEANUP_INTERVAL=10 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  tbfs-registry

echo "Iniciando Registry Service - Nodo 2..."
docker run -d --name tbfs-registry-2 --network tbfs_net -p 9001:9000 \
  -e NODE_ID=registry-2 \
  -e PEERS=tbfs-registry-1,tbfs-registry-3 \
  -e REGISTRY_PORT=9000 \
  -e HEARTBEAT_TIMEOUT=30 \
  -e CLEANUP_INTERVAL=10 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  tbfs-registry

echo "Iniciando Registry Service - Nodo 3..."
docker run -d --name tbfs-registry-3 --network tbfs_net -p 9002:9000 \
  -e NODE_ID=registry-3 \
  -e PEERS=tbfs-registry-1,tbfs-registry-2 \
  -e REGISTRY_PORT=9000 \
  -e HEARTBEAT_TIMEOUT=30 \
  -e CLEANUP_INTERVAL=10 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  tbfs-registry

# Esperar un momento para que los registries se estabilicen
echo "Esperando que los registries se estabilicen..."
sleep 5

# Ejecutar 3 nodos del MetaNameNode
echo "Iniciando MetaNameNode - Nodo 1..."
docker run -d --name tbfs-namenode-1 --network tbfs_net -p 8010:8010 \
  -e NODE_ID=namenode-1 \
  -e PEERS=tbfs-namenode-2,tbfs-namenode-3 \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e REGISTRY_URL=http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000 \
  -e HEARTBEAT_INTERVAL=10 \
  tbfs-namenode

echo "Iniciando MetaNameNode - Nodo 2..."
docker run -d --name tbfs-namenode-2 --network tbfs_net -p 8011:8010 \
  -e NODE_ID=namenode-2 \
  -e PEERS=tbfs-namenode-1,tbfs-namenode-3 \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e REGISTRY_URL=http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000 \
  -e HEARTBEAT_INTERVAL=10 \
  tbfs-namenode

echo "Iniciando MetaNameNode - Nodo 3..."
docker run -d --name tbfs-namenode-3 --network tbfs_net -p 8012:8010 \
  -e NODE_ID=namenode-3 \
  -e PEERS=tbfs-namenode-1,tbfs-namenode-2 \
  -e NAMENODE_PORT=8010 \
  -e HEARTBEAT_TIMEOUT=15 \
  -e LEADER_HEARTBEAT_INTERVAL=5 \
  -e ELECTION_TIMEOUT=15 \
  -e REGISTRY_URL=http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000 \
  -e HEARTBEAT_INTERVAL=10 \
  tbfs-namenode

# Ejecutar Frontend Service
echo "Iniciando Frontend Service..."
docker run -d --name tbfs-frontend --network tbfs_net -p 8501:8501 \
  -e REGISTRY_URL=http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000 \
  -e DOWNLOAD_DIR=downloads \
  tbfs-frontend

echo ""
echo "✅ Servicios iniciados:"
echo "  - Registry Nodo 1: http://localhost:9000"
echo "  - Registry Nodo 2: http://localhost:9001"
echo "  - Registry Nodo 3: http://localhost:9002"
echo "  - MetaNameNode Nodo 1: http://localhost:8010"
echo "  - MetaNameNode Nodo 2: http://localhost:8011"
echo "  - MetaNameNode Nodo 3: http://localhost:8012"
echo "  - Frontend: http://localhost:8501"
echo ""
echo "Para ver los logs:"
echo "  docker logs -f tbfs-registry-1"
echo "  docker logs -f tbfs-namenode-1"
echo "  docker logs -f tbfs-frontend"