# SANDBOX TEST — dwg2gpkg-converter (clean-room, fresh E2B sandbox)

Date: 2026-11-26. Sandbox: `iptwjddp8spzkylmjjjlq`, template `toon-render-4cpu`
(`cey6mrv6a1dhkzl0urm1`), 4 vCPU / 4096 MB RAM, Debian 12.
Created 07:50:44 UTC, expires 08:50:45 UTC (1-hour max lifetime).
Purpose: simulate Render's fresh build as closely as possible, WITHOUT Docker.
Nothing deployed, nothing spent. Service code uploaded from
`~/workspace/work/dwg2gpkg-converter/` (app/ + testdata/ + setup script).

All times UTC below (WIB = UTC+7).

## 1. Timeline

| Time | Event |
|---|---|
| 07:50:44 | Sandbox created (4 vCPU / 4 GB — base template's 473 MB is too thin for a LibreDWG build) |
| 07:51:17 | Upload done: 24 files, 7.2 MB, ~9 s (SDK files API) |
| 07:51:25 | `setup.sh` started (nohup, pid 1610), mirroring the Dockerfile exactly |
| 07:51:33 | `apt-get update` done |
| 07:51:50 | apt install done (git autoconf automake libtool pkg-config gcc make texinfo ca-certificates gdal-bin python3 python3-pip) |
| 07:51:53 | `git clone --depth 1 --branch 0.13.3` LibreDWG done (3 s) |
| 07:52:13 | autoreconf + configure done, `make -j4` started |
| ~07:54 | **Command channel wedged**: sandbox guest agent (envd) stopped responding; 4 consecutive SDK timeouts (see §4) |
| 08:01:51 | `pause` via REST API: 204 |
| 08:01:54 | `resume` via REST API: 409 Conflict (first attempt tracebacked) |
| 08:02:06 | `Sandbox.connect` → `GuestUnresponsiveException`: snapshot was taken while the guest agent was not responding; resume only with `on_resume="reboot"` |
| 08:02:33 | Reboot-resume (`run_sandbox_reboot.py`, `on_resume="reboot"`): channel alive again in ~5 s. Disk intact (`/home/user/conv/*` all present); build process dead; make had finished 17 `src/*.lo` files |
| 08:03:01 | `make -j2` restarted (incremental — already-built objects reused) |
| 08:03:23 | `pip install -r app/requirements.txt` done (fastapi 0.115.6, uvicorn 0.34.0, python-multipart 0.0.20 — exactly as pinned) |
| 08:09:15 | `BUILD_DONE`: MAKE_RC=0, INSTALL_RC=0, `dwg2dxf 0.13.3` |
| 08:09 | uvicorn started on :8080 (one more SDK timeout during this call, but the nohup'd server started anyway — see §4) |
| 08:10:46 | First live tests (below) |

### Build timings

- apt update + install: ~25 s. Clone: 3 s. autoreconf + configure: ~20 s.
- LibreDWG `make`: first run `-j4` 07:52:13→~07:54 (~2 min, interrupted);
  second run `-j2` 08:03:01→~08:09 (~6 min, incremental). **Total make wall
  time ≈ 8 min**, consistent with the ~9 min measured on the local VM.
- `make install` + `ldconfig`: < 1 min (inside the 08:03–08:09 window).

## 2. Live test results (service at http://127.0.0.1:8080)

| Test | Result |
|---|---|
| `GET /health` | **PASS** — `{"status":"ok","tools":{"ogr2ogr":true,"ogrinfo":true,"dwg2dxf":true}}` |
| `GET /` (form) | **PASS** — HTTP 200 |
| `POST /probe` on `PBT_mini_surveyor.dxf` | **FAIL** — HTTP 422 `{"detail":"ogrinfo failed: FAILURE: Unknown option name '-json'"}` |
| `POST /probe` on `example_2000.dwg` | **FAIL** — same `-json` error |
| `POST /convert` DXF + label join (`label_layer=NIB`, `label_field=Text`) | **FAIL** — HTTP 422 `ogr2ogr failed (rc=1): ... no such column: l.Text` |
| `POST /convert` DXF, no label join | **PASS** — HTTP 200, 98,304 B; layer `PBT_2091`, Multi Polygon, **6 features**, EPSG:23834; LUAS/M2 correct (fid 1 `L=177M2` / `177`, fid 2 `1968`, fid 3 `697`, fid 4 `953`) |
| `POST /convert` DWG R2000 (`example_2000.dwg`) | **PASS** — HTTP 200, 98,304 B; layer `PBT_2000`, Multi Polygon, **21 features, 0 NULL geometries**, EPSG:23834 (DGN95 / Indonesia TM-3 zone 48.2), all 11 schema fields present |
| `POST /convert` full `PBT_2091.dxf`, no label join | **PASS** — HTTP 200, 143,360 B; **124 features**; per-feature M2 **identical** to the VM conversion and to the reference GPKG's M2 column (0 differing features — see §3) |
| NIB values in no-label output | All NULL, as expected without the label join (the join is what fills 080201) |

Downloaded outputs (local copies):
`~/workspace/work/dwg2gpkg-converter/sandbox-test-outputs/`
`dwg2000_sandbox.gpkg`, `PBT_mini_surveyor_sandbox.gpkg`, `PBT_2091_sandbox.gpkg`.

## 3. Root cause of the two failures: GDAL version, not the code

The sandbox (Debian 12 bookworm, same base as the Dockerfile) has
**GDAL 3.6.2** (`ogrinfo --version` → `GDAL 3.6.2, released 2023/01/02`).
The service was developed and tested on the local VM's Ubuntu 24.04,
which has **GDAL 3.8.4**. Both failures are version behaviour, proven by
bisection in the sandbox:

### 3a. `/probe`: `ogrinfo -json` does not exist before GDAL 3.7

`main.py` `_ogrinfo_json` / `_ogrinfo_sample` call `ogrinfo -json ...`.
GDAL 3.6.2 rejects it: `FAILURE: Unknown option name '-json'`.
The `-json` output option was added in GDAL 3.7. Every `/probe` call fails
on GDAL 3.6.2, for DXF and DWG alike.

### 3b. `/convert` with label join: `no such column: l.Text` (JOIN only)

The service's label-join SQL is a `LEFT JOIN` of a subquery
(`... FROM (...) p LEFT JOIN (SELECT * FROM "entities" WHERE Layer='NIB'
AND ST_GeometryType(geometry) LIKE 'POINT%') l ON ST_Within(l.geometry,
p._poly) ...`). Bisected with minimal SQLITE-dialect queries
(`dbg2_sql.py`, all against `PBT_mini_surveyor.dxf` on GDAL 3.6.2):

| Case | Query | Result |
|---|---|---|
| A | `SELECT "Text" FROM "entities" WHERE Layer='NIB'` | rc=0 |
| B | `SELECT l."Text" FROM (SELECT * FROM "entities" WHERE Layer='NIB') l` | rc=0 |
| C | B + `AND ST_GeometryType(geometry) LIKE 'POINT%'` | rc=0 |
| D | `... FROM "entities" p LEFT JOIN (SELECT * ...) l ON 1=1` | **rc=1, `no such column: l.Text`** |
| E | `SELECT * FROM (SELECT * ...) l LIMIT 1` | rc=0 |

The `Text` field exists (`ogrinfo -so` lists it). Only the JOIN form
breaks: GDAL 3.6.2's SQLite dialect cannot resolve columns of a
subquery placed on the right side of a JOIN. On GDAL 3.8.4 the same
query works (TEST-REPORT: surveyor DXF → 6/6, NIBs exact).

**Impact on Render:** the Dockerfile builds on `debian:bookworm-slim`,
so the Render image will also have GDAL 3.6.2. That means, as shipped,
**`/probe` is fully broken and NIB mapping from TEXT labels (the core
surveyor workflow) is broken** on Render. Conversion without the label
join, and DWG reading, work fine.

**Fix options (not applied here — test only):**
1. Change the Dockerfile base to an image with GDAL ≥ 3.7, e.g.
   `debian:trixie-slim` (GDAL 3.10) or `ubuntu:24.04` (GDAL 3.8.4,
   the version the service was tested against). Re-test `/probe` +
   label join after the change.
2. Or rewrite the code to avoid the two constructs: probe without
   `-json` (parse classic `ogrinfo` output, or use Python `osgeo`),
   and replace the label `LEFT JOIN` with a scalar subquery in the
   SELECT list (e.g. `(SELECT "Text" FROM "entities" WHERE Layer='NIB'
   AND ST_Within(geometry, p._poly) LIMIT 1) AS "080201"`) — untested
   on 3.6.2, needs verification.

### 3c. The 50,211 vs 50,152 question — NOT a regression

The sandbox PBT_2091 conversion sums to M2 = 50,152, while TEST-REPORT
quotes 50,211 m². Checked: the reference GPKG's own M2 column sums to
**50,152** (124 features, 0 diffs vs the service output). The 50,211
figure in TEST-REPORT is the **geometric float area sum** (ST_Area),
not the truncated-integer M2 column. Sandbox vs VM conversion:
**0 differing features, identical per-feature M2.** No area regression.

## 4. Sandbox / E2B incidents (not converter bugs)

- **Guest agent wedge (~07:54–08:02).** During `make -j4`, the envd
  agent stopped responding: 4 consecutive SDK timeouts (at `_start`
  and during `wait`). A `pause` then captured a snapshot flagged
  `envd_unresponsive_at_pause`; plain resume 409'd and `connect`
  raised `GuestUnresponsiveException`. Recovery: `on_resume="reboot"`
  (cold-boot from disk) — channel alive in ~5 s, disk intact, running
  processes lost. Possible cause: `make -j4` memory pressure on 4 GB
  (4 parallel gcc jobs on LibreDWG's large translation units), but
  **not proven** — memory was free (3.7 GB available) right after the
  reboot, and the channel also timed out once more at 08:10 during the
  uvicorn start with low CPU load. E2B control-plane flakiness cannot
  be ruled out.
- **Mitigation applied:** build restarted with `make -j2`; completed
  without incident. The Dockerfile uses `make -j"$(nproc)"` — on a
  4 GB box `-j4` is risky; recommend capping at `-j2` (Render's
  builder likely has more RAM, but this was not verified).
- **One invalid repro (mine, discarded):** my first direct `ogr2ogr`
  reproduction at 08:11:46 failed with `near ")": syntax error`
  because single quotes inside my single-quoted `-sql` argument were
  stripped by bash. Re-ran properly via `dbg_sql.py` (subprocess list
  args, no shell) — that is the result quoted in §3b.

## 5. Dockerfile differences found

- **Packages: none.** The apt list in `setup.sh` matched the
  Dockerfile exactly; everything installed, including `texinfo`
  (required — see the Dockerfile comment).
- **GDAL version: the difference that matters.** Dockerfile base
  `debian:bookworm-slim` → GDAL 3.6.2; the VM test used Ubuntu 24.04 →
  GDAL 3.8.4. See §3.
- **Make parallelism:** Dockerfile `make -j"$(nproc)"`; this test used
  `-j4` first (wedged, §4), then `-j2` (fine).
- `sqlite3` CLI is **not** installed in the sandbox image (nor in the
  Dockerfile) — per-feature comparison was done with Python's
  `sqlite3` module instead. Not a service issue.

## 6. Not tested

- DWG R2004–R2018 in this sandbox (only R2000 tested here; the VM
  TEST-REPORT covered R2000–R2018 on GDAL 3.8.4), R2019+ (no sample).
- `/convert` with `field_map` / `pbt` / `pbtk` / `tglk` literals.
- Uploads near/above the 32 MiB cap; concurrent requests.
- The Docker image build itself (no Docker daemon here — still
  Render's job to validate).
- Whether Render's builder has enough RAM for `make -j$(nproc)`.

## 7. Verdict

The engine **works** in a clean environment for the core conversion
paths (DXF→GPKG 124/124 identical to the VM run; DWG R2000→GPKG 21/21,
0 NULL geometries), and the Dockerfile's install steps reproduce
exactly. But **the image as written will ship GDAL 3.6.2, on which two
user-facing features fail: `/probe` (all files) and NIB-from-label
mapping in `/convert`**. Fix the base image (or the two SQL/CLI
constructs) and re-test those two paths before calling the Render
deploy done. The sandbox (`iptwjddp8spzkylmjjjlq`) was left to expire
at 08:50:45 UTC; outputs are saved locally under
`sandbox-test-outputs/`.
