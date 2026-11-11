# Guía de despliegue en AWS

Toda la infraestructura se crea con **una sola plantilla de CloudFormation**: [`infra/imgproj-stack.yaml`](infra/imgproj-stack.yaml). Esto cuenta como Infraestructura como Código, uno de los puntos extra del proyecto.

La plantilla crea lo siguiente:

| Recurso | Detalle |
| --- | --- |
| Red | VPC 10.0.0.0/16, 2 subredes públicas, 2 privadas de aplicación, 2 privadas de datos, NAT Gateway y endpoint de S3 |
| 5 servidores EC2 (Amazon Linux 2023) | `haproxy-a`, `haproxy-b`, `app-a`, `app-b` y `bastion` |
| Application Load Balancer | Público en 2 zonas, target group `imgproj-tg-haproxy` |
| Amazon S3 | Bucket privado, cifrado y con versionado |
| Amazon RDS | PostgreSQL privado, respaldos de 7 días |
| AWS Secrets Manager | Contraseña de la base generada automáticamente |
| Grupos de seguridad | Encadenados: ALB → HAProxy → App → RDS |

Las instancias se configuran solas al arrancar: instalan HAProxy o la aplicación, escriben la configuración y levantan los servicios.

---

## 1. Antes de empezar

- **Región:** `us-east-1` (N. Virginia).
- **Par de llaves (opcional):** no hace falta. Sin llave, los servidores se administran con AWS Systems Manager (sección 3). Si quieres SSH, en AWS Academy Learner Lab usa `vockey`.
- **Tu IP pública:** búscala en https://checkip.amazonaws.com. La usarás con `/32` al final, por ejemplo `190.56.10.20/32`.
- **Costo y tiempo:** la creación tarda entre **15 y 20 minutos**, porque RDS es lo más lento. El NAT Gateway y el ALB cobran por hora, así que **borra el stack después de la presentación** (sección 6).

## 2. Crear el stack

1. Abre **CloudFormation → Create stack → With new resources (standard)**.
2. Elige **Upload a template file** y sube `infra/imgproj-stack.yaml`.
3. Escribe el nombre del stack: `imgproj`.
4. Llena los parámetros:

   | Parámetro | Learner Lab | Cuenta normal |
   | --- | --- | --- |
   | KeyName | `vockey` o vacío | **Vacío** (se administra con Systems Manager) |
   | AllowedSSHCidr | `TU.IP/32` | `TU.IP/32` |
   | InstanceProfileName | `LabInstanceProfile` (por defecto) | **Dejar vacío**: se crea un rol de privilegio mínimo |
   | InstanceType | `t3.micro` | `t3.micro` |
   | DBInstanceClass | `db.t3.micro` | `db.t3.micro` |

5. En una cuenta normal, marca la casilla **"I acknowledge that AWS CloudFormation might create IAM resources"**.
6. Pulsa **Submit** y espera a que el estado sea `CREATE_COMPLETE`.
7. Abre la pestaña **Outputs**. Ahí están la URL del sistema (`AppUrl`), la IP del bastión y los demás datos.

Después de `CREATE_COMPLETE`, espera **2 o 3 minutos** más para que las instancias terminen de instalar paquetes. Luego abre `AppUrl` en el navegador.

## 3. Administrar los servidores

### Sin llaves: Systems Manager (lo que se usó en el despliegue real)

- **Terminal interactiva:** EC2 → selecciona la instancia → **Connect → Session Manager → Connect**.
- **Un comando en un servidor**, desde **CloudShell** (ícono `>_` arriba en la consola):

```bash
aws ssm send-command --targets Key=tag:Name,Values=app-a --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl stop imgapp"]' --query Command.CommandId --output text
```

- **Ver la salida de un comando:**

```bash
aws ssm list-command-invocations --command-id ID_DEL_COMANDO --details \
  --query 'CommandInvocations[].CommandPlugins[].Output' --output text
```

- **Estado de los backends en HAProxy:**

```bash
aws ssm send-command --targets Key=tag:Name,Values=haproxy-a --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s \"http://localhost:8404/stats;csv\" | cut -d, -f1,2,18 | grep be_app"]' \
  --query Command.CommandId --output text
```

- **Estado de los destinos del ALB:**

```bash
TG=$(aws elbv2 describe-target-groups --names imgproj-tg-haproxy --query 'TargetGroups[0].TargetGroupArn' --output text)
aws elbv2 describe-target-health --target-group-arn $TG --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text
```

### Con llave: SSH (solo si creaste el stack con `KeyName`)

Usa Git Bash o PowerShell en la carpeta donde está la llave:

```bash
chmod 400 labsuser.pem
ssh -i labsuser.pem ec2-user@IP_DEL_BASTION
```

Las instancias privadas no tienen IP pública. Para entrar a ellas, pasa por el bastión. La llave nunca se copia al bastión:

```bash
ssh -i labsuser.pem -o ProxyCommand="ssh -i labsuser.pem -W %h:%p ec2-user@IP_DEL_BASTION" ec2-user@IP_PRIVADA_DE_APP_A
```

Las IP privadas aparecen en el output `PrivateIps`. También están en el bastión, en `/etc/imgapp.env`.

El bastión trae tres comandos preparados:

| Comando | Para qué sirve |
| --- | --- |
| `imgproj-psql` | Abre `psql` contra RDS; la contraseña se lee de Secrets Manager |
| `imgproj-backup` | Hace un `pg_dump` y lo sube a `s3://BUCKET/backups/` (además corre cada día a las 06:00 UTC) |
| `imgproj-continuidad http://DNS-DEL-ALB` | Prueba de continuidad: muestra cada segundo el código HTTP, el nodo HAProxy y la réplica que respondió |

## 4. Pruebas para la demostración (Tabla 6 del informe)

| ID | Qué hacer | Qué mostrar |
| --- | --- | --- |
| P-01 | Subir un JPG en la web | Página de resultado con etiquetas y confianza |
| P-02 | **S3 → bucket → `uploads/`** | El objeto `uuid.jpg` |
| P-03 | En el bastión: `imgproj-psql -c "SELECT i.original_name, l.name, l.confidence, i.processed_at FROM images i JOIN labels l ON l.image_id = i.id ORDER BY i.processed_at DESC LIMIT 10;"` | Los registros |
| P-04 | Abrir **Historial** | La imagen en primer lugar |
| P-05 | Renombrar un PDF a `.jpg` y subirlo | Mensaje "El contenido del archivo no corresponde..." |
| P-06 | Subir una imagen de más de 5 MB | Mensaje "La imagen supera el tamaño máximo de 5 MB" |
| P-07 | En el bastión: `source /etc/imgapp.env && imgproj-continuidad $ALB_URL` | `replica=app-a` y `replica=app-b` alternándose |
| P-08 | Con P-07 corriendo, en app-a: `sudo systemctl stop imgapp` | Todo sigue en `HTTP 200`, ahora solo con `replica=app-b`. Las estadísticas de HAProxy muestran app-a en DOWN |
| P-09 | Con P-07 corriendo, en haproxy-a: `sudo systemctl stop haproxy` | **EC2 → Target groups → imgproj-tg-haproxy** marca haproxy-a como *unhealthy* en unos 10 s; el sitio sigue respondiendo por haproxy-b |
| P-10 | `sudo systemctl start imgapp` en app-a y `sudo systemctl start haproxy` en haproxy-a | Ambos vuelven a *healthy* sin hacer nada más |
| P-11 | Desde tu PC: `psql -h ENDPOINT_RDS` o `nc -zv ENDPOINT_RDS 5432` | La conexión falla: RDS es privado |
| P-12 | `git grep -n -E "BEGIN .*PRIVATE KEY\|aws_secret_access_key\|AKIA[0-9A-Z]{16}"` en el repositorio | Sin resultados: no hay llaves ni credenciales escritas |

**Ver las estadísticas de HAProxy (figuras 9 y 10).** Abre un túnel desde tu PC:

```bash
ssh -i labsuser.pem -L 8404:IP_PRIVADA_HAPROXY_A:8404 ec2-user@IP_DEL_BASTION
```

Luego abre http://localhost:8404/stats en tu navegador.

**Nota honesta sobre P-09.**
- Cuando se detiene una réplica de la aplicación (P-08), HAProxy reintenta en la otra réplica y no se pierde ninguna solicitud.
- Cuando se detiene un HAProxy de golpe (P-09), el ALB puede devolver algún `502` durante los 5 a 10 segundos que tarda en marcarlo *unhealthy*.
- Para un retiro sin ningún error, primero haz **Deregister** de haproxy-a en el target group, espera 15 segundos y después detén el servicio. Este es el procedimiento de mantenimiento correcto, y conviene explicarlo así en la presentación.

## 5. Recuperación y respaldos (guía de recuperación)

| Falla | Cómo recuperarse |
| --- | --- |
| Réplica de la aplicación | `sudo systemctl restart imgapp`; ver logs con `journalctl -u imgapp -n 50`. Si la instancia se terminó: **Lanzar más como esta** desde la otra réplica y actualizar su IP en `haproxy.cfg` (ver `docs/Guia_Recuperacion.pdf`) |
| Nodo HAProxy | `sudo haproxy -c -f /etc/haproxy/haproxy.cfg` y `sudo systemctl restart haproxy`; ver logs con `journalctl -u haproxy` |
| ALB | Servicio administrado multizona. Si se borra por error, crear otro ALB con el grupo de destino `imgproj-tg-haproxy` (la URL cambia) |
| RDS | **RDS → Databases → imgproj-db-… → Actions → Restore to point in time**; luego cambiar `DB_HOST` en `/etc/imgapp.env` de app-a y app-b y reiniciar `imgapp` |
| Datos lógicos | `imgproj-backup` genera un respaldo; para restaurarlo: `aws s3 cp s3://BUCKET/backups/ARCHIVO.sql.gz - \| gunzip \| imgproj-psql` |
| Imagen borrada en S3 | El bucket tiene versionado: **S3 → objeto → Versions** para recuperar la versión anterior |

Antes de la presentación, toma un snapshot manual: **RDS → Actions → Take snapshot**.

## 6. Borrar todo al terminar

1. Vacía el bucket: **S3 → bucket → Empty**. El bucket tiene versionado, así que vacíalo desde la consola, que borra también las versiones.
2. Ve a **CloudFormation → imgproj → Delete**.
3. RDS deja un snapshot final, por la política `DeletionPolicy: Snapshot`. Bórralo en **RDS → Snapshots** si ya no lo necesitas.

## 7. Si algo falla

| Síntoma | Revisión |
| --- | --- |
| El stack falla en `AppRole` o `AppInstanceProfile` | Estás en Learner Lab con `InstanceProfileName` vacío; usa `LabInstanceProfile` |
| El stack falla en `Database` | La clase de instancia no está permitida en tu cuenta; prueba con `db.t3.micro` o `db.t4g.micro` |
| La web devuelve 503 | Las réplicas no están sanas. En app-a revisa `journalctl -u imgapp -n 80` y `cat /var/log/cloud-init-output.log` |
| Error "No se pudo analizar la imagen" | El rol no tiene permiso de Rekognition, o la región no tiene Rekognition. Revisa `journalctl -u imgapp` |
| ALB con destinos *unhealthy* | En haproxy-a: `systemctl status haproxy` y `curl -i localhost/lb-health` |

## Archivos del proyecto

| Archivo | Contenido |
| --- | --- |
| `app/app.py`, `app/requirements.txt` | Aplicación Flask |
| `haproxy/haproxy.cfg` | Configuración de HAProxy |
| `db/schema.sql` | Esquema de la base de datos |
| `systemd/imgapp.service` | Servicio de la aplicación |
| `config/imgapp.env.example` | Variables de entorno sin secretos |
| `iam/app-policy.json` | Política de privilegio mínimo |
| `scripts/backup.sh`, `scripts/continuidad.sh` | Respaldo y prueba de continuidad |
| `infra/template.src.yaml`, `infra/build_template.py` | Fuente de la plantilla y su generador |
| `infra/imgproj-stack.yaml` | **Plantilla final que se sube a CloudFormation** |
| `tests/test_app.py` | Pruebas automáticas de la aplicación (`pytest`) |

Si cambias `app.py` o algún archivo de configuración, regenera la plantilla con este comando:

```bash
python infra/build_template.py
```
