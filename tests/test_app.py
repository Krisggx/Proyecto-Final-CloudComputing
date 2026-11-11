"""Pruebas locales de la aplicacion sin AWS: se reemplazan S3, Rekognition y la BD."""
import io
import pathlib
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
import app as appmod  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


@pytest.fixture
def client(monkeypatch):
    store = {"images": {}, "labels": {}, "s3": {}}

    monkeypatch.setattr(appmod, "store_image", lambda data, key, ct: store["s3"].__setitem__(key, data))
    monkeypatch.setattr(appmod, "analyze_image", lambda key: [("Perro", 98.12), ("Animal", 97.5)])

    def save_result(image_id, key, name, labels):
        store["images"][image_id] = (image_id, key, name, datetime.now(timezone.utc))
        store["labels"][image_id] = labels

    def query(sql, params=None, fetch="all"):
        if "SELECT 1" in sql:
            return (1,)
        if "FROM images WHERE id" in sql:
            return store["images"].get(params[0])
        if "FROM labels WHERE image_id" in sql:
            return store["labels"].get(params[0], [])
        if "FROM images i" in sql:
            return [row + ("Perro (98%)",) for row in store["images"].values()]
        raise AssertionError(sql)

    monkeypatch.setattr(appmod, "save_result", save_result)
    monkeypatch.setattr(appmod, "query", query)
    monkeypatch.setattr(appmod, "image_url", lambda key: "https://example.invalid/" + key)
    appmod.app.config["TESTING"] = True
    with appmod.app.test_client() as c:
        c.store = store
        yield c


def upload(client, data, name):
    return client.post("/upload", data={"image": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def test_upload_jpeg_flow(client):
    resp = upload(client, JPEG, "perro.jpg")
    assert resp.status_code == 302
    image_id = resp.headers["Location"].split("/images/")[1].split("?")[0]
    uuid.UUID(image_id)
    assert list(client.store["s3"]) == ["uploads/%s.jpg" % image_id]
    page = client.get(resp.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Perro" in body and "98.12 %" in body and "procesada correctamente" in body
    assert page.headers["X-Served-By"]


def test_upload_png_ok(client):
    assert upload(client, PNG, "foto.png").status_code == 302


def test_rejects_pdf_renamed_as_jpg(client):
    resp = upload(client, b"%PDF-1.4 fake", "doc.jpg")
    assert resp.status_code == 400
    assert "no corresponde" in resp.get_data(as_text=True)
    assert client.store["s3"] == {}


def test_rejects_wrong_extension(client):
    resp = upload(client, JPEG, "archivo.gif")
    assert resp.status_code == 400
    assert "Formato no permitido" in resp.get_data(as_text=True)


def test_rejects_too_large(client):
    big = JPEG + b"\x00" * (6 * 1024 * 1024)
    resp = upload(client, big, "grande.jpg")
    assert resp.status_code == 413
    assert "5 MB" in resp.get_data(as_text=True)


def test_rejects_missing_file(client):
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_history_and_health(client):
    upload(client, JPEG, "perro.jpg")
    hist = client.get("/history")
    assert hist.status_code == 200 and "perro.jpg" in hist.get_data(as_text=True)
    health = client.get("/health")
    assert health.status_code == 200 and health.get_json()["status"] == "ok"


def test_unknown_image_is_404(client):
    assert client.get("/images/no-es-uuid").status_code == 404
    assert client.get("/images/%s" % uuid.uuid4()).status_code == 404


def test_html_is_escaped(client):
    resp = upload(client, JPEG, "<script>x</script>.jpg")
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "<script>x</script>" not in page


def test_clear_history(client, monkeypatch):
    upload(client, JPEG, "perro.jpg")
    upload(client, PNG, "foto.png")
    real_query = appmod.query

    def query(sql, params=None, fetch="all"):
        if sql.startswith("WITH d AS (DELETE FROM images"):
            n = len(client.store["images"])
            client.store["images"].clear()
            client.store["labels"].clear()
            return (n,)
        return real_query(sql, params, fetch)

    monkeypatch.setattr(appmod, "query", query)
    page = client.get("/history").get_data(as_text=True)
    assert "Borrar historial" in page and "2 registro(s)" in page
    resp = client.post("/history/clear")
    assert resp.status_code == 302 and "cleared=2" in resp.headers["Location"]
    after = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "se eliminaron 2 registros" in after
    assert "Todavía no hay imágenes procesadas" in after
    assert "Borrar historial" not in after
