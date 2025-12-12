#!/bin/bash
# Script para construir las imágenes Docker en ambas máquinas
# Debe ejecutarse en ambas máquinas antes de desplegar

set -e

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
        echo "🔨 Construyendo imagen de $service_name..."
        docker build -t "$image_name" -f "$dockerfile_path" .
        echo "✅ Imagen $image_name construida"
    else
        echo "✅ Imagen $service_name ya existe, omitiendo construcción (usa --rebuild para forzar)"
    fi
}

echo "🏗️  Construyendo imágenes Docker..."
echo ""

# Construir imágenes
build_if_needed "tbfs-registry:latest" "registry/dockerfile.yml" "Registry"
build_if_needed "tbfs-namenode:latest" "namenode/dockerfile.yml" "MetaNameNode"
build_if_needed "tbfs-datanode:latest" "datanode/dockerfile.yml" "DataNode"
build_if_needed "tbfs-frontend:latest" "client/dockerfile.yml" "Frontend"

echo ""
echo "✅ Todas las imágenes construidas correctamente"
echo ""
echo "📋 Para ver las imágenes:"
echo "   docker images | grep tbfs"
echo ""

