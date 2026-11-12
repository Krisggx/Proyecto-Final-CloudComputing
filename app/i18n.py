"""Textos de la interfaz en español, inglés y kaqchikel.

- "es" está completo y es el idioma de respaldo: si falta una clave en otro
  idioma, se usa el texto en español.
- "kaq" es un BORRADOR parcial: solo traduce textos cortos (menú, títulos y
  botones). Debe revisarlo una persona hablante de kaqchikel antes de usarse
  en público; para corregirlo basta con editar este archivo.
- Los nombres de las etiquetas vienen de Amazon Rekognition y siempre están
  en inglés.
"""

LANGS = {"es": "Español", "en": "English", "kaq": "Kaqchikel (borrador)"}
HTML_LANG = {"es": "es", "en": "en", "kaq": "cak"}
DEFAULT = "es"

TEXT = {
    "es": {
        "nav_analyze": "Analizar",
        "nav_history": "Historial",
        "lang_label": "Idioma",
        "theme_title": "Modo claro / oscuro",
        "theme_aria": "Cambiar entre modo claro y oscuro",
        "footer": "Atendido por la réplica",
        "title_upload": "Subir imagen",
        "title_result": "Resultado",
        "title_history": "Historial",
        "up_h1a": "Descubre qué hay",
        "up_h1b": "en tu imagen",
        "up_lead": "Sube una foto y Amazon Rekognition detecta objetos, escenas y conceptos, cada uno con su nivel de confianza.",
        "up_drop_title": "Arrastra una imagen aquí o haz clic para elegirla",
        "up_drop_sub": "JPG o PNG · máximo {mb} MB",
        "up_hint": "La imagen se guarda en S3 y el resultado en la base de datos.",
        "up_button": "Analizar imagen",
        "up_busy": "Analizando…",
        "up_change": "haz clic para cambiarla",
        "up_too_big": "supera el máximo de {mb} MB",
        "feat1_t": "Validación segura",
        "feat1_d": "Se revisa el tipo real del archivo",
        "feat2_t": "Alta disponibilidad",
        "feat2_d": "Dos réplicas en dos zonas",
        "feat3_t": "Historial",
        "feat3_d": "Cada análisis queda registrado",
        "err_select": "Selecciona una imagen antes de enviar.",
        "err_format": "Formato no permitido. Solo se aceptan imágenes JPG o PNG.",
        "err_empty": "El archivo está vacío.",
        "err_size": "La imagen supera el tamaño máximo de {mb} MB.",
        "err_content": "El contenido del archivo no corresponde a una imagen {ext} válida.",
        "err_aws": "No se pudo analizar la imagen en AWS. Intenta de nuevo.",
        "err_unreadable": "Rekognition no pudo leer la imagen. Verifica que no esté dañada.",
        "err_db": "La imagen se analizó, pero no se pudo guardar el resultado en la base de datos.",
        "res_ok": "Imagen procesada correctamente.",
        "res_labels_1": "1 etiqueta",
        "res_labels_n": "{n} etiquetas",
        "res_delete": "Eliminar",
        "res_history": "Ver historial",
        "res_another": "Analizar otra",
        "res_confirm": "¿Eliminar esta imagen del historial? Esta acción no se puede deshacer.",
        "res_detected": "Etiquetas detectadas",
        "res_main": "Etiqueta principal",
        "res_none": "Rekognition no detectó etiquetas con confianza mayor o igual a {min} %.",
        "hist_count_1": "1 imagen",
        "hist_count_n": "{n} imágenes",
        "hist_h1": "Historial",
        "hist_lead": "Imágenes procesadas, de la más reciente a la más antigua.",
        "hist_clear": "Borrar historial",
        "hist_clear_confirm": "¿Borrar todo el historial? Se eliminarán {n} registro(s) de la base de datos. Esta acción no se puede deshacer.",
        "hist_deleted": "Se eliminó «{name}» del historial.",
        "hist_cleared_1": "Historial borrado: se eliminó 1 registro.",
        "hist_cleared_n": "Historial borrado: se eliminaron {n} registros.",
        "hist_del_title": "Eliminar esta imagen",
        "hist_del_confirm": "¿Eliminar esta imagen del historial?",
        "hist_no_labels": "Sin etiquetas",
        "hist_empty": "Todavía no hay imágenes procesadas.",
        "hist_first": "Analizar la primera",
    },
    "en": {
        "nav_analyze": "Analyze",
        "nav_history": "History",
        "lang_label": "Language",
        "theme_title": "Light / dark mode",
        "theme_aria": "Switch between light and dark mode",
        "footer": "Served by replica",
        "title_upload": "Upload image",
        "title_result": "Result",
        "title_history": "History",
        "up_h1a": "Discover what's",
        "up_h1b": "in your image",
        "up_lead": "Upload a photo and Amazon Rekognition detects objects, scenes and concepts, each with its confidence level.",
        "up_drop_title": "Drag an image here or click to choose one",
        "up_drop_sub": "JPG or PNG · up to {mb} MB",
        "up_hint": "The image is stored in S3 and the result in the database.",
        "up_button": "Analyze image",
        "up_busy": "Analyzing…",
        "up_change": "click to change it",
        "up_too_big": "exceeds the {mb} MB limit",
        "feat1_t": "Safe validation",
        "feat1_d": "The real file type is checked",
        "feat2_t": "High availability",
        "feat2_d": "Two replicas in two zones",
        "feat3_t": "History",
        "feat3_d": "Every analysis is recorded",
        "err_select": "Choose an image before submitting.",
        "err_format": "Format not allowed. Only JPG or PNG images are accepted.",
        "err_empty": "The file is empty.",
        "err_size": "The image exceeds the {mb} MB limit.",
        "err_content": "The file content is not a valid {ext} image.",
        "err_aws": "The image could not be analyzed in AWS. Please try again.",
        "err_unreadable": "Rekognition could not read the image. Make sure it is not damaged.",
        "err_db": "The image was analyzed, but the result could not be saved to the database.",
        "res_ok": "Image processed successfully.",
        "res_labels_1": "1 label",
        "res_labels_n": "{n} labels",
        "res_delete": "Delete",
        "res_history": "View history",
        "res_another": "Analyze another",
        "res_confirm": "Delete this image from the history? This cannot be undone.",
        "res_detected": "Detected labels",
        "res_main": "Top label",
        "res_none": "Rekognition found no labels with confidence of {min} % or higher.",
        "hist_count_1": "1 image",
        "hist_count_n": "{n} images",
        "hist_h1": "History",
        "hist_lead": "Processed images, from newest to oldest.",
        "hist_clear": "Clear history",
        "hist_clear_confirm": "Clear the whole history? {n} record(s) will be deleted from the database. This cannot be undone.",
        "hist_deleted": "“{name}” was deleted from the history.",
        "hist_cleared_1": "History cleared: 1 record deleted.",
        "hist_cleared_n": "History cleared: {n} records deleted.",
        "hist_del_title": "Delete this image",
        "hist_del_confirm": "Delete this image from the history?",
        "hist_no_labels": "No labels",
        "hist_empty": "No images have been processed yet.",
        "hist_first": "Analyze the first one",
    },
    # BORRADOR: revisar con una persona hablante de kaqchikel.
    "kaq": {
        "nav_analyze": "Tanik'oj",
        "nav_history": "Natab'äl",
        "lang_label": "Ch'ab'äl",
        "theme_title": "Saqil / Q'equ'm",
        "title_upload": "Tanik'oj",
        "title_history": "Natab'äl",
        "up_h1a": "Tatz'eta' achike k'o",
        "up_h1b": "pa ri awachib'äl",
        "up_drop_title": "Tacha' jun wachib'äl",
        "up_button": "Tanik'oj",
        "up_busy": "Tak'oyej…",
        "res_ok": "Xb'anatäj.",
        "res_delete": "Tasachaj",
        "res_history": "Natab'äl",
        "res_another": "Jun chik",
        "hist_count_1": "1 wachib'äl",
        "hist_count_n": "{n} wachib'äl",
        "hist_h1": "Natab'äl",
        "hist_clear": "Tasachaj ronojel",
        "hist_empty": "Majun wachib'äl k'a wakami.",
        "hist_first": "Tanik'oj ri nab'ey",
    },
}


def pick_lang(cookie_value, accept_languages):
    """Idioma elegido por cookie; si no hay, el del navegador (es o en); si no, español."""
    if cookie_value in LANGS:
        return cookie_value
    best = accept_languages.best_match(["es", "en"]) if accept_languages else None
    return best or DEFAULT


def translate(lang, key, **kwargs):
    text = TEXT.get(lang, {}).get(key) or TEXT[DEFAULT][key]
    return text.format(**kwargs) if kwargs else text
