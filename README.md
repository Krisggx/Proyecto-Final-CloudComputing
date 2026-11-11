# Sistema web para análisis inteligente de imágenes en AWS

Proyecto final del curso **Cloud Computing**. Universidad del Valle de Guatemala, Campus Altiplano.
Docente: M.A. Ing. Dennys Tezén.

Es un sistema web que hace lo siguiente:

1. Recibe imágenes JPG o PNG.
2. Las guarda en **Amazon S3**.
3. Las analiza con **Amazon Rekognition**.
4. Registra las etiquetas y su confianza en **Amazon RDS (PostgreSQL)**.

El sistema sigue disponible aunque falle uno de sus servidores. Toda la infraestructura se crea con una sola plantilla de **AWS CloudFormation**.

## Arquitectura

```
Usuario ─► Application Load Balancer (2 zonas)
              ├─► HAProxy A (us-east-1a) ─┐
              └─► HAProxy B (us-east-1b) ─┼─► App A (Flask + Gunicorn)
                                          └─► App B (Flask + Gunicorn)
                                                 ├─► Amazon S3
                                                 ├─► Amazon Rekognition
                                                 ├─► Amazon RDS PostgreSQL (privado)
                                                 └─► AWS Secrets Manager
Bastión: administración, respaldos y pruebas
```

## Equipo

| Integrante | Carné | Componente |
| --- | --- | --- |
| Cristina Marie Jimenez Garcia | 20680 | Red, grupos de seguridad y ALB |
| Isidro Alexander Chuj Quijivix | 231621 | HAProxy y comprobaciones de estado |
| Jorge José Adrián Monterroso Flores | 18610 | Aplicación, S3 y Rekognition |
| Edwin Manuel Valenzuela Chaj | 231715 | RDS, respaldos y recuperación |
| Kris Gregorio Garcia Xep | 23259 | Bastión, CloudFormation y documentación |

## Contenido del repositorio

| Carpeta o archivo | Contenido |
| --- | --- |
| [`docs/`](docs/) | Informe académico (APA 7) y presentación en PDF |
| [`DESPLIEGUE.md`](DESPLIEGUE.md) | Guía de despliegue, pruebas y recuperación |
| [`infra/imgproj-stack.yaml`](infra/imgproj-stack.yaml) | Plantilla de CloudFormation que se sube a AWS |
| [`infra/template.src.yaml`](infra/template.src.yaml), [`infra/build_template.py`](infra/build_template.py) | Fuente de la plantilla y su generador |
| [`app/`](app/) | Aplicación Flask |
| [`haproxy/haproxy.cfg`](haproxy/haproxy.cfg) | Configuración de HAProxy |
| [`db/schema.sql`](db/schema.sql) | Esquema de la base de datos |
| [`systemd/`](systemd/), [`config/`](config/), [`iam/`](iam/) | Servicio, variables de ejemplo y política de privilegio mínimo |
| [`scripts/`](scripts/) | Respaldo con `pg_dump` y prueba de continuidad |
| [`tests/`](tests/) | Pruebas automáticas de la aplicación (`pytest`) |
| [`evidencias/`](evidencias/) | Capturas del despliegue real y registro de la prueba de continuidad |

## Despliegue rápido

1. Abre **CloudFormation → Create stack** y sube `infra/imgproj-stack.yaml`.
2. Llena los parámetros:
   - `AllowedSSHCidr`: tu IP pública con `/32`.
   - `InstanceProfileName`: vacío en una cuenta normal, o `LabInstanceProfile` en AWS Academy.
3. Cuando el stack llegue a `CREATE_COMPLETE`, abre la URL que aparece en **Outputs → AppUrl**.

El detalle completo está en [`DESPLIEGUE.md`](DESPLIEGUE.md).

## Resultados

Las 12 pruebas del informe se hicieron sobre el despliegue real y todas cumplieron:

- Detener una réplica de la aplicación produjo 0 errores.
- Retirar un nodo HAProxy de forma ordenada produjo 0 errores.
- Detenerlo de golpe produjo 5 respuestas 502 durante los 9 segundos que el ALB tardó en detectarlo.

## Seguridad

- **Sin secretos en el repositorio:** la contraseña de la base la genera AWS Secrets Manager y la aplicación usa el rol de IAM de la instancia.
- **Base de datos privada:** RDS no tiene IP pública.
