#!/bin/bash
# Script para construir las imágenes Docker en ambas máquinas
# Debe ejecutarse en ambas máquinas antes de desplegar

set -e

# Obtener el directorio del script y cambiar al directorio raíz del proyecto
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
    echo "  -h, --help       Mostrar esta ayuda"
    echo ""
    echo "Nota: Si solo hiciste cambios en el código y los volúmenes están montados,"
    echo "      no necesitas reconstruir las imágenes. Solo reinicia los contenedores."
}

# Verificar si se debe forzar la reconstrucción
FORCE_REBUILD=false
case "$1" in
    -r|--rebuild)
        FORCE_REBUILD=true
        echo "⚠️  Modo de reconstrucción forzada activado"
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

echo "🏗️  Construyendo imágenes Docker..."
echo ""

# Construir imágenes (sin Registry - ya no es necesario)
build_if_needed "tbfs-namenode:latest" "namenode/dockerfile.yml" "MetaNameNode"
build_if_needed "tbfs-datanode:latest" "datanode/dockerfile.yml" "DataNode"
build_if_needed "tbfs-frontend:latest" "client/dockerfile.yml" "Frontend"

echo ""
echo "✅ Todas las imágenes construidas correctamente"
echo ""
echo "📋 Para ver las imágenes:"
echo "   docker images | grep tbfs"
echo ""
echo "💡 Tip: Si solo cambias código Python, no necesitas reconstruir."
echo "   Los volúmenes montan el código directamente en los contenedores."
echo "   Solo reinicia los contenedores para aplicar los cambios."
echo ""
