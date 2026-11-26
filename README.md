# dwg2gpkg-converter (prototype)

Web service: surveyors upload a CAD drawing (DWG/DXF) and download a
GeoPackage in the Yuk Generate PBT plugin schema
(layer `PBT_<number>`, MULTIPOLYGON, EPSG:23834 by default).

## Layout

- `app/main.py` — FastAPI service (`/probe`, `/convert`, `/health`, `/`)
- `app/static/index.html` — minimal upload + mapping form
- `app/requirements.txt` — Python deps
- `Dockerfile` — multi-stage, Cloud Run-ready (Debian slim + GDAL +
  LibreDWG built from source)
- `testdata/` — test inputs/outputs from the pipeline proof
- `TEST-REPORT.md` — what was tested, exact commands, pass/fail

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir app
# open http://localhost:8080/
```

Requires `ogr2ogr`, `ogrinfo` (gdal-bin) and `dwg2dxf` (LibreDWG) on PATH
for DWG input; DXF-only operation works with GDAL alone.

## Deploy (once a GCP project exists)

See `TEST-REPORT.md` for the exact `gcloud run deploy` command.
