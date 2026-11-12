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
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · Visión</title>
<script>
(function(){var t=null;try{t=localStorage.getItem('vision-theme');}catch(e){}
 if(t!=='light'&&t!=='dark'){t=(window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';}
 document.documentElement.setAttribute('data-theme',t);})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
<style>
 :root{--bg:#f8f8fb;--bg-glow:rgba(99,102,241,.10);--surface:#ffffff;--surface-2:#f4f4f7;--line:#e6e6ec;--line-strong:#d4d4dc;
  --ink:#16161d;--muted:#6b6b78;--accent:#5b5bd6;--accent-2:#9b5de5;--accent-hover:#4a4ac4;--accent-soft:#eef0ff;--accent-ink:#4338ca;
  --ok:#15803d;--ok-soft:#effbf3;--ok-line:#c6eed4;--bad:#c42b2b;--bad-soft:#fff2f2;--bad-line:#f6d0d0;
  --shadow:0 1px 2px rgba(22,22,29,.04),0 10px 30px -12px rgba(22,22,29,.12);--shadow-lg:0 24px 48px -20px rgba(22,22,29,.25);
  --radius:18px;--track:#ececf2;--header:rgba(248,248,251,.72);color-scheme:light}
 [data-theme="dark"]{--bg:#0c0c12;--bg-glow:rgba(124,108,255,.16);--surface:#15151d;--surface-2:#1c1c26;--line:#262633;--line-strong:#33334a;
  --ink:#ececf3;--muted:#9a9aab;--accent:#8b8cff;--accent-2:#c084fc;--accent-hover:#a5a6ff;--accent-soft:#1f1f3a;--accent-ink:#b8b9ff;
  --ok:#4ade80;--ok-soft:#10261a;--ok-line:#1d4a30;--bad:#f87171;--bad-soft:#2a1416;--bad-line:#4a2226;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px -12px rgba(0,0,0,.6);--shadow-lg:0 24px 48px -20px rgba(0,0,0,.8);
  --track:#262633;--header:rgba(12,12,18,.72);color-scheme:dark}
 *{box-sizing:border-box}
 html,body{transition:background-color .25s,color .25s}
 body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.55;display:flex;flex-direction:column}
 body::before{content:"";position:fixed;inset:-20vh 0 auto 0;height:70vh;pointer-events:none;z-index:0;
  background:radial-gradient(60% 60% at 50% 0%,var(--bg-glow),transparent 70%)}
 a{color:inherit}
 .top{position:sticky;top:0;z-index:10;background:var(--header);backdrop-filter:saturate(1.4) blur(14px);-webkit-backdrop-filter:saturate(1.4) blur(14px);border-bottom:1px solid var(--line)}
 .top-in{max-width:1080px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;gap:12px}
 .brand{display:flex;align-items:center;gap:10px;text-decoration:none;font-weight:700;letter-spacing:-.02em;font-size:1.05rem;margin-right:auto}
 .logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent-2));display:grid;place-items:center;box-shadow:0 6px 16px -6px var(--accent)}
 .logo svg{width:17px;height:17px;stroke:#fff}
 .tabs{display:flex;gap:2px;background:var(--surface-2);border:1px solid var(--line);padding:3px;border-radius:999px}
 .tabs a{text-decoration:none;font-size:.88rem;font-weight:500;color:var(--muted);padding:6px 14px;border-radius:999px;transition:.15s}
 .tabs a:hover{color:var(--ink)}
 .tabs a.on{background:var(--surface);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.12)}
 .theme{width:38px;height:38px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--ink);display:grid;place-items:center;cursor:pointer;transition:.15s}
 .theme:hover{border-color:var(--line-strong);transform:rotate(-12deg)}
 .theme svg{width:18px;height:18px}
 .theme .sun{display:none} [data-theme="dark"] .theme .sun{display:block} [data-theme="dark"] .theme .moon{display:none}
 main{position:relative;z-index:1;width:100%;max-width:1080px;margin:0 auto;padding:48px 24px 32px;flex:1}
 .eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:.78rem;font-weight:600;letter-spacing:.02em;color:var(--accent-ink);
  background:var(--accent-soft);border:1px solid transparent;padding:5px 12px;border-radius:999px;margin-bottom:16px}
 .eyebrow i{width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block}
 h1{font-size:2.4rem;line-height:1.1;letter-spacing:-.035em;margin:0 0 10px;font-weight:800}
 h1 .grad{background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;background-clip:text;color:transparent}
 h2{font-size:1.05rem;margin:0 0 14px;letter-spacing:-.01em}
 .lead{color:var(--muted);margin:0 0 30px;font-size:1.05rem;max-width:620px}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:24px}
 .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:600 .93rem/1 Inter,system-ui,sans-serif;
  padding:12px 20px;border-radius:12px;border:1px solid transparent;cursor:pointer;text-decoration:none;transition:transform .15s,background .15s,box-shadow .15s,opacity .15s}
 .btn svg{width:16px;height:16px}
 .btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;box-shadow:0 8px 20px -8px var(--accent)}
 .btn-primary:hover{transform:translateY(-1px);box-shadow:0 12px 26px -10px var(--accent)}
 .btn-primary:disabled{opacity:.45;box-shadow:none;cursor:not-allowed;transform:none}
 .btn-ghost{background:var(--surface);border-color:var(--line);color:var(--ink)}
 .btn-ghost:hover{background:var(--surface-2);border-color:var(--line-strong)}
 .btn-danger{background:transparent;border-color:var(--bad-line);color:var(--bad)}
 .btn-danger:hover{background:var(--bad-soft)}
 .alert{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;border-radius:14px;margin-bottom:22px;font-size:.94rem;border:1px solid}
 .alert svg{flex:none;width:18px;height:18px;margin-top:2px}
 .alert-bad{background:var(--bad-soft);color:var(--bad);border-color:var(--bad-line)}
 .alert-ok{background:var(--ok-soft);color:var(--ok);border-color:var(--ok-line)}
 .drop{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;min-height:280px;
  border:1.5px dashed var(--line-strong);border-radius:16px;background:var(--surface-2);padding:32px;cursor:pointer;transition:.2s}
 .drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
 .drop .icon{width:56px;height:56px;border-radius:16px;background:var(--surface);border:1px solid var(--line);display:grid;place-items:center;box-shadow:var(--shadow);transition:.2s}
 .drop:hover .icon{transform:translateY(-3px)}
 .drop .icon svg{width:24px;height:24px;stroke:var(--accent)}
 .drop strong{font-size:1rem} .drop span{color:var(--muted);font-size:.9rem}
 .drop img{max-height:230px;max-width:100%;border-radius:12px;box-shadow:var(--shadow-lg)}
 .file-input{position:absolute;width:1px;height:1px;opacity:0}
 .actions{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:20px;flex-wrap:wrap}
 .hint{color:var(--muted);font-size:.85rem}
 .spin{width:16px;height:16px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:r .7s linear infinite;display:none}
 .busy .spin{display:inline-block}
 @keyframes r{to{transform:rotate(360deg)}}
 .features{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:18px}
 .feature{display:flex;gap:10px;align-items:flex-start;padding:14px 16px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}
 .feature svg{flex:none;width:18px;height:18px;stroke:var(--accent);margin-top:2px}
 .feature b{display:block;font-size:.88rem} .feature span{color:var(--muted);font-size:.8rem}
 .result-head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:22px}
 .result-head h1{font-size:1.8rem;margin:0 0 6px;word-break:break-all}
 .grid2{display:grid;grid-template-columns:1.15fr 1fr;gap:22px;align-items:start}
 .photo{width:100%;border-radius:12px;display:block;background:var(--surface-2)}
 .meta{display:flex;flex-wrap:wrap;gap:8px}
 .chip{display:inline-flex;align-items:center;gap:6px;font-size:.78rem;font-weight:500;color:var(--muted);background:var(--surface-2);
  border:1px solid var(--line);padding:4px 10px;border-radius:999px}
 .chip.accent{color:var(--accent-ink);background:var(--accent-soft);border-color:transparent}
 .top-label{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 18px;border-radius:14px;margin-bottom:8px;
  background:linear-gradient(135deg,var(--accent-soft),transparent);border:1px solid var(--line)}
 .top-label small{display:block;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600}
 .top-label b{font-size:1.35rem;letter-spacing:-.02em}
 .top-label .pct{font-size:1.6rem;font-weight:800;letter-spacing:-.03em;background:linear-gradient(90deg,var(--accent),var(--accent-2));-webkit-background-clip:text;background-clip:text;color:transparent}
 .label{padding:11px 2px;border-bottom:1px solid var(--line)} .label:last-child{border-bottom:0}
 .label-row{display:flex;justify-content:space-between;font-size:.93rem;margin-bottom:7px}
 .label-row b{font-weight:600} .label-row span{color:var(--muted);font-variant-numeric:tabular-nums}
 .track{height:6px;border-radius:99px;background:var(--track);overflow:hidden}
 .fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent),var(--accent-2))}
 .page-head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:28px}
 .page-head .lead{margin:0}
 .gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:20px}
 .tile{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;text-decoration:none;
  box-shadow:var(--shadow);transition:transform .2s,box-shadow .2s,border-color .2s;display:flex;flex-direction:column}
 .tile:hover{transform:translateY(-4px);box-shadow:var(--shadow-lg);border-color:var(--line-strong)}
 .thumb{position:relative;overflow:hidden;aspect-ratio:4/3;background:var(--surface-2)}
 .thumb img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .4s}
 .tile:hover .thumb img{transform:scale(1.05)}
 .thumb .badge{position:absolute;left:10px;bottom:10px;font-size:.75rem;font-weight:600;color:#fff;background:rgba(0,0,0,.55);
  backdrop-filter:blur(6px);padding:4px 10px;border-radius:999px}
 .tile-body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px}
 .tile-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .tile .chips{display:flex;flex-wrap:wrap;gap:6px}
 .empty{text-align:center;padding:64px 24px;color:var(--muted)}
 .empty .icon{width:56px;height:56px;margin:0 auto 14px;border-radius:16px;background:var(--surface-2);display:grid;place-items:center}
 .empty .icon svg{width:26px;height:26px;stroke:var(--muted)}
 footer{position:relative;z-index:1;max-width:1080px;width:100%;margin:0 auto;padding:22px 24px 28px;color:var(--muted);font-size:.8rem;
  display:flex;justify-content:center;gap:8px;align-items:center;flex-wrap:wrap}
 .dot{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.2)}
 @media (max-width:760px){.grid2,.features{grid-template-columns:1fr} h1{font-size:1.8rem} .top-in{padding:10px 14px} main{padding:30px 16px}
  .brand span.name{display:none}}
</style></head><body>
<div class="top"><div class="top-in">
 <a class="brand" href="{{ url_for('index') }}"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></span><span class="name">Visión</span></a>
 <nav class="tabs"><a href="{{ url_for('index') }}" class="{{ 'on' if request.endpoint in ('index', 'upload') else '' }}">Analizar</a><a href="{{ url_for('history') }}" class="{{ 'on' if request.endpoint in ('history', 'show') else '' }}">Historial</a></nav>
 <button class="theme" id="theme" type="button" aria-label="Cambiar entre modo claro y oscuro" title="Modo claro / oscuro">
  <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
  <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
 </button>
</div></div>
<main>{{ body|safe }}</main>
<footer><span class="dot"></span><span>Atendido por la réplica <strong>{{ instance }}</strong> · Amazon S3 · Rekognition · RDS</span></footer>
<script>
document.getElementById('theme').addEventListener('click', function(){
 var t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
 document.documentElement.setAttribute('data-theme', t);
 try { localStorage.setItem('vision-theme', t); } catch (e) {}
});
</script>
</body></html>"""

ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/></svg>'

UPLOAD = """
<span class="eyebrow"><i></i>Amazon Rekognition · S3 · RDS</span>
<h1>Descubre qué hay <span class="grad">en tu imagen</span></h1>
<p class="lead">Sube una foto y Amazon Rekognition detecta objetos, escenas y conceptos, cada uno con su nivel de confianza.</p>
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
<div class="features">
 <div class="feature"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/></svg><div><b>Validación segura</b><span>Se revisa el tipo real del archivo</span></div></div>
 <div class="feature"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/></svg><div><b>Alta disponibilidad</b><span>Dos réplicas en dos zonas</span></div></div>
 <div class="feature"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><div><b>Historial</b><span>Cada análisis queda registrado</span></div></div>
</div>
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
<div class="result-head">
 <div>
  <h1>{{ image.original_name }}</h1>
  <div class="meta"><span class="chip">{{ image.processed_at }}</span><span class="chip accent">{{ labels|length }} etiqueta{{ '' if labels|length == 1 else 's' }}</span></div>
 </div>
 <div class="meta">
  <a class="btn btn-ghost" href="{{ url_for('history') }}">Ver historial</a>
  <a class="btn btn-primary" href="{{ url_for('index') }}">Analizar otra</a>
 </div>
</div>
<div class="grid2">
 <div class="card"><img class="photo" src="{{ image.url }}" alt="{{ image.original_name }}"></div>
 <div class="card">
  <h2>Etiquetas detectadas</h2>
  {% if labels %}
  <div class="top-label"><div><small>Etiqueta principal</small><b>{{ labels[0][0] }}</b></div><span class="pct">{{ '%.0f'|format(labels[0][1]) }} %</span></div>
  {% for name, conf in labels %}
  <div class="label">
   <div class="label-row"><b>{{ name }}</b><span>{{ '%.2f'|format(conf) }} %</span></div>
   <div class="track"><div class="fill" style="width:{{ conf }}%"></div></div>
  </div>
  {% endfor %}
  {% else %}<p class="hint">Rekognition no detectó etiquetas con confianza mayor o igual a {{ min_conf }} %.</p>{% endif %}
 </div>
</div>
<p class="hint" style="margin-top:16px">ID {{ image.id }}</p>"""

HISTORY = """
{% if cleared is not none %}<div class="alert alert-ok">""" + ICON_OK + """<div>Historial borrado: se eliminaron {{ cleared }} registro{{ '' if cleared == 1 else 's' }}.</div></div>{% endif %}
<div class="page-head">
 <div><span class="eyebrow"><i></i>{{ rows|length }} imagen{{ '' if rows|length == 1 else 'es' }}</span><h1>Historial</h1><p class="lead">Imágenes procesadas, de la más reciente a la más antigua.</p></div>
 {% if rows %}
 <form method="post" action="{{ url_for('clear_history') }}" onsubmit="return confirm('¿Borrar todo el historial? Se eliminarán {{ rows|length }} registro(s) de la base de datos. Esta acción no se puede deshacer.');">
  <button class="btn btn-danger" type="submit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg>Borrar historial</button>
 </form>
 {% endif %}
</div>
{% if rows %}
<div class="gallery">
 {% for r in rows %}
 <a class="tile" href="{{ url_for('show', image_id=r.id) }}">
  <div class="thumb"><img src="{{ r.url }}" alt="{{ r.original_name }}" loading="lazy">{% if r.top %}<span class="badge">{{ r.top.split(', ')[0] }}</span>{% endif %}</div>
  <div class="tile-body">
   <div class="tile-name">{{ r.original_name }}</div>
   <div class="chips">{% if r.top %}{% for t in r.top.split(', ') %}<span class="chip">{{ t }}</span>{% endfor %}{% else %}<span class="chip">Sin etiquetas</span>{% endif %}</div>
   <div class="hint">{{ r.processed_at }}</div>
  </div>
 </a>
 {% endfor %}
</div>
{% else %}
<div class="card empty"><div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg></div>Todavía no hay imágenes procesadas.<div class="actions" style="justify-content:center"><a class="btn btn-primary" href="{{ url_for('index') }}">Analizar la primera</a></div></div>
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
                    error="La imagen se analizó, pero no se pudo guardar el resultado en la base de datos.")
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
    cleared = request.args.get("cleared")
    return page("Historial", HISTORY, rows=items,
                cleared=int(cleared) if cleared and cleared.isdigit() else None)


@app.post("/history/clear")
def clear_history():
    """Borra los registros del historial (images y, en cascada, labels).

    Las imágenes se conservan en S3 como respaldo: el rol de las instancias
    no tiene permiso de borrado (privilegio mínimo) y el bucket está versionado.
    """
    row = query("WITH d AS (DELETE FROM images RETURNING 1) SELECT count(*) FROM d", fetch="one")
    deleted = int(row[0]) if row else 0
    log.info("Historial borrado: %d registros", deleted)
    return redirect(url_for("history", cleared=deleted))


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
