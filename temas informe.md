### Temáticas a contener en el informe:

1. Arquitectura o el problema de como diseñar el sistema.

* Organización de su sistema distribuido
* Roles de su sistema
* Distribución de servicios en ambas redes de docker

2. Procesos o el problema de cuantos programas o servicios posee el sistema

* Tipos de procesos dentro del sistema.
* Organización o agrupación de los procesos en una instancia, o en varias según su arquitectura
* Tipo de patrón de diseño con respecto al desempeño, async, hilos, procesos o algun subconjunto de los mismos.

3. Comunicación o el problema de como enviar información mediante la red

* Tipo de comunicación, rpc, sockets, rest, objetos remotos, patrones de mensajes, etc
* Comunicación cliente - servidor y servidor - servidor
* Comunicación entre procesos.

4. Coordinación o el problema de poner todos los servicios de acuerdo

* Sincronización de acciones. ( de ser necesario )
* Acceso exclusivo a recursos. Condiciones de carrera
* Toma de decisiones distribuidas.

5. Nombrado y Localización o el problema de dónde se encuentra un recurso y como llegar al mismo

* Identificación de los datos y servicios
* Ubicación de los datos y servicios
* Localización de los datos y servicios

6. Consistencia y Replicación o el problema de solucionar los problemas que surgen a partir de tener varias copias de un mismo dato en el sistema.

* Distribución de los datos
* Replicación, cantidad de réplicas.
* Confiabilidad de las réplicas de los datos tras una actualización.

7. Tolerancia a fallas o el problema de, para que pasar tanto trabajo distribuyendo datos y servicios si al fallar una componente del sistema todo se viene abajo.

* Respuesta a errores
* Nivel de tolerancia a fallos esperado.
* Fallos parciales. Nodos caídos temporalmente. Nodos nuevos que se incorporan al sistema.

8. Seguridad o el problema de que tan vulnerable es su diseño

* seguridad con respecto a la comunicación
* seguridad con respecto al diseño
* Autorización y autenticación.