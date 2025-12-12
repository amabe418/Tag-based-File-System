# Guía de Despliegue Distribuido con Docker Swarm

Esta guía explica cómo desplegar el sistema TBFS en dos computadoras distintas usando Docker Swarm con una red overlay compartida. **Los contenedores se gestionan manualmente**, permitiendo crear y eliminar contenedores según los necesites.

## Requisitos Previos

- Docker instalado en ambas máquinas
- Las dos máquinas deben poder comunicarse entre sí (misma red o acceso de red)
- Puertos necesarios abiertos en los firewalls:
  - **2377** (Swarm management)
  - **7946** (Swarm node communication)
  - **4789** (Overlay network)
  - **9000+** (Registry - puertos dinámicos según número)
  - **8010+** (MetaNameNode - puertos dinámicos según número)
  - **8001+** (DataNode - puertos dinámicos según número)
  - **8501** (Frontend)

## Scripts Disponibles

Solo necesitas **3 scripts principales**:

1. **`swarm-init.sh`** - Inicializa Docker Swarm y crea la red overlay (Máquina 1 - Manager)
2. **`swarm-join.sh`** - Une un nodo al swarm (Máquina 2 - Worker)
3. **`swarm-create-container.sh`** - Crea un contenedor (cualquier nodo)

**Script auxiliar opcional:**
- **`swarm-build.sh`** - Construye las imágenes Docker (ejecutar en ambas máquinas antes de crear contenedores)

## Pasos de Despliegue

### Paso 1: Construir Imágenes (Opcional pero Recomendado)

En **ambas máquinas**, construir las imágenes Docker:

```bash
./swarm-build.sh
```

O si necesitas reconstruir:

```bash
./swarm-build.sh --rebuild
```

**Nota**: Si las imágenes ya están construidas, puedes omitir este paso.

### Paso 2: Inicializar Swarm en la Máquina 1 (Manager)

En la **primera máquina** (que será el manager):

```bash
# Opción 1: Detectar IP automáticamente
./swarm-init.sh

# Opción 2: Especificar IP manualmente
./swarm-init.sh <IP_DE_LA_MAQUINA_1>
```

Este script:
- Inicializa Docker Swarm
- Crea la red overlay `tbfs_net`
- Muestra el token para unirse como worker

**IMPORTANTE**: Guarda el token de worker que se muestra, lo necesitarás en el paso siguiente.

### Paso 3: Unir la Máquina 2 al Swarm

En la **segunda máquina** (que será el worker):

```bash
./swarm-join.sh <IP_DE_LA_MAQUINA_1> <WORKER_TOKEN>
```

Si no tienes el token, puedes obtenerlo ejecutando en el manager:
```bash
docker swarm join-token worker -q
```

### Paso 4: Verificar el Swarm

En el **manager**, verificar que ambas máquinas están conectadas:

```bash
docker node ls
```

Deberías ver 2 nodos: uno como "Leader" (manager) y otro como "Active" (worker).

### Paso 5: Crear Contenedores

Ahora puedes crear los contenedores que necesites, **uno por uno**, en cualquier nodo:

```bash
# Crear Registry nodes
./swarm-create-container.sh registry 1
./swarm-create-container.sh registry 2
./swarm-create-container.sh registry 3

# Crear MetaNameNode nodes
./swarm-create-container.sh namenode 1
./swarm-create-container.sh namenode 2
./swarm-create-container.sh namenode 3

# Crear DataNode nodes
./swarm-create-container.sh datanode 1
./swarm-create-container.sh datanode 2
./swarm-create-container.sh datanode 3

# Crear Frontend (opcional)
./swarm-create-container.sh frontend 1
```

**Nota importante**: Los contenedores se crean en el nodo donde ejecutas el comando. Para crear un contenedor en un nodo específico, ejecuta el comando en ese nodo o usa SSH.

## Gestión de Contenedores

### Ver contenedores

```bash
# Ver todos los contenedores en la red overlay
docker ps --filter "network=tbfs_net"

# Ver todos los contenedores (incluyendo detenidos)
docker ps -a --filter "network=tbfs_net"
```

### Ver logs

```bash
docker logs -f <container-name>
# Ejemplo:
docker logs -f tbfs-namenode-1
```

### Detener/Iniciar contenedores

```bash
docker stop <container-name>      # Detener
docker start <container-name>     # Iniciar
docker restart <container-name>  # Reiniciar
```

### Eliminar contenedores

```bash
# Detener y eliminar
docker stop <container-name>
docker rm <container-name>

# O forzar eliminación
docker rm -f <container-name>
```

## Comandos Útiles

### Ver estado del Swarm

```bash
docker node ls                    # Ver nodos
docker network ls                 # Ver redes
docker network inspect tbfs_net  # Detalles de la red overlay
```

### Ver información de contenedores

```bash
docker ps                         # Contenedores corriendo
docker ps -a                      # Todos los contenedores
docker inspect <container-name>   # Información detallada
```

### Gestión de imágenes

```bash
docker images | grep tbfs         # Ver imágenes construidas
docker build -t tbfs-<tipo>:latest -f <tipo>/dockerfile.yml .  # Construir imagen manualmente
```

## Solución de Problemas

### El worker no se puede unir al swarm

1. Verificar que los puertos 2377, 7946, 4789 están abiertos en ambos firewalls
2. Verificar conectividad de red entre las máquinas:
   ```bash
   ping <IP_OTRA_MAQUINA>
   ```
3. Verificar que el token es correcto
4. Verificar que Docker está corriendo en ambas máquinas

### Los contenedores no se comunican entre sí

1. Verificar que la red overlay existe:
   ```bash
   docker network ls | grep overlay
   ```
2. Verificar que los contenedores están en la misma red:
   ```bash
   docker network inspect tbfs_net
   ```
3. Verificar logs de los contenedores para ver errores de conexión:
   ```bash
   docker logs <container-name>
   ```

### Un contenedor no inicia

1. Ver logs del contenedor:
   ```bash
   docker logs <container-name>
   ```
2. Verificar que la imagen existe:
   ```bash
   docker images | grep tbfs
   ```
3. Verificar estado del contenedor:
   ```bash
   docker ps -a | grep <container-name>
   ```
4. Intentar iniciar manualmente:
   ```bash
   docker start <container-name>
   docker logs -f <container-name>
   ```

### Verificar conectividad entre contenedores

Desde un contenedor, probar conectividad:
```bash
# En el manager, ejecutar un contenedor de prueba
docker run -it --rm --network tbfs_net alpine sh

# Dentro del contenedor:
ping tbfs-registry-1
ping tbfs-namenode-1
```

## Arquitectura Flexible

El despliegue es **completamente flexible**. Tú decides qué contenedores crear y en qué nodos colocarlos.

**Recomendaciones para alta disponibilidad:**

- **Registry**: Mínimo 3 nodos (distribuidos entre las máquinas)
- **Namenode**: Mínimo 3 nodos (distribuidos entre las máquinas)
- **Datanode**: Mínimo 3 nodos (para replicación de archivos)
- **Frontend**: 1 nodo (puede estar en cualquier máquina)

**Ejemplo de distribución recomendada:**

- **Máquina 1**: Registry-1, Registry-3, Namenode-1, Namenode-3, Datanode-1, Datanode-3, Datanode-5, Frontend
- **Máquina 2**: Registry-2, Namenode-2, Datanode-2, Datanode-4

## Notas Importantes

1. **Volúmenes persistentes**: Los volúmenes se crean automáticamente y persisten en cada nodo. Si eliminas un contenedor, los volúmenes NO se eliminan automáticamente.

2. **Puertos**: Los puertos se exponen en el nodo donde corre el contenedor. Para acceder desde fuera del swarm, usa la IP del nodo correspondiente.

3. **Red overlay**: La red overlay permite que los contenedores se comuniquen usando nombres de contenedor, independientemente del nodo donde estén corriendo.

4. **Gestión dinámica**: Puedes crear y eliminar contenedores en cualquier momento. Los nuevos contenedores se conectarán automáticamente a la red overlay.

5. **Tokens**: Guarda los tokens de forma segura. Si los pierdes, puedes regenerarlos:
   ```bash
   docker swarm join-token worker
   docker swarm join-token manager
   ```

6. **Colocación de contenedores**: Los contenedores se crean en el nodo donde ejecutas el comando. Para crear un contenedor en un nodo específico, ejecuta el comando en ese nodo o usa SSH.

## Limpieza

### Eliminar todos los contenedores

```bash
# Eliminar todos los contenedores en la red overlay
docker ps -a --filter "network=tbfs_net" --format "{{.Names}}" | xargs -r docker rm -f
```

### Salir del Swarm

**En el worker:**
```bash
docker swarm leave
```

**En el manager:**
```bash
docker swarm leave --force
```

⚠️ **ADVERTENCIA**: `docker swarm leave --force` eliminará el swarm completamente.
