#!/bin/bash
# Comandos de la demostración del proyecto final (se ejecuta en AWS CloudShell, us-east-1).
#
#   ./demo.sh estado                 Revisa que todo esté sano antes de presentar
#   ./demo.sh continuidad [segundos] Consulta el sitio cada segundo (por defecto 90 s)
#   ./demo.sh apagar [app-a|app-b]   Detiene la aplicación en una réplica
#   ./demo.sh encender [app-a|app-b] Vuelve a iniciar la aplicación en una réplica
#   ./demo.sh borrar-todo            Después de presentar: borra el stack y sus costos
set -uo pipefail
export AWS_DEFAULT_REGION=us-east-1
STACK=imgproj
A=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text 2>/dev/null)
[ -z "$A" ] || [ "$A" = "None" ] && A=http://imgproj-alb-399228811.us-east-1.elb.amazonaws.com

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
titulo() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

ssm() { # ssm <Name> <comando>  -> ejecuta y muestra la salida
  local id
  id=$(aws ssm send-command --targets "Key=tag:Name,Values=$1" --document-name AWS-RunShellScript \
       --parameters "commands=[\"$2\"]" --query Command.CommandId --output text) || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 2
    st=$(aws ssm list-command-invocations --command-id "$id" --query 'CommandInvocations[0].Status' --output text 2>/dev/null)
    [ "$st" = "Success" ] || [ "$st" = "Failed" ] && break
  done
  aws ssm list-command-invocations --command-id "$id" --details \
      --query 'CommandInvocations[].CommandPlugins[].Output' --output text
}

replica() {
  case "${1:-app-a}" in app-a|app-b) echo "${1:-app-a}" ;; *) rojo "Réplica no válida: usa app-a o app-b"; exit 1 ;; esac
}

case "${1:-ayuda}" in
  estado)
    titulo "Sitio: $A"
    out=$(curl -s --max-time 5 "$A/health"); [[ "$out" == *'"ok"'* ]] && verde "OK  $out" || rojo "FALLA  $out"
    titulo "Balanceador (target group)"
    TG=$(aws elbv2 describe-target-groups --names imgproj-tg-haproxy --query 'TargetGroups[0].TargetGroupArn' --output text)
    aws elbv2 describe-target-health --target-group-arn "$TG" \
      --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text
    titulo "Réplicas vistas desde haproxy-a"
    ssm haproxy-a 'curl -s http://localhost:8404/stats\\;csv | cut -d, -f2,18 | grep -E ^app-'
    titulo "Imágenes en el historial"
    n=$(curl -s --max-time 5 "$A/history" | grep -c 'class="tile"'); echo "$n imagen(es)"
    [ "$n" -gt 0 ] || rojo "El historial está vacío: sube una imagen antes de presentar."
    ;;
  continuidad)
    s=${2:-90}
    titulo "Consultando $A cada segundo durante $s s (Ctrl+C para detener)"
    for _ in $(seq 1 "$s"); do
      c=$(curl -s -o /dev/null -D /tmp/h -w '%{http_code}' --max-time 3 "$A/health")
      lb=$(grep -i '^x-lb-node' /tmp/h | cut -d' ' -f2 | tr -d '\r')
      app=$(grep -i '^x-served-by' /tmp/h | cut -d' ' -f2 | tr -d '\r')
      linea="$(date +%T)  HTTP $c  balanceador=${lb:--}  replica=${app:--}"
      [ "$c" = "200" ] && verde "$linea" || rojo "$linea"
      sleep 1
    done
    ;;
  apagar)
    r=$(replica "${2:-app-a}")
    titulo "Deteniendo la aplicación en $r"
    ssm "$r" "systemctl stop imgapp && echo 'imgapp detenido en $r'"
    echo "HAProxy la marcará DOWN en unos 15 s; el sitio sigue respondiendo con la otra réplica."
    ;;
  encender)
    r=$(replica "${2:-app-a}")
    titulo "Iniciando la aplicación en $r"
    ssm "$r" "systemctl start imgapp && sleep 3 && curl -s localhost:5000/health"
    echo "HAProxy la reincorpora en unos 10 s."
    ;;
  borrar-todo)
    rojo "Esto ELIMINA el sistema completo (servidores, balanceador, base de datos, imágenes y respaldos)."
    read -r -p "Escribe BORRAR para continuar: " ok
    [ "$ok" = "BORRAR" ] || { echo "Cancelado."; exit 0; }
    B=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='ImagesBucketName'].OutputValue" --output text)
    titulo "Vaciando el bucket $B (incluidas sus versiones)"
    python3 -c "import boto3,sys; boto3.resource('s3').Bucket(sys.argv[1]).object_versions.delete(); print('bucket vacío')" "$B" \
      || { rojo "No se pudo vaciar el bucket. Vacíalo en la consola de S3 (botón Vaciar) y vuelve a ejecutar este comando."; exit 1; }
    titulo "Eliminando el stack $STACK (10 a 15 minutos)"
    aws cloudformation delete-stack --stack-name "$STACK"
    aws cloudformation wait stack-delete-complete --stack-name "$STACK" && verde "Stack eliminado."
    titulo "Eliminando snapshots manuales de RDS"
    for s in $(aws rds describe-db-snapshots --snapshot-type manual --query "DBSnapshots[?starts_with(DBSnapshotIdentifier,'imgproj')].DBSnapshotIdentifier" --output text); do
      aws rds delete-db-snapshot --db-snapshot-identifier "$s" >/dev/null && echo "borrado $s"
    done
    verde "Listo. El código, el informe y la plantilla siguen en GitHub."
    ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
