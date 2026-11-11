## Propósito y alcance

Esta guía describe cómo detectar una falla y recuperar cada componente del sistema web para análisis inteligente de imágenes desplegado en Amazon Web Services (AWS): las réplicas de la aplicación, los nodos HAProxy, el Application Load Balancer (ALB), la base de datos Amazon RDS y el bucket de Amazon S3. También describe los respaldos disponibles y el procedimiento para actualizar la aplicación sin interrumpir el servicio. El diseño sigue el pilar de fiabilidad de AWS: detectar la falla, aislarla y recuperar automáticamente cuando sea posible (Amazon Web Services [AWS], s.f.-c).

**Tabla 1**

*Componentes, redundancia y recuperación automática*

| Componente | Redundancia | Recuperación automática | Tiempo de detección medido |
| --- | --- | --- | --- |
| Réplica de la aplicación (app-a, app-b) | 2 réplicas en 2 zonas | systemd reinicia el proceso si termina; HAProxy deja de enviarle tráfico | Unos 15 s (3 comprobaciones de 5 s), sin errores para el usuario |
| Nodo HAProxy (haproxy-a, haproxy-b) | 2 nodos en 2 zonas | El ALB deja de enviarle tráfico | Unos 9 s (2 comprobaciones de 5 s) |
| Application Load Balancer | Servicio administrado multizona | Administrada por AWS | No aplica |
| Base de datos RDS | 1 instancia (Single-AZ) | RDS reinicia la instancia ante fallas del proceso | Respaldos automáticos de 7 días |
| Bucket de S3 | Almacenamiento administrado y versionado | Administrada por AWS | No aplica |

*Nota.* Los tiempos provienen de las pruebas P-08 y P-09 del informe final. Elaboración propia.

## Acceso necesario

Todos los procedimientos se ejecutan desde la consola de AWS, en la región us-east-1 (N. Virginia). No se usan llaves SSH: los servidores se administran con AWS Systems Manager (Run Command y Session Manager) desde AWS CloudShell, que se abre con el ícono `>_` de la consola. Para enviar un comando a un servidor se usa su etiqueta `Name`:

```bash
aws ssm send-command --targets Key=tag:Name,Values=app-a \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl status imgapp --no-pager"]' \
  --query Command.CommandId --output text
```

La salida de un comando se consulta con su identificador:

```bash
aws ssm list-command-invocations --command-id ID_DEL_COMANDO --details \
  --query 'CommandInvocations[].CommandPlugins[].Output' --output text
```

## Diagnóstico inicial

Ante cualquier reporte de falla, primero se identifica la capa afectada con las comprobaciones de la Tabla 2.

**Tabla 2**

*Síntomas y comprobaciones*

| Síntoma | Componente probable | Comprobación |
| --- | --- | --- |
| El sitio no responde o devuelve 503 | ALB o ambos HAProxy | EC2 → Grupos de destino → `imgproj-tg-haproxy` → Destinos: estado de haproxy-a y haproxy-b |
| El sitio responde, pero una réplica no aparece en `X-Served-By` | Réplica de la aplicación | Estado de los backends en HAProxy (comando siguiente) |
| `/health` responde 503 en ambas réplicas | Base de datos RDS | RDS → Bases de datos → `imgproj-db-imgproj`: estado y eventos |
| Error al subir o al ver imágenes | Bucket de S3 o permisos | Registros de la aplicación con `journalctl -u imgapp` |

*Nota.* Elaboración propia.

Estado de los backends visto desde un nodo HAProxy:

```bash
aws ssm send-command --targets Key=tag:Name,Values=haproxy-a \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s \"http://localhost:8404/stats;csv\" | cut -d, -f1,2,18 | grep be_app"]' \
  --query Command.CommandId --output text
```

Estado de los destinos del ALB:

```bash
TG=$(aws elbv2 describe-target-groups --names imgproj-tg-haproxy \
  --query 'TargetGroups[0].TargetGroupArn' --output text)
aws elbv2 describe-target-health --target-group-arn $TG \
  --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text
```

## Recuperación de una réplica de la aplicación

**Efecto en el servicio.** Ninguno. HAProxy marca la réplica como DOWN y reintenta las solicitudes en la otra réplica (prueba P-08: 0 errores).

### Caso 1: el servicio se detuvo o falla

1. Revisar los registros: `journalctl -u imgapp -n 80 --no-pager`.
2. Reiniciar el servicio con `systemctl restart imgapp`, mediante Run Command.
3. Comprobar con `curl -s localhost:5000/health`, que debe responder `{"status":"ok"}`. HAProxy vuelve a marcar la réplica como UP después de 2 comprobaciones correctas.

### Caso 2: la instancia está detenida o no responde

1. EC2 → Instancias → seleccionar la réplica → **Estado de la instancia → Iniciar** (o **Reiniciar**).
2. El servicio `imgapp` está habilitado en systemd y arranca solo al encender la instancia. La IP privada no cambia, así que HAProxy la reincorpora sin cambios.

### Caso 3: la instancia fue terminada

CloudFormation no vuelve a crear un recurso eliminado fuera de la plantilla: una actualización del stack con la misma plantilla no detecta la diferencia. Por eso la réplica se reemplaza así:

1. EC2 → Instancias → seleccionar la réplica que sigue activa (por ejemplo, app-b) → **Acciones → Imágenes y plantillas → Lanzar más como esta**. Esta opción copia la imagen, el tipo, la subred, los grupos de seguridad, el perfil de IAM y el *user data* (AWS, s.f.-b).
2. En el asistente, cambiar la etiqueta `Name` a la de la réplica perdida (por ejemplo, `app-a`) y elegir la subred de su zona.
3. Al terminar el arranque, la instancia ya tiene la aplicación instalada por el *user data*. En `/etc/imgapp.env`, ajustar `INSTANCE_NAME` y recargar con `systemctl reload imgapp`.
4. Actualizar la IP privada de la réplica en ambos nodos HAProxy y recargar HAProxy:

```bash
aws ssm send-command --targets Key=tag:Name,Values=haproxy-a,haproxy-b \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["sed -i \"s/server app-a [0-9.]*:5000/server app-a NUEVA_IP:5000/\" /etc/haproxy/haproxy.cfg","haproxy -c -f /etc/haproxy/haproxy.cfg","systemctl reload haproxy"]'
```

5. Verificar en las estadísticas de HAProxy que la réplica aparezca en UP.

## Recuperación de un nodo HAProxy

**Efecto en el servicio.** Si el nodo se apaga de golpe, puede haber respuestas 502 durante los unos 9 s que tarda el ALB en marcarlo como *unhealthy* (prueba P-09: 5 de 41). Después, el otro nodo atiende todo el tráfico.

### Mantenimiento planificado (sin errores)

1. EC2 → Grupos de destino → `imgproj-tg-haproxy` → Destinos → seleccionar el nodo → **Anular el registro**.
2. Esperar 15 s, que es el tiempo de vaciado configurado, y hacer el mantenimiento.
3. **Registrar destinos** → seleccionar el nodo → puerto 80 → **Incluir como pendiente** → **Registrar destinos pendientes**.

Con este procedimiento la prueba P-09 registró 0 errores en 66 solicitudes.

### Caso 1: el servicio HAProxy falla

1. Validar la configuración: `haproxy -c -f /etc/haproxy/haproxy.cfg`.
2. Reiniciar el servicio: `systemctl restart haproxy`.
3. Revisar los registros si no arranca: `journalctl -u haproxy -n 50 --no-pager`.

### Caso 2: la instancia fue terminada

1. Usar **Lanzar más como esta** sobre el nodo que sigue activo, con la etiqueta `Name` y la subred del nodo perdido. El *user data* instala HAProxy con la configuración y las IP de las réplicas.
2. Corregir el nombre del nodo en la cabecera `X-LB-Node` de `/etc/haproxy/haproxy.cfg` y recargar HAProxy.
3. Registrar la nueva instancia en el grupo de destino `imgproj-tg-haproxy`, puerto 80, y retirar la instancia terminada si aún aparece.

## Recuperación del Application Load Balancer

El ALB es un servicio administrado y distribuido en dos zonas; AWS recupera sus nodos internos sin intervención (AWS, s.f.-d). Si se elimina por error:

1. EC2 → Balanceadores de carga → **Crear balanceador de carga → Application Load Balancer**.
2. Esquema *Expuesto a internet*, la VPC `imgproj-vpc`, las dos subredes públicas y el grupo de seguridad `imgproj-sg-alb`.
3. Agente de escucha HTTP:80 que reenvía al grupo de destino existente `imgproj-tg-haproxy`.
4. Comunicar la nueva URL pública: el nombre DNS del ALB cambia.

## Recuperación de la base de datos

**Efecto en el servicio.** Mientras la base no está disponible, `/health` responde 503 en ambas réplicas y no se pueden registrar ni consultar imágenes. Es el componente con menor redundancia, porque está en una sola zona; la primera recomendación de mejora es activar Multi-AZ.

### Caso 1: la instancia no responde

1. RDS → Bases de datos → `imgproj-db-imgproj` → revisar **Eventos** y **Registros**.
2. **Acciones → Reiniciar**. Al terminar, las réplicas se reconectan solas; no se requiere reiniciar la aplicación.

### Caso 2: pérdida o corrupción de datos

1. RDS → Bases de datos → `imgproj-db-imgproj` → **Acciones → Restaurar a un momento dado**, eligiendo un momento previo al problema dentro de los 7 días de retención (AWS, s.f.-a). Esto crea una instancia nueva con otro endpoint.
2. Al crear la instancia restaurada, asignar el grupo de subredes de datos y el grupo de seguridad `imgproj-sg-rds`.
3. Cambiar `DB_HOST` en `/etc/imgapp.env` de app-a y app-b, y en el bastión, por el nuevo endpoint, y recargar la aplicación con `systemctl reload imgapp`.

### Caso 3: restauración desde el respaldo lógico

El bastión genera cada día un respaldo con `pg_dump` en `s3://<bucket>/backups/`. Para restaurarlo sobre una base vacía:

```bash
aws s3 cp s3://<bucket>/backups/imgdb-AAAAMMDDTHHMMSSZ.sql.gz - | gunzip | imgproj-psql
```

Este comando se ejecuta en el bastión, con Session Manager o Run Command. `imgproj-psql` toma la contraseña de AWS Secrets Manager.

## Recuperación de imágenes en S3

El bucket tiene versionado activo, así que un objeto borrado o sobrescrito conserva sus versiones anteriores (AWS, s.f.-e).

1. S3 → bucket `imgproj-imagesbucket-…` → activar **Mostrar versiones**.
2. Para recuperar un objeto borrado, eliminar su **marcador de eliminación**.
3. Para recuperar una versión anterior, descargarla y volver a subirla.

Si la aplicación no puede leer o escribir en el bucket, revisar que el perfil de instancia conserve los permisos `s3:PutObject` y `s3:GetObject` sobre `uploads/*` (`iam/app-policy.json`).

## Respaldos

**Tabla 3**

*Respaldos disponibles*

| Respaldo | Frecuencia | Retención | Ubicación | Cómo se restaura |
| --- | --- | --- | --- | --- |
| Respaldo automático de RDS | Diario, más registros de transacciones | 7 días | Administrado por RDS | Restaurar a un momento dado |
| Snapshot manual de RDS (`imgproj-antes-demo`) | Bajo demanda, antes de cambios importantes | Hasta que se borre | Administrado por RDS | Restaurar desde snapshot |
| Respaldo lógico (`pg_dump`) | Diario a las 06:00 UTC (temporizador de systemd en el bastión) | Indefinida | `s3://<bucket>/backups/` | `gunzip` + `imgproj-psql` |
| Versiones de objetos de S3 | Con cada cambio | Indefinida | Bucket versionado | Eliminar marcador o restaurar versión |
| Código y configuración | Con cada cambio | Indefinida | Repositorio de GitHub | Volver a desplegar |

*Nota.* Elaboración propia.

Para tomar un snapshot manual antes de un cambio importante:

```bash
aws rds create-db-snapshot --db-instance-identifier imgproj-db-imgproj \
  --db-snapshot-identifier imgproj-antes-de-cambio
```

Para ejecutar el respaldo lógico en ese momento:

```bash
aws ssm send-command --targets Key=tag:Name,Values=bastion \
  --document-name AWS-RunShellScript --parameters 'commands=["imgproj-backup"]'
```

## Actualización de la aplicación sin interrupción

Las actualizaciones se hacen en una réplica a la vez, con recarga suave de Gunicorn. La señal HUP hace que Gunicorn arranque trabajadores con el código nuevo y deje terminar las solicitudes en curso (Gunicorn, s.f.). En la prueba P-14, este procedimiento tuvo 0 errores en 133 solicitudes, frente a 1 error en 196 al reiniciar el servicio.

1. Copiar el nuevo `app.py` en `/opt/imgapp/app.py` de app-a y comprobar su sintaxis con `python -m py_compile`.
2. Recargar: `systemctl reload imgapp`. En instancias creadas antes de agregar `ExecReload`, usar `systemctl kill -s HUP --kill-whom=main imgapp`.
3. Verificar `curl -s localhost:5000/health` y esperar a que HAProxy la vea en UP.
4. Repetir los pasos 1 a 3 en app-b.

## Reconstrucción completa

Si se pierde la mayor parte del entorno, la plantilla `infra/imgproj-stack.yaml` recrea los 39 recursos en unos 20 minutos (AWS, s.f.-f). Antes de borrar un stack dañado, se toma un snapshot de RDS; la política `DeletionPolicy: Snapshot` también genera uno al eliminarlo. La base nueva se crea vacía, así que los datos se recuperan restaurando ese snapshot o el respaldo lógico, siguiendo los pasos de la sección de la base de datos.

## Referencias

Amazon Web Services. (s.f.-a). *Introduction to backups*. Amazon RDS User Guide. https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html

Amazon Web Services. (s.f.-b). *Launch an EC2 instance using details from an existing instance*. Amazon EC2 User Guide. https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/launch-more-like-this.html

Amazon Web Services. (s.f.-c). *Reliability pillar: AWS Well-Architected Framework*. https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/welcome.html

Amazon Web Services. (s.f.-d). *What is an Application Load Balancer?* https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html

Amazon Web Services. (s.f.-e). *Retaining multiple versions of objects with S3 Versioning*. Amazon S3 User Guide. https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html

Amazon Web Services. (s.f.-f). *What is CloudFormation?* https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html

Gunicorn. (s.f.). *Signals*. https://gunicorn.org/signals/
