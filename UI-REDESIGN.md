# Yuk Tech! UI Redesign — DWG2GPKG Converter

Redesign antarmuka untuk layanan DWG/DXF → GeoPackage.

## Perubahan
- Identitas Yuk Tech!: Deep Navy #071B49, Electric Blue #075BEB, Neon Cyan #00C9F2, Ice White #F5F8FF.
- Tipografi Plus Jakarta Sans.
- UI responsif untuk desktop dan mobile.
- Area upload drag-and-drop, validasi file 32 MB, pemeriksaan layer (/probe), pemilihan CRS (/crs), dan unduhan GPKG (/convert).
- Aset SVG lokal: simbol brand, maskot ilustratif, favicon.
- Mesin konversi dan skema API tetap menggunakan endpoint backend yang sudah ada.

## Struktur
- app/static/index.html
- app/static/styles.css
- app/static/app.js
- app/static/assets/

## Catatan backend
Aplikasi perlu menyajikan direktori static pada path /static. app/main.py menambahkan StaticFiles mount untuk itu. Endpoint /crs, /probe, /convert tetap dipertahankan.
