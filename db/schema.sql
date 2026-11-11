-- Esquema de la base de datos (PostgreSQL).
-- La aplicacion lo crea automaticamente al iniciar; este archivo sirve
-- para crearlo o revisarlo a mano desde el bastion:  psql -f schema.sql
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

-- Consulta de evidencia para el informe:
-- SELECT i.original_name, l.name, l.confidence, i.processed_at
--   FROM images i JOIN labels l ON l.image_id = i.id
--  ORDER BY i.processed_at DESC, l.confidence DESC;
