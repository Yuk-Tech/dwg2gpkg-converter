# dwg2gpkg-converter (prototype)

Web service: surveyors upload a CAD drawing (DWG/DXF) and download a
GeoPackage in the Yuk Generate PBT plugin schema
(layer `PBT_<number>`, MULTIPOLYGON, EPSG:23834 by default).

## Modes

- **Surveyor (default)** — real surveyor drawings: parcel boundaries are
  an open line network on layer `020100`, SHM parcels on `lb_lai`, and
  NIB / NAMA / LUAS / No SU / Nomor Hak are TEXT labels on layers
  `080201` / `tn_nama` / `luas` / `080202` / `#080203`. The service
  polygonises the network, joins the labels, and keeps divider lines
  (Jalan `040100`, Gang `040600`, Sungai `03011`/`030100`, Selokan
  `030300`) as OPEN LINES in a second `dividers` layer — they must
  never be closed. Implementation: `app/surveyor_convert.py`, run as a
  subprocess (GDAL's Python bindings segfault inside the uvicorn event
  loop, so the native work lives in a plain process).
- **Generic** — closed polygons + one label layer (per-feature
  ST_BuildArea, the original pipeline).

## Layout

- `app/main.py` — FastAPI service (`/probe`, `/convert`, `/health`, `/`)
- `app/surveyor_convert.py` — surveyor-mode conversion (subprocess)
- `app/static/index.html` — minimal upload + mapping form
- `app/requirements.txt` — Python deps
- `Dockerfile` — multi-stage, Render-ready (Ubuntu 24.04 + GDAL 3.8.4 +
  LibreDWG built from source; python3-gdal for surveyor mode)
- `testdata/` — test inputs/outputs from the pipeline proof
- `TEST-REPORT.md` — what was tested, exact commands, pass/fail
- `SANDBOX-TEST.md` — clean-room E2B test + the GDAL 3.6.2 finding

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir app
# open http://localhost:8080/
```

Requires `ogr2ogr`, `ogrinfo` (gdal-bin) and `dwg2dxf` (LibreDWG) on PATH
for DWG input; DXF-only operation works with GDAL alone. Surveyor mode
also needs `python3-gdal` (system python3) — the service calls it as
`/usr/bin/python3 app/surveyor_convert.py`.

## Deploy (Render)

See `DEPLOY-RENDER.md` for the exact dashboard steps (Web Service from
this repo, Docker runtime, Singapore, Free plan).
