#!/bin/bash
# Ejecutar Registry Service (solo 1 nodo para desarrollo local)
echo "Iniciando Registry Service (puerto 9000)..."
export NODE_ID=registry-1
export PEERS=  # Sin peers para modo desarrollo local
export REGISTRY_PORT=9000
uvicorn Registry.registry:app --host 0.0.0.0 --port 9000 &
REGISTRY_PID=$!

# Esperar un momento para que el registry inicie
echo "Esperando que el registry se estabilice..."
sleep 2

# Ejecutar Backend
echo "Iniciando Backend en puerto 8000..."
export REGISTRY_URL=http://127.0.0.1:9000
export SERVER_PORT=8000
uvicorn Server.api:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Esperar un momento para que el backend inicie
sleep 2

# Ejecutar Frontend
echo "Iniciando Frontend en puerto 8501..."
export REGISTRY_URL=http://127.0.0.1:9000
export API_URL=http://127.0.0.1:8000
streamlit run Client/web.py

# Limpiar procesos al salir
trap "kill $REGISTRY_PID $BACKEND_PID 2>/dev/null" EXIT