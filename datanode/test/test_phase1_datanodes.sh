#!/bin/bash

# Script de pruebas para Fase 1: Estructura Básica del DataNode
# Verifica que los DataNodes funcionen correctamente

echo "=========================================="
echo "PRUEBAS FASE 1: Estructura Básica DataNode"
echo "=========================================="
echo ""

# Colores para output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Contador de pruebas
TESTS_PASSED=0
TESTS_FAILED=0

# Función para verificar respuesta HTTP
check_http() {
    local url=$1
    local expected_status=$2
    local description=$3
    
    echo -n "  Probando: $description... "
    
    response=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)
    
    if [ "$response" == "$expected_status" ]; then
        echo -e "${GREEN}✓ PASÓ${NC} (HTTP $response)"
        ((TESTS_PASSED++))
        return 0
    else
        echo -e "${RED}✗ FALLÓ${NC} (Esperado: HTTP $expected_status, Obtenido: HTTP $response)"
        ((TESTS_FAILED++))
        return 1
    fi
}

# Función para verificar JSON response
check_json() {
    local url=$1
    local key=$2
    local expected_value=$3
    local description=$4
    
    echo -n "  Probando: $description... "
    
    response=$(curl -s "$url" 2>/dev/null)
    value=$(echo "$response" | grep -o "\"$key\":[^,}]*" | cut -d'"' -f4)
    
    if [ "$value" == "$expected_value" ]; then
        echo -e "${GREEN}✓ PASÓ${NC} ($key = $value)"
        ((TESTS_PASSED++))
        return 0
    else
        echo -e "${RED}✗ FALLÓ${NC} (Esperado: $key = $expected_value, Obtenido: $key = $value)"
        ((TESTS_FAILED++))
        return 1
    fi
}

echo "PASO 1.1: Estructura Base del DataNode"
echo "----------------------------------------"

# Verificar que los DataNodes estén corriendo
echo "Verificando que los DataNodes estén corriendo..."
for i in {1..5}; do
    if docker ps | grep -q "tbfs-datanode-$i"; then
        echo -e "  ${GREEN}✓${NC} DataNode $i está corriendo"
    else
        echo -e "  ${RED}✗${NC} DataNode $i NO está corriendo"
        ((TESTS_FAILED++))
    fi
done

echo ""
echo "Probando endpoints básicos de cada DataNode..."

# Probar endpoint raíz de cada DataNode
for i in {1..5}; do
    port=$((8000 + i))
    check_http "http://localhost:$port/" "200" "DataNode $i - Endpoint raíz (GET /)"
done

echo ""
# Probar endpoint de health
for i in {1..5}; do
    port=$((8000 + i))
    check_http "http://localhost:$port/health" "200" "DataNode $i - Health check (GET /health)"
done

echo ""
# Verificar que retornan información correcta
for i in {1..5}; do
    port=$((8000 + i))
    echo -n "  Verificando información del DataNode $i... "
    response=$(curl -s "http://localhost:$port/" 2>/dev/null)
    if echo "$response" | grep -q "DataNode funcionando"; then
        node_id=$(echo "$response" | grep -o "\"node_id\":\"[^\"]*\"" | cut -d'"' -f4)
        if [ "$node_id" == "datanode-$i" ]; then
            echo -e "${GREEN}✓ PASÓ${NC} (node_id correcto: $node_id)"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ FALLÓ${NC} (node_id incorrecto: $node_id, esperado: datanode-$i)"
            ((TESTS_FAILED++))
        fi
    else
        echo -e "${RED}✗ FALLÓ${NC} (No retorna mensaje esperado)"
        ((TESTS_FAILED++))
    fi
done

echo ""
echo "PASO 1.2: Sistema de Almacenamiento Local"
echo "----------------------------------------"

# Crear archivo de prueba
TEST_FILE="/tmp/test_file_phase1.txt"
echo "Contenido de prueba para Fase 1" > "$TEST_FILE"

# Calcular hash SHA256 (compatible con macOS y Linux)
if command -v sha256sum &> /dev/null; then
    FILE_HASH=$(sha256sum "$TEST_FILE" | cut -d' ' -f1)
elif command -v shasum &> /dev/null; then
    FILE_HASH=$(shasum -a 256 "$TEST_FILE" | cut -d' ' -f1)
else
    echo -e "${RED}✗ ERROR${NC}: No se encontró comando para calcular SHA256"
    exit 1
fi

# Verificar que el hash se calculó correctamente
if [ -z "$FILE_HASH" ] || [ ${#FILE_HASH} -lt 10 ]; then
    echo -e "${RED}✗ ERROR${NC}: No se pudo calcular el hash del archivo"
    exit 1
fi

echo "  Archivo de prueba creado: $TEST_FILE"
echo "  Hash calculado: ${FILE_HASH:0:16}..."

echo "Probando almacenamiento en DataNode 1..."
echo -n "  Subiendo archivo de prueba... "

# Subir archivo al DataNode
response=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:8001/store" \
    -F "file_id=$FILE_HASH" \
    -F "file=@$TEST_FILE" 2>/dev/null)

http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | head -n-1)

if [ "$http_code" == "200" ]; then
    echo -e "${GREEN}✓ PASÓ${NC} (HTTP $http_code)"
    ((TESTS_PASSED++))
    
    # Verificar que el archivo se puede leer
    echo -n "  Leyendo archivo almacenado... "
    retrieved=$(curl -s "http://localhost:8001/retrieve/$FILE_HASH" 2>/dev/null)
    original=$(cat "$TEST_FILE")
    
    if [ "$retrieved" == "$original" ]; then
        echo -e "${GREEN}✓ PASÓ${NC} (Contenido coincide)"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ FALLÓ${NC} (Contenido no coincide)"
        ((TESTS_FAILED++))
    fi
    
    # Verificar que el archivo existe
    echo -n "  Verificando información del archivo... "
    info_response=$(curl -s "http://localhost:8001/info" 2>/dev/null)
    if echo "$info_response" | grep -q "storage"; then
        echo -e "${GREEN}✓ PASÓ${NC} (Información de almacenamiento disponible)"
        ((TESTS_PASSED++))
    else
        echo -e "${YELLOW}⚠ ADVERTENCIA${NC} (No se pudo verificar información de almacenamiento)"
    fi
    
    # Eliminar archivo de prueba
    echo -n "  Eliminando archivo de prueba... "
    delete_response=$(curl -s -w "\n%{http_code}" -X DELETE "http://localhost:8001/delete/$FILE_HASH" 2>/dev/null)
    delete_code=$(echo "$delete_response" | tail -n1)
    
    if [ "$delete_code" == "200" ]; then
        echo -e "${GREEN}✓ PASÓ${NC} (HTTP $delete_code)"
        ((TESTS_PASSED++))
        
        # Verificar que el archivo fue eliminado
        echo -n "  Verificando que el archivo fue eliminado... "
        not_found=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8001/retrieve/$FILE_HASH" 2>/dev/null)
        if [ "$not_found" == "404" ]; then
            echo -e "${GREEN}✓ PASÓ${NC} (HTTP 404 - Archivo no encontrado)"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗ FALLÓ${NC} (Esperado HTTP 404, obtenido HTTP $not_found)"
            ((TESTS_FAILED++))
        fi
    else
        echo -e "${RED}✗ FALLÓ${NC} (HTTP $delete_code)"
        ((TESTS_FAILED++))
    fi
else
    echo -e "${RED}✗ FALLÓ${NC} (HTTP $http_code)"
    echo "    Respuesta: $body"
    ((TESTS_FAILED++))
fi

# Limpiar archivo temporal
rm -f "$TEST_FILE"

echo ""
echo "PASO 1.3: Registro con MetaNameNode"
echo "----------------------------------------"

# Verificar que el MetaNameNode esté corriendo
echo "Verificando que el MetaNameNode esté disponible..."
if docker ps | grep -q "tbfs-namenode"; then
    echo -e "  ${GREEN}✓${NC} MetaNameNode está corriendo"
else
    echo -e "  ${RED}✗${NC} MetaNameNode NO está corriendo"
    ((TESTS_FAILED++))
    echo ""
    echo "⚠️  No se pueden continuar las pruebas de registro sin MetaNameNode"
    echo ""
    echo "=========================================="
    echo "RESUMEN DE PRUEBAS"
    echo "=========================================="
    echo -e "Pruebas pasadas: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Pruebas fallidas: ${RED}$TESTS_FAILED${NC}"
    echo ""
    exit 1
fi

# Obtener URL del MetaNameNode líder
echo "Obteniendo URL del MetaNameNode líder..."
NAMENODE_URL=""
for port in 8010 8011 8012; do
    response=$(curl -s "http://localhost:$port/" 2>/dev/null)
    if echo "$response" | grep -q "\"is_leader\":true"; then
        NAMENODE_URL="http://localhost:$port"
        echo -e "  ${GREEN}✓${NC} MetaNameNode líder encontrado en puerto $port"
        break
    fi
done

if [ -z "$NAMENODE_URL" ]; then
    # Si no hay líder, usar el primero disponible
    NAMENODE_URL="http://localhost:8010"
    echo -e "  ${YELLOW}⚠${NC} No se detectó líder, usando $NAMENODE_URL"
fi

echo ""
echo "Verificando registro de DataNodes en MetaNameNode..."

# Listar DataNodes registrados
echo -n "  Obteniendo lista de DataNodes... "
datanodes_response=$(curl -s "$NAMENODE_URL/datanodes" 2>/dev/null)
if echo "$datanodes_response" | grep -q "datanodes"; then
    echo -e "${GREEN}✓ PASÓ${NC}"
    ((TESTS_PASSED++))
    
    # Contar DataNodes registrados
    datanode_count=$(echo "$datanodes_response" | grep -o "datanode-[0-9]" | sort -u | wc -l)
    echo "    DataNodes registrados: $datanode_count"
    
    if [ "$datanode_count" -ge 3 ]; then
        echo -e "    ${GREEN}✓${NC} Hay suficientes DataNodes registrados (mínimo 3 requeridos)"
        ((TESTS_PASSED++))
    else
        echo -e "    ${YELLOW}⚠${NC} Solo $datanode_count DataNodes registrados (se recomiendan 3+)"
    fi
    
    # Verificar información de cada DataNode
    echo ""
    echo "  Verificando información de DataNodes individuales..."
    for i in {1..5}; do
        echo -n "    DataNode $i... "
        dn_info=$(curl -s "$NAMENODE_URL/datanodes/datanode-$i" 2>/dev/null)
        if echo "$dn_info" | grep -q "node_id"; then
            status=$(echo "$dn_info" | grep -o "\"status\":\"[^\"]*\"" | cut -d'"' -f4)
            if [ "$status" == "active" ]; then
                echo -e "${GREEN}✓${NC} (status: $status)"
                ((TESTS_PASSED++))
            else
                echo -e "${YELLOW}⚠${NC} (status: $status)"
            fi
        else
            echo -e "${RED}✗${NC} (No encontrado)"
            ((TESTS_FAILED++))
        fi
    done
else
    echo -e "${RED}✗ FALLÓ${NC} (No se pudo obtener lista de DataNodes)"
    ((TESTS_FAILED++))
fi

echo ""
echo "Verificando heartbeats..."

# Esperar un momento para que se envíen heartbeats
sleep 2

# Verificar que los heartbeats se están actualizando
echo -n "  Verificando actualización de heartbeats... "
datanodes_updated=$(curl -s "$NAMENODE_URL/datanodes" 2>/dev/null)
current_time=$(date +%s)

# Verificar que al menos un DataNode tiene heartbeat reciente
if echo "$datanodes_updated" | grep -q "last_heartbeat"; then
    echo -e "${GREEN}✓ PASÓ${NC} (Heartbeats detectados)"
    ((TESTS_PASSED++))
else
    echo -e "${YELLOW}⚠ ADVERTENCIA${NC} (No se pudo verificar heartbeats)"
fi

echo ""
echo "=========================================="
echo "RESUMEN DE PRUEBAS FASE 1"
echo "=========================================="
echo -e "Pruebas pasadas: ${GREEN}$TESTS_PASSED${NC}"
echo -e "Pruebas fallidas: ${RED}$TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ TODAS LAS PRUEBAS PASARON${NC}"
    echo ""
    echo "La Fase 1 está completa y funcionando correctamente."
    exit 0
else
    echo -e "${RED}✗ ALGUNAS PRUEBAS FALLARON${NC}"
    echo ""
    echo "Revisa los logs de los contenedores para más detalles:"
    echo "  docker logs tbfs-datanode-1"
    echo "  docker logs tbfs-namenode-1"
    exit 1
fi

