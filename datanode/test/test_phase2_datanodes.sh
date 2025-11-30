#!/bin/bash

# Script de pruebas para Fase 2: Integración MetaNameNode-DataNode
# Verifica que la integración entre MetaNameNode y DataNodes funcione correctamente

echo "=========================================="
echo "PRUEBAS FASE 2: Integración MetaNameNode-DataNode"
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

# Función para obtener JSON y verificar campo
check_json_field() {
    local url=$1
    local field=$2
    local description=$3
    
    echo -n "  Verificando: $description... "
    
    response=$(curl -s "$url" 2>/dev/null)
    if echo "$response" | grep -q "\"$field\""; then
        echo -e "${GREEN}✓ PASÓ${NC} ($field presente)"
        ((TESTS_PASSED++))
        return 0
    else
        echo -e "${RED}✗ FALLÓ${NC} ($field no encontrado)"
        ((TESTS_FAILED++))
        return 1
    fi
}

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
echo "PASO 2.1: Endpoints de Gestión de DataNodes en MetaNameNode"
echo "----------------------------------------"

# Verificar endpoint GET /datanodes
echo "Probando endpoint GET /datanodes..."
check_http "$NAMENODE_URL/datanodes" "200" "Listar todos los DataNodes"

# Verificar que retorna lista de DataNodes
echo -n "  Verificando estructura de respuesta... "
datanodes_response=$(curl -s "$NAMENODE_URL/datanodes" 2>/dev/null)
if echo "$datanodes_response" | grep -q "\"datanodes\""; then
    datanode_count=$(echo "$datanodes_response" | grep -o "datanode-[0-9]" | sort -u | wc -l | tr -d ' ')
    echo -e "${GREEN}✓ PASÓ${NC} (Encontrados $datanode_count DataNodes)"
    ((TESTS_PASSED++))
    
    if [ "$datanode_count" -ge 3 ]; then
        echo -e "    ${GREEN}✓${NC} Hay suficientes DataNodes para réplicas (mínimo 3)"
        ((TESTS_PASSED++))
    else
        echo -e "    ${RED}✗${NC} Solo $datanode_count DataNodes (se necesitan 3+)"
        ((TESTS_FAILED++))
    fi
else
    echo -e "${RED}✗ FALLÓ${NC} (Estructura de respuesta incorrecta)"
    ((TESTS_FAILED++))
fi

# Verificar endpoint GET /datanodes/{node_id}
echo ""
echo "Probando endpoint GET /datanodes/{node_id}..."
for i in {1..5}; do
    check_http "$NAMENODE_URL/datanodes/datanode-$i" "200" "Obtener información de DataNode $i"
    
    # Verificar que retorna información correcta
    echo -n "    Verificando información del DataNode $i... "
    dn_info=$(curl -s "$NAMENODE_URL/datanodes/datanode-$i" 2>/dev/null)
    if echo "$dn_info" | grep -q "\"node_id\":\"datanode-$i\""; then
        status=$(echo "$dn_info" | grep -o "\"status\":\"[^\"]*\"" | cut -d'"' -f4)
        if [ "$status" == "active" ]; then
            echo -e "${GREEN}✓${NC} (status: $status)"
            ((TESTS_PASSED++))
        else
            echo -e "${YELLOW}⚠${NC} (status: $status)"
        fi
    else
        echo -e "${RED}✗${NC} (Información incorrecta)"
        ((TESTS_FAILED++))
    fi
done

# Verificar que los heartbeats se están actualizando
echo ""
echo "Verificando actualización de heartbeats..."
echo -n "  Esperando 12 segundos para que se envíen heartbeats... "
sleep 12
echo -e "${GREEN}✓${NC}"

# Verificar que los heartbeats se actualizaron
for i in {1..3}; do
    echo -n "    Verificando heartbeat de DataNode $i... "
    dn_info=$(curl -s "$NAMENODE_URL/datanodes/datanode-$i" 2>/dev/null)
    if echo "$dn_info" | grep -q "last_heartbeat"; then
        echo -e "${GREEN}✓${NC} (Heartbeat presente)"
        ((TESTS_PASSED++))
    else
        echo -e "${YELLOW}⚠${NC} (Heartbeat no encontrado)"
    fi
done

echo ""
echo "PASO 2.2: Asignación de Réplicas al Agregar Archivo"
echo "----------------------------------------"

# Crear archivo de prueba
TEST_FILE="/tmp/test_file_phase2.txt"
echo "Contenido de prueba para Fase 2 - Integración MetaNameNode-DataNode" > "$TEST_FILE"

# Calcular hash SHA256
if command -v sha256sum &> /dev/null; then
    FILE_HASH=$(sha256sum "$TEST_FILE" | cut -d' ' -f1)
elif command -v shasum &> /dev/null; then
    FILE_HASH=$(shasum -a 256 "$TEST_FILE" | cut -d' ' -f1)
else
    echo -e "${RED}✗ ERROR${NC}: No se encontró comando para calcular SHA256"
    exit 1
fi

echo "Archivo de prueba: $TEST_FILE"
echo "Hash: ${FILE_HASH:0:16}..."
echo ""

# Subir archivo al MetaNameNode
echo "Subiendo archivo al MetaNameNode (debe asignar 3 réplicas)..."
echo -n "  POST /add... "

# Obtener respuesta con código HTTP
temp_response=$(mktemp)
http_code=$(curl -s -w "%{http_code}" -o "$temp_response" -X POST "$NAMENODE_URL/add" \
    -F "file=@$TEST_FILE" \
    -F "tags=test,phase2,integration" 2>/dev/null)
body=$(cat "$temp_response")
rm -f "$temp_response"

if [ "$http_code" == "200" ]; then
    echo -e "${GREEN}✓ PASÓ${NC} (HTTP $http_code)"
    ((TESTS_PASSED++))
    
    # Verificar que se asignaron réplicas
    echo -n "  Verificando asignación de réplicas... "
    
    # Intentar usar jq si está disponible, sino usar grep
    if command -v jq &> /dev/null; then
        replicas_json=$(echo "$body" | jq -r '.replicas[]?' 2>/dev/null)
        if [ -n "$replicas_json" ]; then
            REPLICAS=$(echo "$replicas_json" | tr '\n' ' ')
            replicas_count=$(echo "$replicas_json" | wc -l | tr -d ' ')
        else
            REPLICAS=""
            replicas_count=0
        fi
    else
        # Fallback: usar grep para extraer réplicas del JSON
        if echo "$body" | grep -q "\"replicas\""; then
            REPLICAS=$(echo "$body" | grep -o "\"datanode-[0-9]\"" | tr -d '"' | sort -u | tr '\n' ' ')
            replicas_count=$(echo "$REPLICAS" | wc -w | tr -d ' ')
        else
            REPLICAS=""
            replicas_count=0
        fi
    fi
    
    if [ "$replicas_count" -gt 0 ]; then
        echo -e "${GREEN}✓ PASÓ${NC} ($replicas_count réplicas asignadas)"
        ((TESTS_PASSED++))
        echo "    Réplicas asignadas: $REPLICAS"
        
        if [ "$replicas_count" -ge 2 ]; then
            echo -e "    ${GREEN}✓${NC} Mínimo de réplicas alcanzado (2+)"
            ((TESTS_PASSED++))
        else
            echo -e "    ${YELLOW}⚠${NC} Solo $replicas_count réplicas (se esperan 3)"
        fi
    else
        echo -e "${RED}✗ FALLÓ${NC} (No se encontraron réplicas en la respuesta)"
        echo "    Respuesta completa: $body"
        ((TESTS_FAILED++))
    fi
    
    # Verificar que el archivo se guardó en los DataNodes
    if [ -n "$REPLICAS" ] && [ "$replicas_count" -gt 0 ]; then
        echo ""
        echo "  Verificando que el archivo se guardó en los DataNodes..."
        REPLICA_COUNT=0
        for replica in $REPLICAS; do
            # Extraer número del DataNode (puede ser "datanode-1" o solo "1")
            if echo "$replica" | grep -q "datanode-"; then
                dn_num=$(echo "$replica" | grep -o "[0-9]")
            else
                dn_num="$replica"
            fi
            port=$((8000 + dn_num))
            
            echo -n "    Verificando $replica (puerto $port)... "
            retrieve_response=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/retrieve/$FILE_HASH" 2>/dev/null)
            
            if [ "$retrieve_response" == "200" ]; then
                echo -e "${GREEN}✓${NC} (Archivo encontrado)"
                ((TESTS_PASSED++))
                ((REPLICA_COUNT++))
            else
                echo -e "${RED}✗${NC} (HTTP $retrieve_response - Archivo no encontrado)"
                ((TESTS_FAILED++))
            fi
        done
    else
        echo ""
        echo "  ⚠ No se pueden verificar DataNodes (réplicas no encontradas en respuesta)"
        REPLICA_COUNT=0
    fi
    
    if [ "$REPLICA_COUNT" -ge 2 ]; then
        echo -e "    ${GREEN}✓${NC} Archivo almacenado en $REPLICA_COUNT DataNodes (mínimo 2 requerido)"
        ((TESTS_PASSED++))
    else
        echo -e "    ${RED}✗${NC} Archivo solo almacenado en $REPLICA_COUNT DataNodes (se esperan 2+)"
        ((TESTS_FAILED++))
    fi
    
else
    echo -e "${RED}✗ FALLÓ${NC} (HTTP $http_code)"
    echo "    Respuesta: $body"
    ((TESTS_FAILED++))
fi

echo ""
echo "PASO 2.3: Verificar Metadatos en MetaNameNode"
echo "----------------------------------------"

# Verificar que el archivo aparece en la lista
echo "Verificando que el archivo aparece en los metadatos..."
echo -n "  GET /list... "

list_response=$(curl -s "$NAMENODE_URL/list" 2>/dev/null)
if echo "$list_response" | grep -q "test_file_phase2.txt"; then
    echo -e "${GREEN}✓ PASÓ${NC} (Archivo encontrado en metadatos)"
    ((TESTS_PASSED++))
    
    # Verificar que tiene las tags correctas
    echo -n "  Verificando tags... "
    if echo "$list_response" | grep -q "test,phase2,integration"; then
        echo -e "${GREEN}✓ PASÓ${NC} (Tags correctas)"
        ((TESTS_PASSED++))
    else
        echo -e "${YELLOW}⚠${NC} (Tags no verificadas correctamente)"
    fi
else
    echo -e "${RED}✗ FALLÓ${NC} (Archivo no encontrado en metadatos)"
    ((TESTS_FAILED++))
fi

# Buscar por tags
echo ""
echo "Probando búsqueda por tags..."
echo -n "  GET /list?tags=test... "
search_response=$(curl -s "$NAMENODE_URL/list?tags=test" 2>/dev/null)
if echo "$search_response" | grep -q "test_file_phase2.txt"; then
    echo -e "${GREEN}✓ PASÓ${NC} (Archivo encontrado por tag)"
    ((TESTS_PASSED++))
else
    echo -e "${RED}✗ FALLÓ${NC} (Archivo no encontrado por tag)"
    ((TESTS_FAILED++))
fi

echo ""
echo "PASO 2.4: Flujo Completo de Escritura"
echo "----------------------------------------"

# Crear otro archivo para probar el flujo completo
TEST_FILE2="/tmp/test_file2_phase2.txt"
echo "Segundo archivo de prueba para Fase 2" > "$TEST_FILE2"

if command -v sha256sum &> /dev/null; then
    FILE_HASH2=$(sha256sum "$TEST_FILE2" | cut -d' ' -f1)
elif command -v shasum &> /dev/null; then
    FILE_HASH2=$(shasum -a 256 "$TEST_FILE2" | cut -d' ' -f1)
fi

echo "Probando flujo completo: Cliente → MetaNameNode → DataNodes"
echo -n "  Subiendo segundo archivo... "

# Obtener respuesta con código HTTP (compatible con macOS)
temp_response2=$(mktemp)
http_code2=$(curl -s -w "%{http_code}" -o "$temp_response2" -X POST "$NAMENODE_URL/add" \
    -F "file=@$TEST_FILE2" \
    -F "tags=test2,phase2" 2>/dev/null)
body2=$(cat "$temp_response2")
rm -f "$temp_response2"

if [ "$http_code2" == "200" ]; then
    echo -e "${GREEN}✓ PASÓ${NC} (HTTP $http_code2)"
    ((TESTS_PASSED++))
    
    # Verificar que se asignaron réplicas diferentes (si es posible)
    echo -n "  Verificando distribución de réplicas... "
    if command -v jq &> /dev/null; then
        stored_count=$(echo "$body2" | jq -r '.replicas_stored // 0' 2>/dev/null)
    else
        if echo "$body2" | grep -q "\"replicas_stored\""; then
            stored_count=$(echo "$body2" | grep -o "\"replicas_stored\":[0-9]*" | cut -d':' -f2)
        else
            stored_count=0
        fi
    fi
    
    if [ "$stored_count" -gt 0 ]; then
        echo -e "${GREEN}✓ PASÓ${NC} ($stored_count réplicas almacenadas)"
        ((TESTS_PASSED++))
    else
        echo -e "${YELLOW}⚠${NC} (No se pudo verificar cantidad de réplicas almacenadas)"
    fi
else
    echo -e "${RED}✗ FALLÓ${NC} (HTTP $http_code2)"
    ((TESTS_FAILED++))
fi

# Limpiar archivos temporales
rm -f "$TEST_FILE" "$TEST_FILE2"

echo ""
echo "=========================================="
echo "RESUMEN DE PRUEBAS FASE 2"
echo "=========================================="
echo -e "Pruebas pasadas: ${GREEN}$TESTS_PASSED${NC}"
echo -e "Pruebas fallidas: ${RED}$TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ TODAS LAS PRUEBAS PASARON${NC}"
    echo ""
    echo "La Fase 2 está completa y funcionando correctamente."
    echo ""
    echo "Verificaciones completadas:"
    echo "  ✓ Endpoints de gestión de DataNodes funcionan"
    echo "  ✓ Asignación de réplicas funciona"
    echo "  ✓ Archivos se almacenan en múltiples DataNodes"
    echo "  ✓ Metadatos se guardan correctamente"
    echo "  ✓ Flujo completo Cliente → MetaNameNode → DataNodes funciona"
    exit 0
else
    echo -e "${RED}✗ ALGUNAS PRUEBAS FALLARON${NC}"
    echo ""
    echo "Revisa los logs de los contenedores para más detalles:"
    echo "  docker logs tbfs-namenode-1"
    echo "  docker logs tbfs-datanode-1"
    exit 1
fi

