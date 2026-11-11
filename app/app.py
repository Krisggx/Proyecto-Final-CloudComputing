"""Sistema web para analisis inteligente de imagenes.

Flujo: el usuario sube una imagen -> se valida -> se guarda en Amazon S3 ->
Amazon Rekognition (DetectLabels) la analiza -> el resultado se registra en
Amazon RDS (PostgreSQL) -> se muestra la imagen con sus etiquetas.

Configuracion por variables de entorno (ver config/imgapp.env.example).
No contiene credenciales: AWS se autentica con el rol de la instancia y la
contrasena de la base se lee de AWS Secrets Manager.
"""
import json
import logging
import os
import socket
import uuid

import boto3
import psycopg2
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, abort, redirect, render_template_string, request, url_for
from werkzeug.utils import secure_filename

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/Guatemala"))
except Exception:  # sin base de zonas horarias: se muestra en UTC
    LOCAL_TZ = None

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("BUCKET", "")
DB_HOST = os.environ.get("DB_HOST", "")
DB_NAME = os.environ.get("DB_NAME", "imgdb")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", socket.gethostname())
MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "70"))
MAX_LABELS = int(os.environ.get("MAX_LABELS", "10"))
MAX_MB = 5

# extension -> (familia, Content-Type)
ALLOWED = {
    "jpg": ("jpeg", "image/jpeg"),
    "jpeg": ("jpeg", "image/jpeg"),
    "png": ("png", "image/png"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("imgapp")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

_clients = {}
_db_credentials = None
_schema_ready = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id            UUID PRIMARY KEY,
    s3_key        TEXT NOT NULL,
    original_name TEXT NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS labels (
    id         SERIAL PRIMARY KEY,
    image_id   UUID NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    confidence NUMERIC(5,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_processed_at ON images (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_labels_image_id ON labels (image_id);
"""


class UserError(Exception):
    """Error que se muestra al usuario tal cual."""


# ---------------------------------------------------------------- AWS y BD

def aws(service):
    if service not in _clients:
        _clients[service] = boto3.client(service, region_name=REGION)
    return _clients[service]


def db_credentials():
    global _db_credentials
    if _db_credentials is None:
        secret = aws("secretsmanager").get_secret_value(SecretId=DB_SECRET_ARN)
        data = json.loads(secret["SecretString"])
        _db_credentials = (data["username"], data["password"])
    return _db_credentials


def db_connect():
    user, password = db_credentials()
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=user, password=password,
        connect_timeout=3, sslmode="require",
    )


def ensure_schema(conn):
    """Crea las tablas si no existen. El candado evita carreras entre replicas."""
    global _schema_ready
    if _schema_ready:
        return
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(424242)")
        cur.execute(SCHEMA_SQL)
    _schema_ready = True


def query(sql, params=None, fetch="all"):
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "all":
                return cur.fetchall()
            if fetch == "one":
                return cur.fetchone()
            return None
    finally:
        conn.close()


# ------------------------------------------------------- logica del sistema

def sniff_family(head):
    """Tipo real del archivo por su firma (primeros bytes), no por el nombre."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def validate_upload(file_storage):
    """Devuelve (bytes, extension, content_type, nombre) o lanza UserError."""
    if file_storage is None or not file_storage.filename:
        raise UserError("Selecciona una imagen antes de enviar.")
    name = secure_filename(file_storage.filename) or "imagen"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED:
        raise UserError("Formato no permitido. Solo se aceptan imagenes JPG o PNG.")
    data = file_storage.read()
    if not data:
        raise UserError("El archivo esta vacio.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise UserError("La imagen supera el tamano maximo de %d MB." % MAX_MB)
    family, content_type = ALLOWED[ext]
    if sniff_family(data[:16]) != family:
        raise UserError("El contenido del archivo no corresponde a una imagen %s valida." % ext.upper())
    return data, ext, content_type, name[:200]


def store_image(data, key, content_type):
    aws("s3").put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)


def analyze_image(key):
    resp = aws("rekognition").detect_labels(
        Image={"S3Object": {"Bucket": BUCKET, "Name": key}},
        MaxLabels=MAX_LABELS,
        MinConfidence=MIN_CONFIDENCE,
    )
    return [(l["Name"], round(float(l["Confidence"]), 2)) for l in resp.get("Labels", [])]


def save_result(image_id, key, original_name, labels):
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO images (id, s3_key, original_name) VALUES (%s, %s, %s)",
                (image_id, key, original_name),
            )
            for name, confidence in labels:
                cur.execute(
                    "INSERT INTO labels (image_id, name, confidence) VALUES (%s, %s, %s)",
                    (image_id, name, confidence),
                )
    finally:
        conn.close()


def image_url(key):
    return aws("s3").generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=900,
    )


def local_time(dt):
    if dt is None:
        return ""
    if LOCAL_TZ is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


# ------------------------------------------------------------------ vistas

LAYOUT = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} - Analisis de imagenes</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1d2433}
 header{background:#232f3e;color:#fff;padding:14px 20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
 header a{color:#fff;text-decoration:none;font-weight:600} header .brand{font-size:1.1rem;margin-right:auto}
 main{max-width:960px;margin:24px auto;padding:0 16px}
 .card{background:#fff;border:1px solid #e1e4e8;border-radius:10px;padding:20px;margin-bottom:20px}
 .error{background:#fdecea;border:1px solid #f5c2c0;color:#8a1c14;padding:12px;border-radius:8px;margin-bottom:16px}
 .ok{background:#e6f4ea;border:1px solid #b7dfc2;color:#1e5b2e;padding:12px;border-radius:8px;margin-bottom:16px}
 button{background:#ff9900;border:0;color:#111;font-weight:700;padding:10px 18px;border-radius:8px;cursor:pointer}
 table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:8px;border-bottom:1px solid #eee}
 .bar{background:#eef1f5;border-radius:6px;height:10px;width:100%} .bar span{display:block;height:10px;border-radius:6px;background:#2f7de1}
 img.preview{max-width:100%;max-height:420px;border-radius:8px} img.thumb{width:72px;height:72px;object-fit:cover;border-radius:6px}
 footer{text-align:center;color:#6a7385;font-size:.85rem;padding:20px}
 .muted{color:#6a7385}
</style></head><body>
<header><span class="brand">Analisis inteligente de imagenes</span>
<a href="{{ url_for('index') }}">Subir imagen</a><a href="{{ url_for('history') }}">Historial</a></header>
<main>{{ body|safe }}</main>
<footer>Atendido por la replica <strong>{{ instance }}</strong> &middot; Amazon S3 + Amazon Rekognition + Amazon RDS</footer>
</body></html>"""

UPLOAD = """
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<div class="card">
 <h2>Subir una imagen</h2>
 <p class="muted">Formatos JPG o PNG, maximo {{ max_mb }} MB. Se analizara con Amazon Rekognition.</p>
 <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
  <p><input type="file" name="image" accept=".jpg,.jpeg,.png,image/jpeg,image/png" required></p>
  <button type="submit">Analizar imagen</button>
 </form>
</div>"""

RESULT = """
{% if new %}<div class="ok">Imagen procesada correctamente.</div>{% endif %}
<div class="card">
 <h2>{{ image.original_name }}</h2>
 <p class="muted">Procesada el {{ image.processed_at }} &middot; ID {{ image.id }}</p>
 <img class="preview" src="{{ image.url }}" alt="{{ image.original_name }}">
</div>
<div class="card">
 <h3>Etiquetas detectadas</h3>
 {% if labels %}
 <table><tr><th>Etiqueta</th><th>Confianza</th><th style="width:40%"></th></tr>
 {% for name, conf in labels %}
  <tr><td>{{ name }}</td><td>{{ '%.2f'|format(conf) }} %</td>
  <td><div class="bar"><span style="width:{{ conf }}%"></span></div></td></tr>
 {% endfor %}</table>
 {% else %}<p>Rekognition no detecto etiquetas con confianza mayor o igual a {{ min_conf }} %.</p>{% endif %}
</div>"""

HISTORY = """
<div class="card">
 <h2>Historial de imagenes procesadas</h2>
 {% if rows %}
 <table><tr><th></th><th>Imagen</th><th>Etiquetas principales</th><th>Fecha</th></tr>
 {% for r in rows %}
  <tr><td><a href="{{ url_for('show', image_id=r.id) }}"><img class="thumb" src="{{ r.url }}" alt=""></a></td>
  <td><a href="{{ url_for('show', image_id=r.id) }}">{{ r.original_name }}</a></td>
  <td>{{ r.top or 'Sin etiquetas' }}</td><td>{{ r.processed_at }}</td></tr>
 {% endfor %}</table>
 {% else %}<p>Todavia no hay imagenes procesadas.</p>{% endif %}
</div>"""


def page(title, body_template, status=200, **ctx):
    body = render_template_string(body_template, **ctx)
    html = render_template_string(LAYOUT, title=title, body=body, instance=INSTANCE_NAME)
    return html, status


@app.after_request
def served_by(resp):
    resp.headers["X-Served-By"] = INSTANCE_NAME
    return resp


@app.errorhandler(413)
def too_large(_err):
    return page("Subir imagen", UPLOAD, status=413, max_mb=MAX_MB,
                error="La imagen supera el tamano maximo de %d MB." % MAX_MB)


@app.get("/")
def index():
    return page("Subir imagen", UPLOAD, max_mb=MAX_MB, error=None)


@app.post("/upload")
def upload():
    try:
        data, ext, content_type, original_name = validate_upload(request.files.get("image"))
    except UserError as exc:
        return page("Subir imagen", UPLOAD, status=400, max_mb=MAX_MB, error=str(exc))

    image_id = str(uuid.uuid4())
    key = "uploads/%s.%s" % (image_id, ext)
    try:
        store_image(data, key, content_type)
        labels = analyze_image(key)
        save_result(image_id, key, original_name, labels)
    except (ClientError, BotoCoreError) as exc:
        log.exception("Error de AWS al procesar %s", key)
        msg = "No se pudo analizar la imagen en AWS. Intenta de nuevo."
        if isinstance(exc, ClientError) and "InvalidImageFormat" in exc.response.get("Error", {}).get("Code", ""):
            msg = "Rekognition no pudo leer la imagen. Verifica que no este danada."
        return page("Subir imagen", UPLOAD, status=502, max_mb=MAX_MB, error=msg)
    except psycopg2.Error:
        log.exception("Error de base de datos al registrar %s", key)
        return page("Subir imagen", UPLOAD, status=503, max_mb=MAX_MB,
                    error="La imagen se analizo, pero no se pudo guardar el resultado en la base de datos.")
    log.info("Imagen %s procesada con %d etiquetas", image_id, len(labels))
    return redirect(url_for("show", image_id=image_id, new=1))


@app.get("/images/<image_id>")
def show(image_id):
    try:
        uuid.UUID(image_id)
    except ValueError:
        abort(404)
    row = query("SELECT id, s3_key, original_name, processed_at FROM images WHERE id = %s",
                (image_id,), fetch="one")
    if row is None:
        abort(404)
    labels = query("SELECT name, confidence FROM labels WHERE image_id = %s ORDER BY confidence DESC",
                   (image_id,))
    image = {"id": row[0], "url": image_url(row[1]), "original_name": row[2],
             "processed_at": local_time(row[3])}
    return page("Resultado", RESULT, image=image, labels=[(n, float(c)) for n, c in labels],
                new=request.args.get("new") == "1", min_conf=MIN_CONFIDENCE)


@app.get("/history")
def history():
    rows = query("""
        SELECT i.id, i.s3_key, i.original_name, i.processed_at,
               (SELECT string_agg(name || ' (' || round(confidence)::int || '%)', ', ' ORDER BY confidence DESC)
                  FROM (SELECT name, confidence FROM labels l
                         WHERE l.image_id = i.id ORDER BY confidence DESC LIMIT 3) t) AS top
          FROM images i
         ORDER BY i.processed_at DESC
         LIMIT 100""")
    items = [{"id": r[0], "url": image_url(r[1]), "original_name": r[2],
              "processed_at": local_time(r[3]), "top": r[4]} for r in rows]
    return page("Historial", HISTORY, rows=items)


@app.get("/health")
def health():
    try:
        query("SELECT 1", fetch="one")
        return {"status": "ok", "instance": INSTANCE_NAME}, 200
    except Exception as exc:  # cualquier falla de BD o de credenciales
        log.warning("Health check fallido: %s", exc)
        return {"status": "error", "instance": INSTANCE_NAME}, 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
