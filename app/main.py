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

app = FastAPI(title="dwg2gpkg-converter", version="0.3.0")

MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MiB (Cloud Run HTTP/1 request limit)

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
    target_layer: str = Form("PBT_2091"),
    target_epsg: int = Form(23834),
    pbt: str = Form(""),
    pbtk: str = Form(""),
    tglk: str = Form(""),
    parcel_layer: str = Form("020100"),
    shm_layer: str = Form("lb_lai"),
) -> FileResponse:
    """Convert an uploaded DWG/DXF to a GPKG in the plugin schema.

    One mode only (Derana, 2026-11-26): the proven surveyor path —
    polygonise the parcel line network + join all TEXT labels + SHM +
    open-line dividers (see surveyor_convert.py).
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        raise HTTPException(status_code=400, detail="only .dwg and .dxf uploads are supported")

    literals: dict[str, str] = {}
    if pbt:
        literals["PBT"] = pbt
    if pbtk:
        literals["PBTK"] = pbtk
    if tglk:
        literals["TGLK"] = tglk

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
