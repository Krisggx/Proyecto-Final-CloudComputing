"""Genera infra/imgproj-stack.yaml (la plantilla que se sube a CloudFormation).

Toma infra/template.src.yaml e inserta:
  __APP_BUNDLE_PARAMS__   -> parámetros de SSM con el código de la aplicación
                             (app/app.py + app/i18n.py en tar.xz, base64, partes de 4 KB)
  __APP_FETCH__           -> línea del user data que descarga esos parámetros y
                             extrae el código en /opt/imgapp
  __APP_REQUIREMENTS__    -> paquetes de app/requirements.txt
  __FILE:ruta__           -> contenido del archivo indicado, con la sangría de la línea

El código viaja en parámetros de SSM (nivel estándar, sin costo) porque el user
data de EC2 está limitado a 16 KB. Así la plantilla sigue siendo autocontenida:
no hace falta subir nada más a AWS.

Uso:  python infra/build_template.py
"""
import base64
import io
import lzma
import pathlib
import re
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "infra" / "template.src.yaml"
OUT = ROOT / "infra" / "imgproj-stack.yaml"
APP_FILES = ["app.py", "i18n.py"]
CHUNK = 4000          # límite de un parámetro estándar de SSM: 4096 caracteres
USERDATA_LIMIT = 16 * 1024


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def bundle():
    """tar.xz determinista con los archivos de la aplicación, en base64."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name in APP_FILES:
            data = (ROOT / "app" / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    packed = lzma.compress(raw.getvalue(), format=lzma.FORMAT_XZ, preset=9 | lzma.PRESET_EXTREME)
    return base64.b64encode(packed).decode("ascii")


def main():
    text = SRC.read_text(encoding="utf-8")

    for name in APP_FILES:
        if "${" in read("app/" + name):
            sys.exit("app/%s contiene '${', que CloudFormation (Fn::Sub) interpretaría." % name)
    b64 = bundle()
    parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
    params = []
    for n, part in enumerate(parts, 1):
        params.append(
            "  AppBundlePart%d:\n"
            "    Type: AWS::SSM::Parameter\n"
            "    Properties:\n"
            "      Name: !Sub /imgproj/${AWS::StackName}/app/part%d\n"
            "      Type: String\n"
            "      Tier: Standard\n"
            "      Description: Codigo de la aplicacion, parte %d de %d (tar.xz en base64)\n"
            "      Value: %s" % (n, n, n, len(parts), part))
    text = text.replace("__APP_BUNDLE_PARAMS__", "\n\n".join(params))
    refs = " ".join("${AppBundlePart%d}" % n for n in range(1, len(parts) + 1))
    fetch = ('for p in %s; do aws ssm get-parameter --region ${AWS::Region} --name "$p" '
             '--query Parameter.Value --output text; done | tr -d "\\n" | base64 -d | xz -d | tar -x -C /opt/imgapp' % refs)
    text = re.sub(r"^([ \t]*)__APP_FETCH__$", lambda m: m.group(1) + fetch, text, flags=re.M)

    reqs = [l.strip() for l in read("app/requirements.txt").splitlines() if l.strip() and not l.startswith("#")]
    text = text.replace("__APP_REQUIREMENTS__", " ".join("'%s'" % r for r in reqs))

    def embed(match):
        indent, rel = match.group(1), match.group(2)
        body = read(rel).rstrip("\n")
        if "${" in body and not rel.startswith("haproxy/"):
            sys.exit("%s contiene '${', que CloudFormation (Fn::Sub) interpretaría." % rel)
        return "\n".join((indent + line) if line else "" for line in body.splitlines())

    text = re.sub(r"^([ \t]*)__FILE:([^_]+?)__$", embed, text, flags=re.M)

    leftovers = re.findall(r"__[A-Z_]+(?::[^_]+)?__", text)
    if leftovers:
        sys.exit("Marcadores sin reemplazar: %s" % leftovers)

    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("Generado %s (%d bytes)" % (OUT.relative_to(ROOT), len(text.encode("utf-8"))))
    print("Código de la aplicación: %d caracteres en %d parámetros de SSM" % (len(b64), len(parts)))


if __name__ == "__main__":
    main()
