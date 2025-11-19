#!/bin/bash
# Crear red si no existe
docker network create tbfs_net 2>/dev/null || true

# Construir imágenes
echo "Construyendo imagen del Registry..."
docker build -t tbfs-registry -f Registry/dockerfile.yml .

echo "Construyendo imagen del Backend..."
docker build -t tbfs-backend -f Server/dockerfile.yml .

echo "Construyendo imagen del Frontend..."
docker build -t tbfs-frontend -f Client/dockerfile.yml .

# Detener contenedores existentes si existen
echo "Deteniendo contenedores existentes..."
docker stop tbfs-registry-1 tbfs-registry-2 tbfs-registry-3 tbfs-backend tbfs-frontend 2>/dev/null || true
docker rm tbfs-registry-1 tbfs-registry-2 tbfs-registry-3 tbfs-backend tbfs-frontend 2>/dev/null || true

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

# Ejecutar Backend Service
echo "Iniciando Backend Service..."
docker run -d --name tbfs-backend --network tbfs_net -p 8000:8000 \
  -e REGISTRY_URL=http://tbfs-registry-1:9000,http://tbfs-registry-2:9000,http://tbfs-registry-3:9000 \
  -e SERVER_PORT=8000 \
  -e HEARTBEAT_INTERVAL=10 \
  tbfs-backend

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
echo "  - Backend: http://localhost:8000"
echo "  - Frontend: http://localhost:8501"
echo ""
echo "Para ver los logs:"
echo "  docker logs -f tbfs-registry-1"
echo "  docker logs -f tbfs-backend"
echo "  docker logs -f tbfs-frontend"