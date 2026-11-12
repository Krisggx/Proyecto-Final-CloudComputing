"""Genera infra/imgproj-stack.yaml (la plantilla que se sube a CloudFormation).

Toma infra/template.src.yaml e inserta:
  __APP_PY_GZB64__        -> app/app.py comprimido (xz + base64)
  __APP_REQUIREMENTS__    -> paquetes de app/requirements.txt
  __FILE:ruta__           -> contenido del archivo indicado, con la sangria de la linea

Asi el codigo y las configuraciones tienen una sola fuente (sus archivos) y la
plantilla es autocontenida: no hace falta subir nada mas a AWS.

Uso:  python infra/build_template.py
"""
import base64
import lzma
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "infra" / "template.src.yaml"
OUT = ROOT / "infra" / "imgproj-stack.yaml"
USERDATA_LIMIT = 16 * 1024  # limite de EC2 para user data, en bytes


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def main():
    text = SRC.read_text(encoding="utf-8")

    app_py = read("app/app.py")
    if "${" in app_py:
        sys.exit("app.py contiene '${', que CloudFormation (Fn::Sub) interpretaria.")
    gz = base64.b64encode(lzma.compress(app_py.encode("utf-8"), format=lzma.FORMAT_XZ, preset=9 | lzma.PRESET_EXTREME)).decode("ascii")
    text = text.replace("__APP_PY_GZB64__", gz)

    reqs = [l.strip() for l in read("app/requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
    text = text.replace("__APP_REQUIREMENTS__", " ".join("'%s'" % r for r in reqs))

    def embed(match):
        indent, rel = match.group(1), match.group(2)
        body = read(rel).rstrip("\n")
        if "${" in body and not rel.startswith("haproxy/"):
            sys.exit("%s contiene '${', que CloudFormation (Fn::Sub) interpretaria." % rel)
        return "\n".join((indent + line) if line else "" for line in body.splitlines())

    text = re.sub(r"^([ \t]*)__FILE:([^_]+?)__$", embed, text, flags=re.M)

    leftovers = re.findall(r"__[A-Z_]+(?::[^_]+)?__", text)
    if leftovers:
        sys.exit("Marcadores sin reemplazar: %s" % leftovers)

    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("Generado %s (%d bytes)" % (OUT.relative_to(ROOT), len(text.encode("utf-8"))))
    print("app.py comprimido: %d bytes en base64" % len(gz))


if __name__ == "__main__":
    main()
