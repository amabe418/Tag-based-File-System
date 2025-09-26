# 📂 Proyecto Sistemas Distribuidos

Este repositorio contiene el desarrollo de un sistema de gestión de ficheros con **etiquetas**, primero en una **versión centralizada** y luego en una **versión distribuida**.  

## 📌 Roadmap de 2 meses

El trabajo se organiza en **8 semanas**, desde el análisis inicial hasta la implementación distribuida con tolerancia a fallos.

---

### 🗓️ Semana 1 – Análisis y diseño inicial
- [ ] Entender requerimientos (add, delete, list, add-tags, delete-tags).
- [ ] Definir estructura de datos para la versión centralizada:
  - Tabla de ficheros: `{nombre → {etiquetas}}`.
- [ ] Soporte de consultas por etiquetas (subset matching).
- [ ] Diseñar interfaz de consola.
- [ ] Entregar diagrama simple de clases y flujo de operaciones.

---

### 🗓️ Semana 2 – Implementación centralizada básica
- [ ] Implementar funciones:
  - `add file-list tag-list`
  - `list tag-query`
- [ ] Guardar metadatos en memoria.
- [ ] Probar con ejemplos sencillos.

---

### 🗓️ Semana 3 – Ampliación centralizada
- [ ] Implementar:
  - `delete tag-query`
  - `add-tags tag-query tag-list`
  - `delete-tags tag-query tag-list`
- [ ] Agregar persistencia simple (JSON/SQLite).
- [ ] Documentar casos de uso.

---

### 🗓️ Semana 4 – Validación centralizada
- [ ] Pruebas exhaustivas con ficheros y etiquetas.
- [ ] Optimizar consultas de etiquetas (índices o `set` en Python).
- [ ] Preparar informe de la versión centralizada.

---

### 🗓️ Semana 5 – Diseño distribuido
- [ ] Definir roles de nodos:
  - Nodo de almacenamiento
  - Nodo de enrutamiento/consulta
  - Nodo coordinador (opcional)
- [ ] Diseñar comunicación entre nodos (sockets, gRPC o REST).
- [ ] Diseñar estrategia de replicación (mínimo 2 copias por fichero).

---

### 🗓️ Semana 6 – Implementación distribuida inicial
- [ ] Implementar nodos básicos:
  - Nodo almacenamiento
  - Nodo cliente (interfaz consola)
- [ ] Definir protocolo de mensajes (JSON sobre sockets/HTTP).
- [ ] Permitir operaciones `add` y `list` en modo distribuido.

---

### 🗓️ Semana 7 – Tolerancia a fallos y ampliación distribuida
- [ ] Añadir replicación de datos.
- [ ] Implementar reconexión automática de nodos.
- [ ] Soportar comandos restantes (`delete`, `add-tags`, `delete-tags`).

---

### 🗓️ Semana 8 – Integración y presentación
- [ ] Pruebas con varios nodos en red local o Docker.
- [ ] Medir disponibilidad (qué pasa si cae un nodo).
- [ ] Documentar todo:
  - Comparación centralizado vs distribuido
  - Diagramas de arquitectura
  - Casos de uso y resultados
- [ ] Preparar presentación final.

---

## 📊 Gestión visual del proyecto
📌 Además del plan escrito, el avance semanal se gestionará en **[GitHub Projects](../../projects)** con un tablero Kanban:  
- **To Do** → tareas aún no iniciadas  
- **In Progress** → lo que se está desarrollando  
- **Done** → tareas completadas  

---

✨ Con este README se puede ver el plan general y, desde el tablero de Projects, seguir el avance visualmente.
