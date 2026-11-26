# TEST REPORT — dwg2gpkg-converter prototype

Date: 2026-11-26. Environment: local VM, Ubuntu 24.04 (noble), GDAL 3.8.4,
Python 3.12.3, LibreDWG 0.13.3 (built from source — see §1).
Reference data: `~/workspace/user/files/PBT_2091.gpkg`
(124 features, layer `PBT_2091`, MULTIPOLYGON, EPSG:23834).

Target schema (Yuk Generate PBT plugin): layer `PBT_<number>`,
MULTIPOLYGON, EPSG:23834, fields `080201` (NIB), `NAMA`, `LUAS`
(`'L=177M2'`), `M2` (clean area), `A` (NIK), `080203` (Nomor Hak),
`PBTK`, `TGLK`, `PBT`, `B`, `KET`.

## 1. Environment setup

- `sudo apt-get install -y gdal-bin` → GDAL 3.8.4. **PASS.**
- LibreDWG is **not** in Ubuntu 24.04 apt repos (`apt-cache search
  libredwg` → empty). Built from source instead:
  `git clone --depth 1 --branch 0.13.3 https://github.com/LibreDWG/libredwg.git`,
  `autoreconf --install --symlink -I m4`, `./configure --disable-python
  --disable-trace`, `make -j2`, `sudo make install`, `ldconfig`.
  - Gotcha 1: `autogen.sh` hangs on `git fetch --tags` (slow network).
    Workaround: write `.tarball-version`/`.version` manually (`0.13.3`)
    and run `autoreconf` + `git submodule update --init --recursive`
    directly.
  - Gotcha 2: a runtime restart mid-task wiped `/tmp` (source tree) and
    all apt-installed packages. Re-cloned into the persistent workspace
    (`dwg2gpkg-converter/.build-src/libredwg`) and re-installed
    gdal-bin + build toolchain. The Dockerfile builds LibreDWG in a
    builder stage, so a deployed image does not depend on this VM.
  - Build result: **built and installed.** `make -j2` took ~9 min on
    this VM and exited 2 — but only because the `doc/` subdirectory
    failed (`LibreDWG.info`, Error 127: `makeinfo` not installed). The
    library and all programs built fine (`programs/dwg2dxf`,
    `dwgread`, `dwgwrite`, `dxf2dwg` all present). Installed with
    `make -C src install` + `make -C programs install` + `ldconfig`:
    `dwg2dxf 0.13.3` verified at `/usr/local/bin/dwg2dxf`. The
    Dockerfile now installs `texinfo` in the builder stage so a plain
    `make` succeeds there.

## 2. Test inputs

| File | How made | Contents |
|---|---|---|
| `testdata/PBT_2091.dxf` (87 KB) | `ogr2ogr -f DXF PBT_2091.dxf PBT_2091.gpkg -nln PBT_2091` | 124 HATCH polygons, single `entities` layer. **Attributes are LOST**: DXF has no attribute table — GDAL logs `ERROR 1: DXF layer does not support arbitrary field creation` for all 17 fields. This is the real-world situation: a surveyor DXF carries geometry + TEXT, not an attribute table. |
| `testdata/PBT_mini_surveyor.dxf` (11 features) | Hand-written by `testdata/make_mini_surveyor_dxf.py` (GDAL's DXF writer cannot emit TEXT entities) | 5 closed LWPOLYLINE parcels on layer `BIDANG`, 1 on `JALAN`, 5 TEXT entities on layer `NIB` at parcel centroids with real NIB values from the reference GPKG. |
| `testdata/PBT_2091_labels.dxf` (248 features) | `ogr2ogr` UNION ALL parcels + `ST_Centroid` points | **Abandoned as a label test**: GDAL writes the label points as POINT entities and drops the `Text` attribute (0 TEXT entities in the file). Kept as a negative result. |

## 3. Pipeline proof

### 3a. DXF → GPKG (ogr2ogr, SQLITE dialect)

Command (equivalent of what the service runs):

```sh
ogr2ogr -f GPKG PBT_2091_from_dxf.gpkg PBT_2091.dxf -nln PBT_2091 \
  -nlt MULTIPOLYGON -s_srs EPSG:23834 -t_srs EPSG:23834 -dialect SQLITE \
  -sql "SELECT CAST(NULL AS TEXT) AS '080201', ...,
        ('L=' || CAST(CAST(ST_Area(geometry) AS INTEGER) AS TEXT) || 'M2') AS LUAS,
        CAST(CAST(ST_Area(geometry) AS INTEGER) AS TEXT) AS M2, ...
        FROM entities WHERE ST_GeometryType(geometry) IN ('POLYGON','MULTIPOLYGON')"
```

Result: **PASS.** Layer `PBT_2091`, Multi Polygon, **124/124 features**,
EPSG:23834, all 11 schema fields present. Area check (Python/osgeo,
sorted per-feature areas vs reference): **identical** — total 50,211 m²
both sides. Sample: fid 1 `LUAS='L=177M2'`, `M2='177'` (matches reference).

### 3b. Surveyor-style DXF (LWPOLYLINE + TEXT) → GPKG via the service

Two real-world behaviours found and fixed in the service SQL:

1. **GDAL reads closed LWPOLYLINE parcels as LINESTRING, not POLYGON**
   (verified: `ST_GeometryType` = `LINESTRING` for all 5 BIDANG + 1
   JALAN). Fix: polygonise with `ST_BuildArea()` in the SQL
   (`CASE WHEN ...='LINESTRING' THEN ST_BuildArea(geometry) ELSE geometry END`).
   `ST_Force2D` does **not** exist in this SpatiaLite build — not used.
2. **TEXT entities read back as `POINT Z`**, so a filter on
   `ST_GeometryType(...) = 'POINT'` matches nothing. Fix: `LIKE 'POINT%'`.

Service test (live HTTP, `POST /convert`, `label_layer=NIB`,
`label_field=Text`, `field_map={"KET":"Layer"}`):

```sh
curl -F "file=@PBT_mini_surveyor.dxf" -F "source_layer=entities" \
  -F "target_layer=PBT_2091" -F "target_epsg=23834" \
  -F 'field_map={"KET": "Layer"}' -F "pbt=2091" -F "pbtk=2091" \
  -F "tglk=06 November 2026" -F "label_layer=NIB" -F "label_field=Text" \
  http://127.0.0.1:8080/convert
```

Result: **PASS.** HTTP 200, layer `PBT_2091`, Multi Polygon,
**6/6 features**, EPSG:23834. NIBs from the spatial label join match the
reference exactly (`00377, 00793, 00794, 00795, 00796`; JALAN → NULL).
`KET` mapped from the DXF `Layer` column (`BIDANG` ×5, `JALAN` ×1).
`M2`/`LUAS` computed from geometry (`177` → `L=177M2`).

### 3c. DWG → DXF → GPKG

**DWG test-file production — FAILED (LibreDWG write path is broken
for the entity types that matter).** Two attempts:

1. `dxf2dwg PBT_2091.dxf -o PBT_2091.dwg` (30 KB, exit 0, one warning
   `Object handle not found 131196/0x2007C`). The resulting DWG is
   corrupt: `dwgread` reports `ERROR: Invalid object handle`,
   `bit_read_TV buffer overflow`, `Invalid HATCH.num_paths 429185569`.
   `dwg2dxf` on it exits 0 but produces a DXF with **0 features**.
2. `dxf2dwg PBT_mini_surveyor.dxf -o PBT_mini.dwg` (3.6 KB, exit 0).
   `dwg2dxf` on it exits 0 but produces a **truncated DXF** (942 lines,
   ends mid-TABLES after `ENDTAB`, no ENTITIES section, no EOF) that
   GDAL refuses to open (`error at line 943`).
3. `dwgwrite -o PBT_mini_w.dwg PBT_mini_surveyor.dxf` prints
   `ERROR: Unhandled preR13 class LWPOLYLINE, fixedtype 77` and writes
   a 2.6 KB DWG that makes **`dwgread` hang** (killed after >60 s with
   no output).

**Conclusion: LibreDWG 0.13.3 cannot manufacture a valid test DWG from
a DXF containing HATCH or LWPOLYLINE entities.** Its write path is not
a reliable way to create DWG test fixtures. (Its read path is a
different story — see below.)

**DWG read path — PASS, across versions.** LibreDWG ships real sample
DWGs (`libredwg/test/test-data/example_{2000,2004,2007,2010,2013,2018}.dwg`,
presumably AutoCAD-produced; the matching reference DXFs each have 125
entities). Results of `dwg2dxf example_$v.dwg`:

| Version | dwg2dxf exit | Features in converted DXF (ref: 125) |
|---|---|---|
| R2000 | 0 | 123 |
| R2004 | 0 | 123 |
| R2007 | 0 | 123 |
| R2010 | 0 | 123 |
| R2013 | 0 | 123 |
| R2018 | 0 | 123 |

123 vs 125: 2 entities are dropped (most likely the WIPEOUT entity —
`dwg2dxf` logs `Warning: Unstable Class entity 504 WIPEOUT` — and one
other). `dwgread` on the samples exits 0 (`SUCCESS`). No R2019+ sample
exists in the tree, so R2019/R2021+ read support is **untested here**;
LibreDWG documents R2018 as the newest well-supported version.

**Service DWG end-to-end — PASS (after two real bugs were found and
fixed).** `POST /convert` with each `example_$v.dwg`:

- First attempt: **HTTP 422.** Root cause: the DXF driver emits errors
  on some entities in dwg2dxf's output (VIEWPORT, 3D solids —
  `error at line 8226`, `Unexpected geometry type: POLYHEDRALSURFACE`,
  `error at line 33440`). `ogrinfo` tolerates these and still returns
  rows; `ogr2ogr` aborts the translation. **Fix 1:** `-skipfailures`
  on the ogr2ogr call — the parseable parcels are then written.
  (Trade-off: unparseable entities are silently dropped; the output
  feature count is the honest signal.)
- Second attempt: HTTP 200 but the GPKG had **25 features, ALL with
  NULL geometry**. Root cause: the service SQL had no outer filter, so
  rows whose `_poly` is an empty polygon (unclosed LINESTRING) or a
  3D polygon (POLYGON Z) were written — and ogr2ogr then wrote NULL
  geometry for *every* feature. **Fix 2:** outer
  `WHERE ST_GeometryType(p._poly) IN ('POLYGON','MULTIPOLYGON') AND NOT
  ST_IsEmpty(p._poly)` in `_build_convert_sql`. (Verified in isolation:
  without the filter 25 features/25 NULL; with it 21 features/0 NULL.)
- Final result: **all six versions → HTTP 200, 21 features, Multi
  Polygon, EPSG:23834, 0 NULL geometries.** The 21 = 13 2D POLYGONs +
  8 closed LINESTRINGs polygonised by ST_BuildArea; the 46 POINT Z,
  35 LINESTRING Z, 4 POLYGON Z, 8 GEOMETRYCOLLECTION Z and other 3D
  entities are excluded by design (the plugin schema is 2D parcels).

Note the earlier false alarm: the first dwg2dxf matrix run showed
exit 1 for all versions — that was my own error (output path
`/tmp/dwg2gpkg-converter/testdata/` did not exist; `WRITE ERROR`).
Re-run with a valid path: exit 0 for all six versions.

## 4. Service smoke tests (live, http://127.0.0.1:8080)

| Test | Result |
|---|---|
| `GET /health` | **PASS** — `{"status":"ok","tools":{"ogr2ogr":true,"ogrinfo":true,"dwg2dxf":true}}` |
| `GET /` (HTML form) | **PASS** — HTTP 200 |
| `POST /probe` on `PBT_2091.dxf` | **PASS** — layer `entities`, 124 features, fields `Layer, PaperSpace, SubClasses, Linetype, EntityHandle, Text`, 3 sample features. (Fix applied during testing: GDAL 3.8 `ogrinfo -json` only includes `features` with `-features`.) |
| `POST /probe` on `example_2000.dwg` (real R2000 DWG) | **PASS** — layer `entities`, 123 features, same field list. |
| `POST /convert` on `PBT_2091.dxf` | **PASS** — HTTP 200, 124/124 features, areas identical to reference (§3a). |
| `POST /convert` on `PBT_mini_surveyor.dxf` (+ label join) | **PASS** — §3b. |
| `POST /convert` on `example_{2000,2004,2007,2010,2013,2018}.dwg` | **PASS** — HTTP 200 each, 21 features, Multi Polygon, EPSG:23834, 0 NULL geometries (§3c). |
| Bugs fixed during testing | `BackgroundTask` import is `starlette.background`, not `fastapi.background` (HTTP 500 on first /convert). `-skipfailures` added to ogr2ogr (one bad CAD entity aborted whole translations). Outer polygon filter added to the convert SQL (missing filter wrote all-NULL geometries). Non-zero ogr2ogr exit now accepted when the output GPKG layer is readable (ogr2ogr exits 1 even with `-skipfailures` when translation terminates early). |

Run locally:

```sh
cd ~/workspace/work/dwg2gpkg-converter
python3 -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --app-dir app
```

## 5. DWG support (LibreDWG 0.13.3) — READ proven, WRITE broken

**Read (what the service needs): PROVEN.** `dwg2dxf` converts real
DWGs R2000–R2018 (LibreDWG's own sample files): exit 0, 123/125
entities each; the service turns each into a valid GPKG (21 2D
polygons, HTTP 200). Details and the two bugs this exposed: §3c.

**Write (making test DWGs): BROKEN.** `dxf2dwg` and `dwgwrite`
produce corrupt DWGs from DXFs containing HATCH or LWPOLYLINE (the
two entity types surveyor files actually use): HATCH DWGs read back
with `Invalid HATCH.num_paths` and convert to 0 features; LWPOLYLINE
DWGs convert to truncated DXF or hang `dwgread`. Do not rely on
LibreDWG to create DWG fixtures. For future DWG tests, use a DWG
produced by real AutoCAD (or supplied by a surveyor) — or the
LibreDWG sample files in `libredwg/test/test-data/`.

**Version limits (honest):** tested R2000–R2018 read via dwg2dxf.
R2019/R2021+ untested (no sample in the tree; LibreDWG documents
R2018 as newest well-supported). `dwgwrite --as` only supports
r2000 output in practice (r2004–r2021 listed as "planned"). Complex
objects (dynamic blocks, AEC objects, 3D solids) are partially
supported or dropped — the example files lose 2/125 entities and all
3D geometry is excluded from the GPKG by design (2D schema).

## 6. Deploy to Cloud Run (once a GCP project exists)

Build + deploy (from `~/workspace/work/dwg2gpkg-converter/`):

```sh
gcloud builds submit --tag gcr.io/<PROJECT_ID>/dwg2gpkg-converter
gcloud run deploy dwg2gpkg-converter \
  --image gcr.io/<PROJECT_ID>/dwg2gpkg-converter \
  --platform managed --region asia-southeast1 \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 300 --max-instances 5
```

Notes: the image listens on `$PORT` (default 8080); it is stateless
(temp files only under `/tmp`); no credentials are baked in. Free-tier
headroom (Nov 2026 pricing): 2M requests + 180,000 vCPU-seconds/month.
Caveat: Cloud Run caps a single HTTP/1 request/response at 32 MiB —
the service enforces the same 32 MiB upload limit. Larger DWG files
need chunked/resumable upload (not implemented in this prototype).

## 7. Open items / honest gaps

1. **Docker image NOT built locally.** No Docker daemon exists in
   this VM (`docker: command not found`), so the Dockerfile was not
   executed. Its build steps are exactly the commands run and verified
   on this VM (same LibreDWG 0.13.3 version, same `autoreconf` /
   `configure` / `make install` sequence, plus `texinfo` for the doc
   build). First `gcloud builds submit` is the real validation.
2. **Attribute mapping is the real product problem, not format
   conversion.** A surveyor DXF has no attribute table; NIB/NAMA only
   exist as TEXT labels. The service's label join handles the common
   case (one TEXT inside each parcel). Multi-line labels, labels
   outside the parcel, and NAMA/NIK as separate TEXT layers need a
   mapping UI pass per file — not built here.
3. **CRS is assumed, not detected.** DXF/DWG have no CRS; the service
   defaults to EPSG:23834 (TM-3 48.2, Cianjur). Files drawn in another
   TM-3 zone or in local coordinates will convert with wrong
   coordinates unless the user picks the right EPSG.
4. **No DWF support.** No open-source DWF reader exists; DWF was
   dropped from v1 scope (see parent discussion 2026-11-26).
5. **32 MiB upload cap** (Cloud Run request limit).
6. **Silent drops.** With `-skipfailures`, entities the DXF driver
   cannot parse are dropped without a per-feature report; the only
   signal is the output feature count vs the probe count. A future
   version should return both counts in the response.
7. **LibreDWG write path is broken** for HATCH/LWPOLYLINE (§3c) —
   no DWG fixtures can be made from the reference data; future DWG
   regression tests need a real AutoCAD-produced DWG (e.g. a surveyor's
   actual file) or the bundled samples.

## 8. Follow-up — sandbox clean-room test (2026-11-26, SANDBOX-TEST.md)

A fresh E2B sandbox install following the Dockerfile exactly exposed
that the Dockerfile's `debian:bookworm-slim` base ships **GDAL 3.6.2**,
which breaks the service: `/probe` fails entirely (`ogrinfo -json`
needs GDAL ≥ 3.7) and the label JOIN fails (`no such column: l.Text` —
NIB mapping from TEXT labels dead). The Dockerfile base was changed
to `ubuntu:24.04` (GDAL 3.8.4, the version tested on this VM) and
`make` was capped at `-j2`. Render build is the final validation.
