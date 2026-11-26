# dwg2gpkg-converter (prototype)

Web service: surveyors upload a CAD drawing (DWG/DXF) and download a
GeoPackage in the Yuk Generate PBT plugin schema
(layer `PBT_<number>`, MULTIPOLYGON, EPSG:23834 by default).

## Cara kerja (satu mode saja)

Satu jalur konversi (keputusan Derana, 2026-11-26 — mode lama "Umum"
dihapus di v0.3.0; kodenya ada di riwayat git kalau suatu saat perlu):
file ukur asli — batas persil berupa jaringan garis di lapisan `020100`,
persil SHM di `lb_lai`, dan NIB / nama / luas / No SU / Nomor Hak berupa
tulisan (TEXT) di lapisan `080201` / `tn_nama` / `luas` / `080202` /
`#080203`. Service mengubah jaringan garis jadi persil, menggabungkan
semua tulisan jadi atribut (lapisan TEXT lain yang belum dikenal otomatis
jadi field baru; tulisan di luar persil disimpan di lapisan `labels`),
dan menjaga garis pembatas (Jalan `040100`, Gang `040600`, Sungai
`03011`/`030100`, Selokan `030300`) tetap berupa garis terbuka di lapisan
kedua `dividers` — tidak pernah ditutup. Implementasi:
`app/surveyor_convert.py`, dijalankan sebagai subprocess (GDAL segfault
di dalam event loop uvicorn, jadi pekerjaan native-nya hidup di proses
terpisah).

Form di halaman hanya menanyakan yang berubah tiap pekerjaan: file,
sistem koordinat, nomor PBT, nomor PBTK, tanggal. Sisanya pakai nilai
tetap (lapisan asal `entities`, batas persil `020100`, SHM `lb_lai`)
dan nama lapisan hasil mengikuti nomor PBT (`PBT_<nomor>`).

Sistem koordinat dipilih pakai nama, bukan kode EPSG (Derana,
2026-11-26): TM-3 zona 46.2–54.1 (DGN95, 16 zona), UTM zona 46–54
Utara/Selatan (WGS84), UTM datum lama ID74 zona 46–52 Utara, dan
lintang/bujur WGS84 — 42 pilihan, diambil dari `GET /crs`. Nilai
bawaan: TM-3 zona 48.2.

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
