#!/bin/bash
# Prueba de continuidad: consulta el ALB cada segundo y muestra
# el codigo HTTP, el nodo HAProxy y la replica que respondio.
# Uso: ./continuidad.sh http://DNS-DEL-ALB
URL="$1"
while true; do
  OUT=$(curl -s -o /dev/null -D - -w "%{http_code}" --max-time 3 "$URL/health")
  CODE=$(echo "$OUT" | tail -n1)
  LB=$(echo "$OUT" | grep -i '^x-lb-node' | cut -d' ' -f2 | tr -d '\r')
  APP=$(echo "$OUT" | grep -i '^x-served-by' | cut -d' ' -f2 | tr -d '\r')
  echo "$(date +%H:%M:%S)  HTTP $CODE  balanceador=$LB  replica=$APP"
  sleep 1
done
