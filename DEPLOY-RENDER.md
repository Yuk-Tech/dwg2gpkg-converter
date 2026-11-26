# Deploy dwg2gpkg-converter ke Render (free)

Kode: repo GitHub berisi folder ini (`Dockerfile` di root, `app/` di bawahnya,
`render.yaml` di root). Tidak ada secret apa pun di repo ini.

## Sekali saja (di dashboard Render)

1. Buka https://dashboard.render.com -> **New +** -> **Blueprint**.
2. Pilih **Connect a repository** -> pilih repo `dwg2gpkg-converter`
   (kalau repo belum muncul, install dulu Render GitHub App ke akun/org GitHub).
3. Render membaca `render.yaml` -> klik **Apply**. Build pertama jalan
   (~10-15 menit, LibreDWG dibangun dari source; jatah build free 500 mnt/bln).
4. Setelah status **Live**, buka URL service-nya -> form upload muncul.

## Cek setelah deploy

- `GET <url>/health` -> `{"status":"ok","tools":{"ogr2ogr":true,"ogrinfo":true,"dwg2dxf":true}}`
- Upload `testdata/PBT_mini_surveyor.dxf` -> Convert -> unduh GPKG,
  buka di QGIS: layer `PBT_2091`, 6 fitur, NIB terisi.

## Catatan free tier (terverifikasi Nov 2026)

- 512 MB RAM, 0,1 CPU, 750 jam/bulan, tidur setelah 15 menit idle
  (request pertama bangun ~1 menit). Bandwidth 5 GB/bulan.
- File upload dibatasi aplikasi 32 MiB (`MAX_UPLOAD_BYTES` di `app/main.py`).
- Tidak ada kartu / tidak ada tagihan di free tier.
