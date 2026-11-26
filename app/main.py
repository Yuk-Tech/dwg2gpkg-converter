"""DWG/DXF -> GPKG converter service (prototype).

Backend engine for the Yuk Generate PBT plugin: surveyors upload a CAD
drawing (DWG/DXF) and get back a GeoPackage in the plugin schema
(layer PBT_<number>, MULTIPOLYGON, EPSG:23834 by default).

Single pipeline (one mode only — Derana, 2026-11-26: the proven
surveyor path is the only path; the old generic mode was removed in v0.3.0,
its code lives in git history if ever needed):
  DWG -> LibreDWG dwg2dxf -> DXF -> surveyor conversion -> GPKG
  DXF -> surveyor conversion -> GPKG
Real surveyor drawings draw parcel boundaries as an OPEN LINE NETWORK
(shared edges) with NIB / NAMA / LUAS / SU / Nomor Hak as TEXT labels on
their own layers:
  parcel network -> ST_BuildArea(ST_Collect(...)) -> faces -> label joins
  SHM parcels (closed-flag polylines, read as LINESTRING by GDAL) -> rings
  closed by hand; dividers (jalan/gang/sungai/selokan) stay OPEN LINES in
  a second `dividers` layer — they must never be closed (Derana, 2026-11-26).
  ALL textual content becomes attributes (Derana, 2026-11-26): known labels
  into fixed fields, any other TEXT layer into a field named after the
  layer, TEXTs outside every parcel preserved in a `labels` layer.
Verified on Tes Convert.dxf (2026-11-26): 21 faces + 3 SHM = 24/24 parcels,
areas matching the surveyor's luas texts within 1 m2.

Stateless: all temp files live under /tmp (tempfile.TemporaryDirectory).
No credentials, no database, no persistent state.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI(title="dwg2gpkg-converter", version="0.3.5")

MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MiB (Cloud Run HTTP/1 request limit)

# Coordinate systems by plain name (Derana, 2026-11-26: no EPSG codes in
# the form — the user picks a name, the service resolves it to EPSG).
# Codes verified against PROJ (2026-11-26): TM-3 is DGN95, zones 46.2
# (EPSG 23830) through 54.1 (EPSG 23845); EPSG has no TM-3 46.1 or 54.2
# (outside Indonesia's landmass). UTM WGS84: 46N-54N (32646-32654),
# 46S-54S (32746-32754). ID74 UTM: 46N-52N (23846-23852; 53N deprecated).
# key -> (group, plain label, EPSG code)
CRS_OPTIONS: dict[str, tuple[str, str, int]] = {}
for _zone, _epsg in [
    ("46.2", 23830), ("47.1", 23831), ("47.2", 23832), ("48.1", 23833),
    ("48.2", 23834), ("49.1", 23835), ("49.2", 23836), ("50.1", 23837),
    ("50.2", 23838), ("51.1", 23839), ("51.2", 23840), ("52.1", 23841),
    ("52.2", 23842), ("53.1", 23843), ("53.2", 23844), ("54.1", 23845),
]:
    CRS_OPTIONS[f"tm3-{_zone}"] = ("TM-3 (paling umum untuk ukur tanah)", f"TM-3 zona {_zone}", _epsg)
for _z in range(46, 55):
    CRS_OPTIONS[f"utm-{_z}n"] = ("UTM (WGS84)", f"UTM zona {_z} Utara", 32600 + _z)
    CRS_OPTIONS[f"utm-{_z}s"] = ("UTM (WGS84)", f"UTM zona {_z} Selatan", 32700 + _z)
for _z in range(46, 53):
    CRS_OPTIONS[f"id74-{_z}n"] = ("UTM (datum lama ID74)", f"UTM zona {_z} Utara", 23800 + _z)
CRS_OPTIONS["wgs84"] = ("Lainnya", "Lintang/bujur (WGS84)", 4326)

DEFAULT_CRS = "tm3-48.2"  # TM-3 zona 48.2 — Cianjur dan sekitarnya

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _q_ident(name: str) -> str:
    """Quote an SQL identifier after a strict whitelist check."""
    if not _IDENT.match(name):
        raise HTTPException(status_code=400, detail=f"invalid identifier: {name!r}")
    return f'"{name}"'


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"command timed out: {cmd[0]}") from exc


def _dwg_to_dxf(dwg_path: Path, dxf_path: Path) -> None:
    """Convert DWG -> DXF with LibreDWG. Raises HTTPException on failure."""
    if shutil.which("dwg2dxf") is None:
        raise HTTPException(status_code=501, detail="dwg2dxf (LibreDWG) not installed on this server")
    res = _run(["dwg2dxf", str(dwg_path), "-o", str(dxf_path)])
    if res.returncode != 0 or not dxf_path.exists():
        raise HTTPException(
            status_code=422,
            detail=f"dwg2dxf failed (rc={res.returncode}): {(res.stderr or res.stdout).strip()[:500]}",
        )


def _to_dxf(upload_path: Path, suffix: str, workdir: Path) -> Path:
    """Normalise an uploaded DWG/DXF to a DXF file. Returns the DXF path."""
    if suffix == ".dxf":
        return upload_path
    if suffix == ".dwg":
        dxf_path = workdir / "input.dxf"
        _dwg_to_dxf(upload_path, dxf_path)
        return dxf_path
    raise HTTPException(status_code=400, detail="only .dwg and .dxf uploads are supported")


def _ogrinfo_json(dxf_path: Path) -> dict:
    res = _run(["ogrinfo", "-json", "-so", str(dxf_path)])
    if res.returncode != 0:
        raise HTTPException(status_code=422, detail=f"ogrinfo failed: {res.stderr.strip()[:500]}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="ogrinfo returned unparseable output") from exc


def _ogrinfo_sample(dxf_path: Path, layer: str, limit: int = 3) -> list[dict]:
    """First `limit` features of a layer (attributes only, no geometry)."""
    res = _run(
        ["ogrinfo", "-json", "-nomd", "-features", str(dxf_path), layer,
         "-sql", f"SELECT * FROM {_q_ident(layer)} LIMIT {int(limit)}"]
    )
    if res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for feat in data.get("layers", [{}])[0].get("features", []):
        props = dict(feat.get("properties", {}))
        props.pop("geometry", None)
        out.append(props)
    return out


@app.get("/crs")
def crs_list() -> dict:
    """Coordinate systems the form offers (plain names; EPSG resolved here)."""
    groups: dict[str, list[dict]] = {}
    for key, (group, label, epsg) in CRS_OPTIONS.items():
        groups.setdefault(group, []).append({"key": key, "label": label, "epsg": epsg})
    return {
        "default": DEFAULT_CRS,
        "groups": [{"name": name, "options": opts} for name, opts in groups.items()],
    }


@app.get("/health")
def health() -> dict:
    tools = {name: shutil.which(name) is not None for name in ("ogr2ogr", "ogrinfo", "dwg2dxf")}
    return {"status": "ok", "tools": tools}


@app.post("/probe")
async def probe(file: UploadFile = File(...)) -> dict:
    """List layers + sample attributes of an uploaded DWG/DXF."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        raise HTTPException(status_code=400, detail="only .dwg and .dxf uploads are supported")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 32 MiB limit")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        up = workdir / f"upload{suffix}"
        up.write_bytes(data)
        dxf = _to_dxf(up, suffix, workdir)
        info = _ogrinfo_json(dxf)
        layers = []
        for lyr in info.get("layers", []):
            name = lyr["name"]
            layers.append(
                {
                    "name": name,
                    "geometry_type": lyr.get("geometryFields", [{}])[0].get("type", "?"),
                    "feature_count": lyr.get("featureCount"),
                    "fields": [f["name"] for f in lyr.get("fields", [])],
                    "sample": _ogrinfo_sample(dxf, name),
                }
            )
        return {"filename": file.filename, "format": suffix.lstrip("."), "layers": layers}


# --- Conversion (single mode) ------------------------------------------
# Real surveyor drawings need native osgeo work (polygonise + label joins),
# but GDAL's Python bindings segfaulted inside the uvicorn event loop
# (2026-11-26), so the conversion runs as a SUBPROCESS in
# app/surveyor_convert.py (plain python3 with system python3-gdal).
# Layer codes are LAYER NAMES in the surveyor's DXF (Derana, 2026-11-26):
# 040100 Jalan, 040600 Gang, 03011/030100 Sungai, 030300 Selokan,
# lb_lai SHM; labels on 080201 (NIB), tn_nama, luas, 080202 (No SU),
# #080203 (M. Nomor Hak). Dividers stay OPEN LINES, never closed.

import sys

_SURVEYOR_SCRIPT = Path(__file__).parent / "surveyor_convert.py"


def _system_python() -> str:
    """System python3 (has osgeo via python3-gdal); the venv python does not."""
    for cand in ("/usr/bin/python3", sys.executable):
        if cand and Path(cand).exists():
            return cand
    return "python3"


def _convert(
    dxf_path: Path,
    source_layer: str,
    target_layer: str,
    target_epsg: int,
    literals: dict[str, str],
    parcel_layer: str,
    shm_layer: str,
    workdir: Path,
) -> Path:
    """Conversion via the surveyor_convert.py subprocess."""
    out_path = workdir / f"{target_layer}.gpkg"
    res = _run(
        [
            _system_python(), str(_SURVEYOR_SCRIPT),
            str(dxf_path), source_layer, target_layer, str(int(target_epsg)),
            parcel_layer, shm_layer, json.dumps(literals), str(out_path),
        ],
        timeout=300,
    )
    if res.returncode != 0 or not out_path.exists():
        raise HTTPException(
            status_code=422,
            detail=f"conversion failed: {(res.stderr or res.stdout).strip()[:500]}",
        )
    return out_path


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    source_layer: str = Form("entities"),
    target_layer: str = Form(""),
    crs: str = Form(DEFAULT_CRS),
    filename: str = Form("PBT_2091"),
    parcel_layer: str = Form("020100"),
    shm_layer: str = Form("lb_lai"),
) -> FileResponse:
    """Convert an uploaded DWG/DXF to a GPKG in the plugin schema.

    One mode only (Derana, 2026-11-26): the proven surveyor path —
    polygonise the parcel line network + join all TEXT labels + SHM +
    open-line dividers (see surveyor_convert.py). The form only asks
    for what varies per job (file, coordinate system, output file
    name); everything else has a fixed default (Derana, 2026-11-26:
    fields that don't need filling were removed from the form).
    PBTK and date are NOT asked — the plugin sets them later (Derana,
    2026-11-26). The output layer and the downloaded file are named
    after `filename`.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        raise HTTPException(status_code=400, detail="only .dwg and .dxf uploads are supported")
    if crs not in CRS_OPTIONS:
        raise HTTPException(status_code=400, detail=f"unknown coordinate system: {crs!r}")
    target_epsg = CRS_OPTIONS[crs][2]
    if not target_layer:
        target_layer = filename or "PBT_2091"

    # No literals from the form: PBT / PBTK / TGLK are set later in the
    # plugin (Derana, 2026-11-26), so the schema fields stay NULL here.
    literals: dict[str, str] = {}

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 32 MiB limit")

    tmp = tempfile.TemporaryDirectory()
    workdir = Path(tmp.name)
    up = workdir / f"upload{suffix}"
    up.write_bytes(data)
    try:
        dxf = _to_dxf(up, suffix, workdir)
        out_path = _convert(
            dxf, source_layer, target_layer, target_epsg, literals,
            parcel_layer, shm_layer, workdir,
        )
        out_ok = out_path.exists() and _run(
            ["ogrinfo", "-so", str(out_path), target_layer]
        ).returncode == 0
        if not out_ok:
            raise HTTPException(
                status_code=422, detail="conversion produced no readable output",
            )
    except HTTPException:
        tmp.cleanup()
        raise

    # FileResponse needs the file to outlive the request: keep the temp dir
    # alive via a background cleanup tied to the response.
    return FileResponse(
        path=str(out_path),
        media_type="application/geopackage+sqlite3",
        filename=f"{target_layer}.gpkg",
        background=BackgroundTask(tmp.cleanup),
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8"))
