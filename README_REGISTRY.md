# Registry Service - Sistema de Descubrimiento Distribuido

## Descripción

El Registry Service es un componente central del sistema distribuido TBFS que permite el descubrimiento dinámico de servidores de datos. Cada servidor se registra automáticamente al iniciar y mantiene su estado activo mediante heartbeats periódicos.

## Arquitectura

```
┌─────────────┐
│   Cliente   │
│  (CLI/Web)  │
└──────┬──────┘
       │ 1. Consulta servidores disponibles
       ▼
┌─────────────┐
│   Registry  │
│  Service    │
└──────┬──────┘
       │ 2. Devuelve lista de servidores activos
       │
       │ 3. Registro y Heartbeats
       ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Backend 1  │     │  Backend 2  │     │  Backend N  │
└─────────────┘     └─────────────┘     └─────────────┘
```

## Componentes

### 1. Registry Service (`Registry/registry.py`)

Servicio FastAPI que mantiene el registro de servidores activos.

**Endpoints:**
- `GET /` - Estado del registry
- `POST /register` - Registro de un nuevo servidor
- `POST /heartbeat` - Recepción de heartbeat
- `GET /servers` - Lista todos los servidores
- `GET /servers/active` - Lista solo servidores activos
- `GET /servers/{server_id}` - Información de un servidor específico
- `DELETE /servers/{server_id}` - Eliminar servidor del registro

**Características:**
- Limpieza automática de servidores inactivos (timeout configurable)
- Detección de recuperación de servidores
- Almacenamiento en memoria (puede extenderse a base de datos)

### 2. Registry Client (Backend) (`Server/core/registry_client.py`)

Cliente que se ejecuta en cada servidor de datos para:
- Auto-registrarse al iniciar
- Enviar heartbeats periódicos
- Re-registrarse si se pierde la conexión

### 3. Registry Client (Frontend) (`Client/registry_client.py`)

Cliente que se ejecuta en los clientes (CLI y Web) para:
- Consultar servidores disponibles
- Seleccionar servidor (random, first, round-robin)
- Caché de servidores para reducir consultas

## Configuración

### Variables de Entorno

**Registry Service:**
- `HEARTBEAT_TIMEOUT`: Tiempo en segundos antes de marcar un servidor como inactivo (default: 30)
- `CLEANUP_INTERVAL`: Intervalo de limpieza en segundos (default: 10)

**Backend:**
- `REGISTRY_URL`: URL del registry service (default: http://registry:9000)
- `SERVER_PORT`: Puerto del servidor (default: 8000)
- `HEARTBEAT_INTERVAL`: Intervalo de heartbeat en segundos (default: 10)
- `SERVER_ID`: ID único del servidor (opcional, se genera automáticamente)

**Frontend:**
- `REGISTRY_URL`: URL del registry service (default: http://registry:9000)
- `API_URL`: URL de fallback si el registry no está disponible

## Uso

### Ejecución Local

```bash
./run_local.sh
```

Esto iniciará:
1. Registry Service en puerto 9000
2. Backend en puerto 8000
3. Frontend en puerto 8501

### Ejecución con Docker

```bash
./run_swarm.sh
```

O usando docker-compose:

```bash
docker-compose up -d
```

### Verificar Estado del Registry

```bash
curl http://localhost:9000/
curl http://localhost:9000/servers/active
```

## Flujo de Operación

1. **Inicio del Backend:**
   - El backend se inicia y automáticamente se registra en el registry
   - Comienza a enviar heartbeats cada `HEARTBEAT_INTERVAL` segundos

2. **Consulta del Cliente:**
   - El cliente consulta `/servers/active` al registry
   - Selecciona un servidor (por defecto: aleatorio)
   - Realiza la petición directamente al servidor seleccionado

3. **Detección de Inactividad:**
   - Si un servidor no envía heartbeat por `HEARTBEAT_TIMEOUT` segundos, se marca como inactivo
   - El cliente no verá servidores inactivos en sus consultas

4. **Recuperación:**
   - Si un servidor inactivo vuelve a enviar heartbeat, se marca automáticamente como activo

## Ventajas del Sistema

- **Descubrimiento Dinámico**: No requiere configuración manual de servidores
- **Tolerancia a Fallos**: Los clientes pueden cambiar de servidor automáticamente
- **Escalabilidad**: Fácil agregar/quitar servidores sin reconfigurar clientes
- **Monitoreo**: El registry proporciona visibilidad del estado de todos los servidores

## Mejoras Futuras

- Persistencia del registro en base de datos
- Balanceo de carga más sofisticado
- Métricas y estadísticas de uso
- Autenticación y autorización
- Replicación del registry para alta disponibilidad

