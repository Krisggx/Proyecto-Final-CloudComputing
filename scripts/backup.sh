#!/bin/bash
# Respaldo logico de la base de datos hacia S3 (se ejecuta en el bastion).
# Lee la configuracion de /etc/imgapp.env y la contrasena de Secrets Manager.
set -euo pipefail
source /etc/imgapp.env
SECRET=$(aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$DB_SECRET_ARN" --query SecretString --output text)
export PGUSER=$(echo "$SECRET" | jq -r .username)
export PGPASSWORD=$(echo "$SECRET" | jq -r .password)
export PGSSLMODE=require
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump -h "$DB_HOST" -d "$DB_NAME" | gzip | aws s3 cp - "s3://$BUCKET/backups/imgdb-$STAMP.sql.gz"
echo "Respaldo guardado en s3://$BUCKET/backups/imgdb-$STAMP.sql.gz"
