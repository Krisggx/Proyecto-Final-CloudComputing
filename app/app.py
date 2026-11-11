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
        raise UserError("El archivo está vacío.")
    if len(data) > MAX_MB * 1024 * 1024:
        raise UserError("La imagen supera el tamaño máximo de %d MB." % MAX_MB)
    family, content_type = ALLOWED[ext]
    if sniff_family(data[:16]) != family:
        raise UserError("El contenido del archivo no corresponde a una imagen %s válida." % ext.upper())
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
<title>{{ title }} · Visión</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
 :root{--bg:#fafaf9;--surface:#ffffff;--line:#e7e5e4;--ink:#1c1917;--muted:#78716c;--soft:#f5f5f4;
  --accent:#4f46e5;--accent-hover:#4338ca;--accent-soft:#eef2ff;--ok:#15803d;--ok-soft:#f0fdf4;--bad:#b91c1c;--bad-soft:#fef2f2;
  --radius:16px;--shadow:0 1px 2px rgba(28,25,23,.04),0 8px 24px rgba(28,25,23,.06)}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.5}
 a{color:inherit}
 .top{position:sticky;top:0;z-index:5;background:rgba(250,250,249,.85);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
 .top-in{max-width:1040px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:16px}
 .brand{display:flex;align-items:center;gap:10px;text-decoration:none;font-weight:700;letter-spacing:-.01em;margin-right:auto}
 .logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#6366f1,#a855f7);display:grid;place-items:center}
 .logo svg{width:16px;height:16px;stroke:#fff}
 .tabs{display:flex;gap:4px;background:var(--soft);padding:4px;border-radius:999px}
 .tabs a{text-decoration:none;font-size:.9rem;font-weight:500;color:var(--muted);padding:6px 14px;border-radius:999px;transition:.15s}
 .tabs a:hover{color:var(--ink)}
 .tabs a.on{background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.08)}
 main{max-width:1040px;margin:0 auto;padding:40px 24px 24px}
 h1{font-size:2rem;letter-spacing:-.025em;margin:0 0 6px}
 h2{font-size:1.1rem;margin:0 0 16px;letter-spacing:-.01em}
 .lead{color:var(--muted);margin:0 0 28px}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:24px}
 .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 .95rem/1 Inter,system-ui,sans-serif;
  padding:12px 20px;border-radius:12px;border:1px solid transparent;cursor:pointer;text-decoration:none;transition:.15s}
 .btn-primary{background:var(--accent);color:#fff;box-shadow:0 1px 2px rgba(79,70,229,.3),inset 0 1px 0 rgba(255,255,255,.15)}
 .btn-primary:hover{background:var(--accent-hover);transform:translateY(-1px)}
 .btn-primary:disabled{background:#c7d2fe;box-shadow:none;cursor:not-allowed;transform:none}
 .btn-ghost{background:var(--surface);border-color:var(--line);color:var(--ink)}
 .btn-ghost:hover{background:var(--soft)}
 .alert{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;border-radius:12px;margin-bottom:20px;font-size:.95rem}
 .alert svg{flex:none;width:18px;height:18px;margin-top:2px}
 .alert-bad{background:var(--bad-soft);color:var(--bad);border:1px solid #fecaca}
 .alert-ok{background:var(--ok-soft);color:var(--ok);border:1px solid #bbf7d0}
 .drop{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;min-height:260px;
  border:2px dashed #d6d3d1;border-radius:var(--radius);background:var(--soft);padding:32px;cursor:pointer;transition:.15s}
 .drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
 .drop .icon{width:52px;height:52px;border-radius:14px;background:var(--surface);display:grid;place-items:center;box-shadow:var(--shadow)}
 .drop .icon svg{width:24px;height:24px;stroke:var(--accent)}
 .drop strong{font-size:1rem} .drop span{color:var(--muted);font-size:.9rem}
 .drop img{max-height:220px;max-width:100%;border-radius:12px;box-shadow:var(--shadow)}
 .file-input{position:absolute;width:1px;height:1px;opacity:0}
 .actions{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:20px;flex-wrap:wrap}
 .hint{color:var(--muted);font-size:.85rem}
 .spin{width:16px;height:16px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:r .7s linear infinite;display:none}
 .busy .spin{display:inline-block}
 @keyframes r{to{transform:rotate(360deg)}}
 .grid2{display:grid;grid-template-columns:1.1fr 1fr;gap:24px;align-items:start}
 .photo{width:100%;border-radius:12px;display:block;background:var(--soft)}
 .meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
 .chip{display:inline-flex;align-items:center;gap:6px;font-size:.8rem;font-weight:500;color:var(--muted);background:var(--soft);
  border:1px solid var(--line);padding:4px 10px;border-radius:999px}
 .chip.accent{color:var(--accent);background:var(--accent-soft);border-color:#e0e7ff}
 .label{padding:12px 0;border-bottom:1px solid var(--soft)} .label:last-child{border-bottom:0}
 .label-row{display:flex;justify-content:space-between;font-size:.95rem;margin-bottom:8px}
 .label-row b{font-weight:600} .label-row span{color:var(--muted);font-variant-numeric:tabular-nums}
 .track{height:6px;border-radius:99px;background:var(--soft);overflow:hidden}
 .fill{height:100%;border-radius:99px;background:linear-gradient(90deg,#6366f1,#a855f7)}
 .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px}
 .tile{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;text-decoration:none;
  box-shadow:var(--shadow);transition:.15s;display:flex;flex-direction:column}
 .tile:hover{transform:translateY(-2px);box-shadow:0 12px 32px rgba(28,25,23,.10)}
 .tile img{width:100%;aspect-ratio:4/3;object-fit:cover;background:var(--soft);display:block}
 .tile-body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px}
 .tile-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .tile .chips{display:flex;flex-wrap:wrap;gap:6px}
 .empty{text-align:center;padding:56px 24px;color:var(--muted)}
 footer{max-width:1040px;margin:0 auto;padding:24px;color:var(--muted);font-size:.8rem;display:flex;justify-content:center;gap:8px;align-items:center}
 .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 3px #dcfce7}
 @media (max-width:760px){.grid2{grid-template-columns:1fr} h1{font-size:1.6rem} .top-in{padding:12px 16px} main{padding:28px 16px}}
</style></head><body>
<div class="top"><div class="top-in">
 <a class="brand" href="{{ url_for('index') }}"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></span>Visión</a>
 <nav class="tabs"><a href="{{ url_for('index') }}" class="{{ 'on' if request.endpoint in ('index', 'upload') else '' }}">Analizar</a><a href="{{ url_for('history') }}" class="{{ 'on' if request.endpoint in ('history', 'show') else '' }}">Historial</a></nav>
</div></div>
<main>{{ body|safe }}</main>
<footer><span class="dot"></span><span>Atendido por la réplica <strong>{{ instance }}</strong> · Amazon S3 · Rekognition · RDS</span></footer>
</body></html>"""

ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></svg>'

UPLOAD = """
<h1>Analiza una imagen</h1>
<p class="lead">Amazon Rekognition detecta objetos, escenas y conceptos, con su nivel de confianza.</p>
{% if error %}<div class="alert alert-bad">""" + ICON_ALERT + """<div>{{ error }}</div></div>{% endif %}
<form class="card" id="f" method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data">
 <label class="drop" id="drop" for="file">
  <span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M20 16v3a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-3"/></svg></span>
  <strong id="drop-title">Arrastra una imagen aquí o haz clic para elegirla</strong>
  <span id="drop-sub">JPG o PNG · máximo {{ max_mb }} MB</span>
 </label>
 <input class="file-input" id="file" type="file" name="image" accept=".jpg,.jpeg,.png,image/jpeg,image/png" required>
 <div class="actions">
  <span class="hint">La imagen se guarda en S3 y el resultado en la base de datos.</span>
  <button class="btn btn-primary" id="go" type="submit" disabled><span class="spin"></span><span id="go-text">Analizar imagen</span></button>
 </div>
</form>
<script>
(function(){
 var input=document.getElementById('file'), drop=document.getElementById('drop'), go=document.getElementById('go'),
     title=document.getElementById('drop-title'), sub=document.getElementById('drop-sub'), max={{ max_mb }}*1024*1024;
 function show(file){
  if(!file){return;}
  var mb=(file.size/1048576).toFixed(1);
  title.textContent=file.name; sub.textContent=mb+' MB · haz clic para cambiarla';
  go.disabled=file.size>max;
  if(file.size>max){sub.textContent=mb+' MB · supera el máximo de {{ max_mb }} MB';}
  var old=drop.querySelector('img'); if(old){old.remove();}
  if(/^image\\/(jpeg|png)$/.test(file.type)){
   var img=document.createElement('img'); img.alt=''; img.src=URL.createObjectURL(file);
   drop.querySelector('.icon').style.display='none'; drop.insertBefore(img, title);
  }
 }
 input.addEventListener('change', function(){ show(input.files[0]); });
 ['dragenter','dragover'].forEach(function(e){ drop.addEventListener(e, function(ev){ ev.preventDefault(); drop.classList.add('over'); }); });
 ['dragleave','drop'].forEach(function(e){ drop.addEventListener(e, function(ev){ ev.preventDefault(); drop.classList.remove('over'); }); });
 drop.addEventListener('drop', function(ev){ if(ev.dataTransfer.files.length){ input.files=ev.dataTransfer.files; show(input.files[0]); } });
 document.getElementById('f').addEventListener('submit', function(){ go.disabled=true; go.classList.add('busy'); document.getElementById('go-text').textContent='Analizando…'; });
})();
</script>"""

RESULT = """
{% if new %}<div class="alert alert-ok">""" + ICON_OK + """<div>Imagen procesada correctamente.</div></div>{% endif %}
<div class="grid2">
 <div class="card">
  <img class="photo" src="{{ image.url }}" alt="{{ image.original_name }}">
  <div class="meta">
   <span class="chip accent">{{ image.original_name }}</span>
   <span class="chip">{{ image.processed_at }}</span>
  </div>
 </div>
 <div class="card">
  <h2>Etiquetas detectadas</h2>
  {% if labels %}
  {% for name, conf in labels %}
  <div class="label">
   <div class="label-row"><b>{{ name }}</b><span>{{ '%.2f'|format(conf) }} %</span></div>
   <div class="track"><div class="fill" style="width:{{ conf }}%"></div></div>
  </div>
  {% endfor %}
  {% else %}<p class="hint">Rekognition no detectó etiquetas con confianza mayor o igual a {{ min_conf }} %.</p>{% endif %}
  <div class="actions">
   <a class="btn btn-ghost" href="{{ url_for('history') }}">Ver historial</a>
   <a class="btn btn-primary" href="{{ url_for('index') }}">Analizar otra</a>
  </div>
 </div>
</div>
<p class="hint" style="margin-top:16px">ID {{ image.id }}</p>"""

HISTORY = """
<h1>Historial</h1>
<p class="lead">Imágenes procesadas, de la más reciente a la más antigua.</p>
{% if rows %}
<div class="gallery">
 {% for r in rows %}
 <a class="tile" href="{{ url_for('show', image_id=r.id) }}">
  <img src="{{ r.url }}" alt="{{ r.original_name }}" loading="lazy">
  <div class="tile-body">
   <div class="tile-name">{{ r.original_name }}</div>
   <div class="chips">{% if r.top %}{% for t in r.top.split(', ') %}<span class="chip">{{ t }}</span>{% endfor %}{% else %}<span class="chip">Sin etiquetas</span>{% endif %}</div>
   <div class="hint">{{ r.processed_at }}</div>
  </div>
 </a>
 {% endfor %}
</div>
{% else %}
<div class="card empty">Todavía no hay imágenes procesadas.<div class="actions" style="justify-content:center"><a class="btn btn-primary" href="{{ url_for('index') }}">Analizar la primera</a></div></div>
{% endif %}"""


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
                error="La imagen supera el tamaño máximo de %d MB." % MAX_MB)


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
            msg = "Rekognition no pudo leer la imagen. Verifica que no esté dañada."
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
