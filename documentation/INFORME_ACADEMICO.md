# Informe Académico: Sistema de Archivos Distribuido Basado en Etiquetas (TBFS)

## Resumen Ejecutivo

El Tag-based File System (TBFS) es un sistema de archivos distribuido diseñado para almacenar y gestionar archivos mediante un sistema de etiquetado. El sistema implementa una arquitectura distribuida con separación de responsabilidades entre la gestión de metadatos y el almacenamiento físico, proporcionando alta disponibilidad, escalabilidad y tolerancia a fallos mediante técnicas de replicación y consenso distribuido.

---

## 1. Arquitectura del Sistema

### 1.1 Organización del Sistema Distribuido

El sistema TBFS está organizado en una arquitectura de tres capas principales que separan las responsabilidades de descubrimiento de servicios, gestión de metadatos y almacenamiento físico:

**Capa de Descubrimiento de Servicios (Registry Service):**
El Registry Service actúa como un servicio de descubrimiento centralizado que mantiene un catálogo de todos los servicios activos en el sistema. Este servicio permite que los clientes y otros componentes del sistema localicen dinámicamente los servicios disponibles sin necesidad de conocer direcciones IP o puertos específicos de antemano.

**Capa de Gestión de Metadatos (MetaNameNode):**
El MetaNameNode es responsable exclusivamente de la gestión de metadatos de los archivos, incluyendo nombres, etiquetas asociadas, identificadores únicos (hashes) y la ubicación de las réplicas físicas. Esta separación permite que el MetaNameNode sea ligero y se enfoque únicamente en operaciones de metadatos, sin almacenar los archivos físicos.

**Capa de Almacenamiento Físico (DataNodes):**
Los DataNodes son responsables del almacenamiento físico de los archivos en disco. Cada DataNode opera de forma independiente y reporta su estado al MetaNameNode, incluyendo información sobre espacio disponible y salud del nodo.

### 1.2 Roles del Sistema

**Registry Service:**
- Mantiene un registro distribuido de todos los servicios activos
- Proporciona mecanismos de descubrimiento para clientes y servicios
- Implementa consenso distribuido para garantizar consistencia del registro
- Detecta y elimina servicios inactivos automáticamente

**MetaNameNode:**
- Gestiona el catálogo de metadatos de archivos (nombres, etiquetas, hashes)
- Coordina la asignación de réplicas a DataNodes
- Mantiene registro del estado y disponibilidad de DataNodes
- Implementa lógica de re-replicación automática ante fallos
- Proporciona endpoints de API para operaciones de archivos (crear, leer, eliminar, buscar)

**DataNodes:**
- Almacenan archivos físicos en sistemas de archivos locales
- Reportan periódicamente su estado al MetaNameNode (heartbeats)
- Responden a solicitudes de lectura y escritura del MetaNameNode
- Gestionan espacio en disco y validan disponibilidad antes de almacenar

**Cliente:**
- Interfaz de usuario (frontend web y CLI) para interactuar con el sistema
- Consulta el Registry para descubrir servicios disponibles
- Se comunica con el MetaNameNode líder para operaciones de archivos
- Descarga archivos directamente desde DataNodes cuando es necesario

### 1.3 Distribución de Servicios en Redes Docker

El sistema está desplegado utilizando Docker Swarm, organizando los servicios en una red virtual dedicada (`tbfs_net`) que permite comunicación entre contenedores:

**Registry Service Cluster:**
- Tres réplicas del Registry Service (registry-1, registry-2, registry-3)
- Cada réplica expuesta en puertos diferentes del host (9000, 9001, 9002)
- Comunicación interna a través de nombres de servicio Docker

**MetaNameNode Cluster:**
- Tres réplicas del MetaNameNode (namenode-1, namenode-2, namenode-3)
- Cada réplica expuesta en puertos diferentes del host (8010, 8011, 8012)
- Volúmenes persistentes para bases de datos de metadatos
- Comunicación entre peers para replicación de metadatos

**DataNodes:**
- Cinco DataNodes por defecto (datanode-1 a datanode-5)
- Cada DataNode expuesto en puertos diferentes (8001-8005)
- Volúmenes persistentes para almacenamiento de archivos
- Escalabilidad dinámica (pueden agregarse o removerse)

**Frontend:**
- Un contenedor de frontend (Streamlit) expuesto en puerto 8501
- Acceso a la red interna para comunicación con servicios backend

---

## 2. Procesos del Sistema

### 2.1 Tipos de Procesos

El sistema está compuesto por cuatro tipos principales de procesos, cada uno con responsabilidades específicas:

**Procesos de Registry:**
Cada nodo del Registry Service ejecuta un proceso FastAPI que mantiene un registro en memoria de servicios activos. Estos procesos implementan un algoritmo de consenso tipo Raft para garantizar que todos los nodos del Registry tengan una visión consistente del estado del sistema.

**Procesos de MetaNameNode:**
Cada réplica del MetaNameNode ejecuta un proceso FastAPI que gestiona metadatos almacenados en bases de datos SQLite. Estos procesos implementan elección de líder y replicación de operaciones para mantener consistencia entre réplicas.

**Procesos de DataNode:**
Cada DataNode ejecuta un proceso FastAPI independiente que gestiona almacenamiento local de archivos. Estos procesos operan de forma autónoma pero se coordinan con el MetaNameNode para reportar estado y recibir instrucciones de almacenamiento.

**Procesos de Cliente:**
El frontend ejecuta un proceso Streamlit que proporciona una interfaz web interactiva. El cliente CLI ejecuta scripts Python que interactúan directamente con la API del MetaNameNode.

### 2.2 Organización de Procesos

**Agrupación por Instancia:**
Cada servicio está encapsulado en un contenedor Docker independiente, proporcionando aislamiento y portabilidad. Los procesos relacionados (como múltiples réplicas del mismo servicio) se ejecutan en instancias separadas pero idénticas, permitiendo escalabilidad horizontal.

**Arquitectura de Microservicios:**
El sistema sigue un patrón de microservicios donde cada componente es un servicio independiente con su propia base de datos o almacenamiento. Esta arquitectura permite:
- Desarrollo y despliegue independiente de cada servicio
- Escalabilidad selectiva según necesidades
- Tolerancia a fallos parciales (un servicio puede fallar sin afectar otros)

### 2.3 Patrones de Diseño de Desempeño

**Modelo de Hilos (Threading):**
El sistema utiliza hilos para operaciones concurrentes dentro de cada proceso:
- Hilos de monitoreo para detectar nodos inactivos
- Hilos de heartbeat para mantener comunicación periódica
- Hilos de elección de líder para procesos de consenso

**Modelo Asíncrono:**
Las operaciones de entrada/salida (I/O) utilizan operaciones asíncronas cuando es posible, especialmente en:
- Peticiones HTTP entre servicios
- Operaciones de lectura/escritura de archivos
- Operaciones de base de datos

**Procesos Independientes:**
Cada servicio se ejecuta como un proceso independiente, permitiendo:
- Aprovechamiento de múltiples núcleos de CPU
- Aislamiento de fallos
- Escalabilidad horizontal mediante adición de instancias

**Patrón de Líder-Seguidor:**
Tanto el Registry como el MetaNameNode implementan un patrón de líder-seguidor donde:
- Un nodo actúa como líder y procesa todas las operaciones de escritura
- Los nodos seguidores replican las operaciones del líder
- En caso de fallo del líder, se realiza una elección para seleccionar un nuevo líder

---

## 3. Comunicación del Sistema

### 3.1 Tipo de Comunicación

El sistema utiliza **REST (Representational State Transfer)** como protocolo principal de comunicación. Todos los servicios exponen APIs RESTful utilizando FastAPI, que proporciona:
- Comunicación basada en HTTP/HTTPS
- Serialización JSON para intercambio de datos
- Documentación automática de APIs
- Validación de datos mediante modelos Pydantic

**Ventajas de REST:**
- Simplicidad de implementación y depuración
- Compatibilidad con herramientas estándar (curl, navegadores)
- Independencia de lenguaje y plataforma
- Facilidad de integración con sistemas externos

### 3.2 Comunicación Cliente-Servidor

**Cliente → Registry:**
Los clientes consultan el Registry Service para descubrir servicios disponibles. Esta comunicación es de solo lectura y utiliza peticiones HTTP GET para obtener listas de servicios activos.

**Cliente → MetaNameNode:**
Los clientes se comunican directamente con el MetaNameNode líder para:
- Subir archivos (POST)
- Descargar archivos (GET)
- Buscar archivos por etiquetas (GET)
- Gestionar etiquetas (POST, DELETE)

El MetaNameNode implementa redirección HTTP 307 (Temporary Redirect) para dirigir peticiones de clientes que se conectan a nodos no-líder hacia el líder actual.

**Cliente → DataNode:**
Los clientes no se comunican directamente con DataNodes. Toda la comunicación pasa a través del MetaNameNode, que actúa como intermediario y coordina las operaciones con los DataNodes.

### 3.3 Comunicación Servidor-Servidor

**Registry ↔ Registry:**
Los nodos del Registry se comunican entre sí para:
- Elección de líder mediante votación
- Replicación de registros de servicios
- Heartbeats para detectar nodos inactivos
- Sincronización de estado del cluster

**MetaNameNode ↔ MetaNameNode:**
Las réplicas del MetaNameNode se comunican para:
- Elección de líder mediante votación
- Replicación de operaciones de metadatos
- Heartbeats para mantener coherencia
- Sincronización de bases de datos de metadatos

**MetaNameNode → DataNode:**
El MetaNameNode se comunica con DataNodes para:
- Registrar nuevos DataNodes en el sistema
- Recibir heartbeats periódicos con información de estado
- Enviar archivos para almacenamiento (escritura)
- Solicitar archivos para lectura
- Eliminar archivos de almacenamiento
- Coordinar re-replicación de archivos

**DataNode → Registry:**
Los DataNodes consultan el Registry para descubrir la ubicación del MetaNameNode líder, permitiendo registro automático y actualización de estado.

### 3.4 Comunicación entre Procesos

**Comunicación Inter-proceso dentro del mismo servicio:**
Dentro de cada proceso, los hilos se comunican mediante:
- Variables de estado compartidas protegidas por locks (mutex)
- Colas de mensajes para operaciones asíncronas
- Eventos y señales para sincronización

**Comunicación Inter-proceso entre servicios:**
La comunicación entre servicios diferentes se realiza exclusivamente mediante peticiones HTTP REST, proporcionando:
- Desacoplamiento entre servicios
- Independencia de implementación
- Facilidad de escalabilidad y despliegue

---

## 4. Coordinación del Sistema

### 4.1 Sincronización de Acciones

**Sincronización de Operaciones de Escritura:**
Todas las operaciones de escritura (crear, modificar, eliminar archivos) deben ser procesadas por el MetaNameNode líder. Los nodos no-líder redirigen automáticamente las peticiones de escritura al líder, garantizando que solo un nodo procese modificaciones en un momento dado.

**Sincronización de Replicación:**
Cuando el líder procesa una operación de escritura, replica la operación a todos los nodos seguidores antes de confirmar la operación al cliente. Esto garantiza que todos los nodos tengan una visión consistente de los metadatos.

**Sincronización de Heartbeats:**
Los DataNodes envían heartbeats periódicos al MetaNameNode para reportar su estado. El MetaNameNode utiliza esta información para mantener un registro actualizado de DataNodes activos y detectar fallos.

### 4.2 Acceso Exclusivo a Recursos

**Protección de Bases de Datos:**
Las operaciones de base de datos en el MetaNameNode están protegidas mediante locks (mutex) que garantizan acceso exclusivo a las estructuras de datos compartidas. Esto previene condiciones de carrera cuando múltiples hilos intentan acceder simultáneamente a la base de datos.

**Protección del Estado del Cluster:**
El estado del cluster (información sobre líder, términos, peers) está protegido por locks para garantizar que las actualizaciones sean atómicas y consistentes.

**Gestión de Locks:**
El sistema utiliza locks de lectura-escritura donde sea apropiado, permitiendo múltiples lecturas concurrentes mientras se bloquean las escrituras.

### 4.3 Toma de Decisiones Distribuidas

**Elección de Líder (Leader Election):**
Tanto el Registry como el MetaNameNode implementan algoritmos de elección de líder tipo Raft:
- Cuando un nodo detecta la ausencia de un líder, inicia una elección
- Los nodos votan por candidatos basándose en términos y prioridad
- El candidato que recibe mayoría de votos se convierte en líder
- El líder mantiene su posición enviando heartbeats periódicos

**Consenso en Replicación:**
Las operaciones de escritura requieren consenso entre réplicas:
- El líder propone una operación
- Los seguidores confirman la recepción
- La operación se confirma cuando la mayoría de réplicas confirman
- Si una réplica falla, la mayoría restante puede continuar operando

**Decisión de Asignación de Réplicas:**
El MetaNameNode toma decisiones distribuidas sobre dónde almacenar réplicas de archivos:
- Evalúa el estado de todos los DataNodes disponibles
- Considera espacio disponible, carga y estado de salud
- Asigna réplicas de forma balanceada para distribuir carga
- Excluye DataNodes en proceso de drenaje o inactivos

---

## 5. Nombrado y Localización

### 5.1 Identificación de Datos y Servicios

**Identificación de Archivos:**
Los archivos se identifican mediante:
- **Nombre original:** El nombre proporcionado por el usuario
- **Hash SHA-256:** Identificador único derivado del contenido del archivo
- **ID de archivo:** Identificador numérico único en la base de datos de metadatos

El hash SHA-256 garantiza que archivos con contenido idéntico tengan el mismo identificador, permitiendo deduplicación y verificación de integridad.

**Identificación de Servicios:**
Los servicios se identifican mediante:
- **NODE_ID:** Identificador único de cada instancia (ej: "namenode-1", "datanode-3")
- **Nombre de servicio Docker:** Nombre canónico para comunicación en red (ej: "tbfs-namenode-1")
- **URL completa:** Combinación de protocolo, host y puerto (ej: "http://tbfs-namenode-1:8010")

**Identificación de DataNodes:**
Cada DataNode tiene un identificador único que persiste a través de reinicios, permitiendo al MetaNameNode rastrear qué archivos están almacenados en cada DataNode.

### 5.2 Ubicación de Datos y Servicios

**Ubicación de Metadatos:**
Los metadatos se almacenan en bases de datos SQLite locales en cada réplica del MetaNameNode. Cada réplica mantiene una copia completa de los metadatos, garantizando disponibilidad incluso si algunas réplicas fallan.

**Ubicación de Archivos Físicos:**
Los archivos físicos se almacenan en sistemas de archivos locales de los DataNodes. La estructura de almacenamiento organiza archivos por los primeros caracteres de su hash para distribuir la carga y facilitar la búsqueda.

**Ubicación de Servicios:**
Los servicios se localizan mediante:
- **Registry Service:** Mantiene un catálogo centralizado de servicios activos
- **Nombres de servicio Docker:** Permiten resolución de nombres dentro de la red Docker
- **Puertos expuestos:** Permiten acceso desde el host para clientes externos

### 5.3 Localización de Datos y Servicios

**Descubrimiento de Servicios:**
El Registry Service proporciona mecanismos de descubrimiento:
- Los clientes consultan el Registry para obtener listas de servicios disponibles
- El Registry retorna información sobre servicios activos, incluyendo URLs y estado
- Los servicios se registran automáticamente al iniciar

**Localización del Líder:**
Para localizar el MetaNameNode líder:
- Los clientes consultan cualquier réplica del MetaNameNode
- Cada réplica responde con información sobre el líder actual
- Los clientes se redirigen automáticamente al líder para operaciones de escritura

**Localización de Réplicas de Archivos:**
Para localizar réplicas de un archivo específico:
- El MetaNameNode mantiene un registro de qué DataNodes almacenan cada archivo
- Cuando se solicita un archivo, el MetaNameNode consulta su base de datos
- Retorna una lista de DataNodes que contienen el archivo, ordenada por prioridad
- El sistema intenta leer desde el DataNode primario, con fallback a réplicas secundarias

**Búsqueda por Etiquetas:**
Los archivos se localizan mediante búsqueda por etiquetas:
- Los metadatos incluyen asociaciones entre archivos y etiquetas
- Las consultas filtran archivos que contienen todas las etiquetas especificadas
- El MetaNameNode procesa las consultas localmente en su base de datos

---

## 6. Consistencia y Replicación

### 6.1 Distribución de Datos

**Distribución de Metadatos:**
Los metadatos se distribuyen mediante replicación completa en todas las réplicas del MetaNameNode. Cada réplica mantiene una copia completa de la base de datos de metadatos, garantizando que cualquier réplica pueda responder a consultas de lectura.

**Distribución de Archivos Físicos:**
Los archivos físicos se distribuyen entre múltiples DataNodes mediante un esquema de réplicas. Cada archivo se almacena en exactamente tres DataNodes diferentes, proporcionando redundancia y tolerancia a fallos.

**Estrategia de Distribución:**
La asignación de réplicas utiliza un algoritmo que considera:
- Distribución equitativa de carga entre DataNodes
- Disponibilidad de espacio en cada DataNode
- Estado de salud y actividad de los DataNodes
- Exclusión de DataNodes en proceso de drenaje

### 6.2 Replicación

**Réplicas de Metadatos:**
El sistema mantiene tres réplicas de metadatos (una en cada MetaNameNode). Las operaciones de escritura se replican a todas las réplicas antes de confirmarse, garantizando consistencia fuerte.

**Réplicas de Archivos:**
Cada archivo se replica en tres DataNodes diferentes. El sistema garantiza que al menos dos de las tres réplicas se almacenen exitosamente antes de confirmar una operación de escritura, proporcionando tolerancia a fallos de un DataNode.

**Réplicas del Registry:**
El Registry Service mantiene tres réplicas que sincronizan su estado mediante consenso distribuido, garantizando que todos los nodos tengan una visión consistente de los servicios registrados.

### 6.3 Confiabilidad de Réplicas Tras Actualización

**Modelo de Consistencia:**
El sistema implementa consistencia fuerte (strong consistency) para metadatos:
- Todas las réplicas del MetaNameNode deben confirmar una operación antes de que se considere completada
- Si una réplica falla durante una operación, la operación se aborta y se reintenta
- Los clientes siempre leen de una réplica actualizada

**Modelo de Replicación de Archivos:**
Para archivos físicos, el sistema utiliza un modelo de consistencia eventual con garantías de disponibilidad:
- El sistema requiere que al menos dos de tres réplicas se almacenen exitosamente
- Si una réplica falla durante la escritura, el sistema puede continuar con las réplicas restantes
- Las réplicas fallidas se re-replican automáticamente cuando se detecta el fallo

**Verificación de Integridad:**
Los archivos se identifican mediante hash SHA-256, permitiendo:
- Verificación de integridad al leer archivos
- Detección de corrupción de datos
- Deduplicación de archivos idénticos

**Re-replicación Automática:**
El sistema monitorea continuamente el estado de los DataNodes. Cuando se detecta que un DataNode está inactivo:
- Se identifican todos los archivos afectados
- Se lee cada archivo desde una réplica activa
- Se re-replica el archivo a un nuevo DataNode disponible
- Se actualiza el registro de réplicas en el MetaNameNode

---

## 7. Tolerancia a Fallos

### 7.1 Respuesta a Errores

**Detección de Fallos:**
El sistema implementa múltiples mecanismos de detección de fallos:
- **Heartbeats:** Los servicios envían señales periódicas de vida. La ausencia de heartbeats indica un fallo potencial
- **Timeouts:** Las peticiones HTTP tienen timeouts configurados para detectar servicios no responsivos
- **Verificación de salud:** Endpoints de health check permiten verificar el estado de servicios

**Manejo de Fallos de MetaNameNode:**
- Si el líder falla, los nodos seguidores detectan la ausencia de heartbeats
- Se inicia automáticamente una elección de líder
- El nuevo líder asume el control y continúa procesando operaciones
- Los clientes se redirigen automáticamente al nuevo líder

**Manejo de Fallos de DataNode:**
- El MetaNameNode detecta DataNodes inactivos mediante ausencia de heartbeats
- Se identifican todos los archivos almacenados en el DataNode fallido
- Se inicia automáticamente un proceso de re-replicación
- Los archivos se re-replican desde réplicas activas a nuevos DataNodes

**Manejo de Fallos de Registry:**
- Si el líder del Registry falla, se realiza una elección de líder
- El nuevo líder asume el catálogo de servicios
- Los servicios continúan operando normalmente durante la transición

### 7.2 Nivel de Tolerancia a Fallos Esperado

**Tolerancia a Fallos de MetaNameNode:**
El sistema puede tolerar el fallo de hasta un nodo del MetaNameNode (de tres totales) manteniendo operaciones normales. Con dos réplicas activas, el sistema puede:
- Continuar procesando operaciones de lectura y escritura
- Mantener consenso para operaciones de escritura
- Realizar elecciones de líder si es necesario

**Tolerancia a Fallos de DataNode:**
El sistema puede tolerar el fallo simultáneo de hasta un DataNode (de tres réplicas por archivo) sin pérdida de datos. Con dos réplicas activas, el sistema puede:
- Continuar sirviendo solicitudes de lectura
- Re-replicar archivos afectados automáticamente
- Mantener disponibilidad de todos los archivos

**Tolerancia a Fallos de Registry:**
El sistema puede tolerar el fallo de hasta un nodo del Registry (de tres totales) manteniendo funcionalidad completa. Con dos réplicas activas, el Registry puede:
- Continuar proporcionando descubrimiento de servicios
- Mantener consenso para actualizaciones del catálogo
- Realizar elecciones de líder si es necesario

### 7.3 Fallos Parciales

**Nodos Caídos Temporalmente:**
El sistema maneja nodos que fallan temporalmente y luego se recuperan:
- Los nodos que se reinician se registran automáticamente al volver en línea
- Los DataNodes que se recuperan pueden contener archivos que fueron re-replicados durante su ausencia
- El sistema detecta archivos duplicados y puede limpiarlos si es necesario
- Los nodos se reintegran automáticamente al sistema sin intervención manual

**Nodos Nuevos que se Incorporan:**
El sistema soporta la incorporación dinámica de nuevos nodos:
- Los nuevos DataNodes se registran automáticamente con el MetaNameNode al iniciar
- El MetaNameNode comienza a asignar nuevas réplicas a los DataNodes nuevos
- Los nuevos nodos se integran gradualmente en la distribución de carga
- No se requiere reinicio del sistema para incorporar nuevos nodos

**Drenaje Controlado de Nodos:**
El sistema implementa un mecanismo de drenaje controlado para remover DataNodes de forma segura:
- Un DataNode puede marcarse para drenaje, evitando nuevas asignaciones
- Todos los archivos almacenados en el DataNode se re-replican a otros DataNodes
- Una vez completado el drenaje, el DataNode puede ser removido sin pérdida de datos
- Este proceso permite mantenimiento planificado sin interrupciones

**Recuperación Automática:**
El sistema implementa recuperación automática de fallos:
- Los procesos de re-replicación se ejecutan automáticamente en segundo plano
- El sistema restaura automáticamente el nivel de replicación deseado
- Los clientes experimentan mínima interrupción durante la recuperación
- No se requiere intervención manual para la mayoría de los fallos

---

## 8. Seguridad del Sistema

### 8.1 Seguridad en la Comunicación

**Protocolo de Comunicación:**
Actualmente, el sistema utiliza HTTP para todas las comunicaciones. En un entorno de producción, se recomendaría:
- Migración a HTTPS para cifrado de comunicaciones
- Uso de certificados TLS para autenticación de servicios
- Validación de certificados para prevenir ataques man-in-the-middle

**Comunicación en Red Privada:**
Los servicios se comunican a través de una red Docker privada, proporcionando:
- Aislamiento de la red pública
- Prevención de acceso no autorizado desde fuera de la red
- Control de tráfico mediante configuración de red Docker

**Validación de Datos:**
El sistema implementa validación de datos en todas las APIs:
- Validación de tipos de datos mediante modelos Pydantic
- Validación de formatos (ej: hashes SHA-256)
- Sanitización de entradas para prevenir inyección de datos

### 8.2 Seguridad en el Diseño

**Separación de Responsabilidades:**
La arquitectura de separación entre MetaNameNode y DataNodes proporciona:
- Limitación del alcance de un compromiso potencial
- Un DataNode comprometido no puede acceder directamente a metadatos
- Un MetaNameNode comprometido no puede acceder directamente a archivos físicos

**Aislamiento de Contenedores:**
Cada servicio se ejecuta en un contenedor Docker aislado:
- Límites de recursos (CPU, memoria) por contenedor
- Aislamiento de sistemas de archivos
- Prevención de interferencia entre servicios

**Gestión de Volúmenes:**
Los volúmenes persistentes están aislados por servicio:
- Cada MetaNameNode tiene su propio volumen de base de datos
- Cada DataNode tiene su propio volumen de almacenamiento
- Prevención de acceso cruzado entre volúmenes

**Validación de Integridad:**
Los archivos se identifican mediante hashes SHA-256:
- Detección de corrupción de datos
- Verificación de integridad al leer archivos
- Prevención de modificación no autorizada de archivos

### 8.3 Autorización y Autenticación

**Estado Actual:**
El sistema actualmente no implementa autenticación ni autorización. Todos los servicios son accesibles sin credenciales, lo cual es apropiado para un entorno de desarrollo o red privada confiable.

**Recomendaciones para Producción:**
Para un entorno de producción, se recomendaría implementar:

**Autenticación de Clientes:**
- Sistema de autenticación basado en tokens (JWT)
- Validación de credenciales antes de permitir operaciones
- Gestión de sesiones para clientes autenticados

**Autorización Basada en Roles:**
- Control de acceso basado en roles (RBAC)
- Permisos diferenciados para lectura y escritura
- Auditoría de operaciones para trazabilidad

**Autenticación Entre Servicios:**
- Autenticación mutua TLS (mTLS) entre servicios
- Tokens de servicio para comunicación inter-servicio
- Validación de identidad de servicios antes de aceptar peticiones

**Gestión de Secretos:**
- Almacenamiento seguro de credenciales y claves
- Rotación periódica de secretos
- Uso de sistemas de gestión de secretos (ej: HashiCorp Vault)

**Limitación de Acceso:**
- Firewall y reglas de red para limitar acceso
- Whitelisting de IPs permitidas
- Rate limiting para prevenir abuso

---

## Conclusiones

El sistema TBFS implementa una arquitectura distribuida robusta que separa efectivamente las responsabilidades de descubrimiento de servicios, gestión de metadatos y almacenamiento físico. La implementación de técnicas de replicación, consenso distribuido y tolerancia a fallos proporciona un sistema altamente disponible y escalable.

El uso de REST para comunicación, contenedores Docker para despliegue, y algoritmos de consenso tipo Raft para coordinación, resultan en un sistema que es tanto técnicamente sólido como práctico de mantener y escalar. La separación de metadatos y almacenamiento físico permite optimizaciones independientes y escalabilidad horizontal.

Las áreas identificadas para mejora futura incluyen la implementación de seguridad más robusta (autenticación, autorización, cifrado) y optimizaciones adicionales para rendimiento en cargas de trabajo de gran escala.

