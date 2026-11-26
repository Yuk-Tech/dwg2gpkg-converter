"""DWG/DXF -> GPKG converter service (prototype).

Backend engine for the Yuk Generate PBT plugin: surveyors upload a CAD
drawing (DWG/DXF) and get back a GeoPackage in the plugin schema
(layer PBT_<number>, MULTIPOLYGON, EPSG:23834 by default).

Pipeline:
  DXF -> ogr2ogr (SQLITE dialect, field mapping + computed LUAS/M2) -> GPKG
  DWG -> LibreDWG dwg2dxf -> DXF -> same as above

Surveyor mode (mode=surveyor) handles real surveyor drawings, which draw
parcel boundaries as an OPEN LINE NETWORK (shared edges) with NIB / NAMA /
LUAS / SU / Nomor Hak as TEXT labels on their own layers:
  parcel network -> ST_BuildArea(ST_Collect(...)) -> faces -> label joins
  SHM parcels (closed-flag polylines, read as LINESTRING by GDAL) -> rings
  closed by hand; dividers (jalan/gang/sungai/selokan) stay OPEN LINES in
  a second `dividers` layer — they must never be closed (Derana, 2026-11-26).
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

app = FastAPI(title="dwg2gpkg-converter", version="0.2.0")

MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MiB (Cloud Run HTTP/1 request limit)

# Plugin schema: target field -> SQL expression template.
# {src} is replaced with a quoted source column, {lit} with a quoted literal.
TARGET_FIELDS = [
    "080201",  # NIB
    "NAMA",
    "LUAS",    # pre-formatted 'L=177M2'
    "M2",      # clean area, integer metres
    "A",       # NIK
    "080202",  # No SU (Nomor Surat Ukur) — wajib ada di PBT
    "080203",  # Nomor Hak / SHM marker
    "PBTK",
    "TGLK",
    "PBT",
    "B",
    "KET",     # divider words like JALAN / IRIGASI
]

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


def _q_ident(name: str) -> str:
    """Quote an SQL identifier after a strict whitelist check."""
    if not _IDENT.match(name):
        raise HTTPException(status_code=400, detail=f"invalid identifier: {name!r}")
    return f'"{name}"'


def _q_lit(value: str) -> str:
    """Quote an SQL string literal (escape single quotes)."""
    return "'" + str(value).replace("'", "''") + "'"


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


def _build_convert_sql(
    source_layer: str,
    target_layer: str,
    field_map: dict[str, str],
    literals: dict[str, str],
    label_layer: str | None,
    label_field: str | None,
) -> str:
    """Build the SQLITE-dialect SELECT that maps CAD -> plugin schema.

    field_map: target field -> source column name (or "" for NULL).
    literals:  target field -> literal string value.
    label_layer/label_field: optional TEXT-label join. CAD files have a
    single "entities" layer; labels are TEXT entities on their own DXF
    layer, so label_layer is the *value* of the Layer attribute (e.g.
    "NIB") and label_field the attribute holding the text (default
    "Text"). Label points are spatially joined into parcels
    (ST_Within) and their text becomes the NIB (080201).
    """
    selects: list[str] = []
    for target in TARGET_FIELDS:
        if target in literals:
            selects.append(f"{_q_lit(literals[target])} AS {_q_ident(target)}")
        elif target == "LUAS":
            selects.append(
                "('L=' || CAST(CAST(ST_Area(p._poly) AS INTEGER) AS TEXT) || 'M2')"
                f" AS {_q_ident(target)}"
            )
        elif target == "M2":
            selects.append(f"CAST(CAST(ST_Area(p._poly) AS INTEGER) AS TEXT) AS {_q_ident(target)}")
        elif target == "080201" and label_layer and label_field:
            selects.append(f"l.{_q_ident(label_field)} AS {_q_ident(target)}")
        elif target in field_map and field_map[target]:
            selects.append(f"p.{_q_ident(field_map[target])} AS {_q_ident(target)}")
        else:
            selects.append(f"CAST(NULL AS TEXT) AS {_q_ident(target)}")

    # GDAL reads closed LWPOLYLINE parcels as LINESTRING (not POLYGON),
    # so polygonise linework with ST_BuildArea; real polygons pass through.
    # A LINESTRING that is not closed yields an empty polygon (NULL area).
    parcel_filter = "ST_GeometryType(geometry) IN ('POLYGON','MULTIPOLYGON','LINESTRING','MULTILINESTRING')"
    if label_layer:
        parcel_filter += f" AND Layer <> {_q_lit(label_layer)}"
    poly_expr = (
        "CASE WHEN ST_GeometryType(geometry) IN ('LINESTRING','MULTILINESTRING') "
        "THEN ST_BuildArea(geometry) ELSE geometry END"
    )
    from_clause = (
        f"(SELECT *, {poly_expr} AS _poly FROM {_q_ident(source_layer)} "
        f"WHERE {parcel_filter}) p"
    )
    if label_layer and label_field:
        from_clause += (
            f" LEFT JOIN (SELECT * FROM {_q_ident(source_layer)} "
            f"WHERE Layer = {_q_lit(label_layer)} "
            "AND ST_GeometryType(geometry) LIKE 'POINT%') l"
            " ON ST_Within(l.geometry, p._poly)"
        )
    # Outer filter is REQUIRED, not cosmetic: without it, rows whose
    # _poly is an empty polygon (unclosed LINESTRING) or a 3D polygon
    # (POLYGON Z from dwg2dxf output) are written, and ogr2ogr then
    # writes *every* feature with a NULL geometry into the GPKG
    # (observed on LibreDWG example_2000.dwg: 25 features, all NULL).
    # With it, only real 2D polygons are written (21/21 with geometry).
    return (
        f"SELECT {', '.join(selects)}, p._poly AS geometry FROM {from_clause} "
        "WHERE ST_GeometryType(p._poly) IN ('POLYGON','MULTIPOLYGON') "
        "AND NOT ST_IsEmpty(p._poly)"
    )


# --- Surveyor mode -------------------------------------------------------
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


def _convert_surveyor(
    dxf_path: Path,
    source_layer: str,
    target_layer: str,
    target_epsg: int,
    literals: dict[str, str],
    parcel_layer: str,
    shm_layer: str,
    workdir: Path,
) -> Path:
    """Surveyor-mode conversion via the surveyor_convert.py subprocess."""
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
            detail=f"surveyor conversion failed: {(res.stderr or res.stdout).strip()[:500]}",
        )
    return out_path


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    source_layer: str = Form("entities"),
    target_layer: str = Form("PBT_2091"),
    target_epsg: int = Form(23834),
    field_map: str = Form("{}"),
    pbt: str = Form(""),
    pbtk: str = Form(""),
    tglk: str = Form(""),
    label_layer: str = Form(""),
    label_field: str = Form("Text"),
    mode: str = Form("surveyor"),
    parcel_layer: str = Form("020100"),
    shm_layer: str = Form("lb_lai"),
) -> FileResponse:
    """Convert an uploaded DWG/DXF to a GPKG in the plugin schema.

    mode=generic: per-feature ST_BuildArea (closed polygons, single
    label layer). mode=surveyor (default): polygonise the parcel
    line network + join the surveyor's label layers + SHM + line
    dividers (see _convert_surveyor).
    """
    if mode not in ("generic", "surveyor"):
        raise HTTPException(status_code=400, detail="mode must be 'generic' or 'surveyor'")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".dwg", ".dxf"):
        raise HTTPException(status_code=400, detail="only .dwg and .dxf uploads are supported")
    try:
        mapping = json.loads(field_map or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="field_map is not valid JSON") from exc
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400, detail="field_map must be a JSON object")
    for target, src in mapping.items():
        if target not in TARGET_FIELDS:
            raise HTTPException(status_code=400, detail=f"unknown target field: {target!r}")
        if src:
            _q_ident(src)  # whitelist check

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
        if mode == "surveyor":
            out_path = _convert_surveyor(
                dxf, source_layer, target_layer, target_epsg, literals,
                parcel_layer, shm_layer, workdir,
            )
            out_ok = out_path.exists() and _run(
                ["ogrinfo", "-so", str(out_path), target_layer]
            ).returncode == 0
            if not out_ok:
                raise HTTPException(
                    status_code=422, detail="surveyor conversion produced no readable output",
                )
        else:
            sql = _build_convert_sql(
                source_layer,
                target_layer,
                mapping,
                literals,
                label_layer or None,
                label_field or None,
            )
            out_path = workdir / f"{target_layer}.gpkg"
            cmd = [
                "ogr2ogr", "-f", "GPKG", str(out_path), str(dxf),
                "-nln", target_layer, "-nlt", "MULTIPOLYGON",
                "-s_srs", f"EPSG:{int(target_epsg)}", "-t_srs", f"EPSG:{int(target_epsg)}",
                # -skipfailures: real-world CAD files contain entities the DXF
                # driver cannot parse (e.g. VIEWPORTs, 3D solids from dwg2dxf
                # output). Without it, one bad entity aborts the whole
                # translation; with it, the parseable parcels are still
                # written (see TEST-REPORT.md section 3d).
                "-dialect", "SQLITE", "-skipfailures", "-sql", sql,
            ]
            res = _run(cmd)
            # ogr2ogr can exit non-zero yet still write a usable GPKG (e.g.
            # when the DXF driver hits an unparseable entity mid-file, it
            # prints "Terminating translation prematurely" but -skipfailures
            # keeps the features written so far). Accept the output when the
            # target layer is actually present and readable.
            out_ok = out_path.exists() and _run(
                ["ogrinfo", "-so", str(out_path), target_layer]
            ).returncode == 0
            if not out_ok:
                raise HTTPException(
                    status_code=422,
                    detail=f"ogr2ogr failed (rc={res.returncode}): {(res.stderr or res.stdout).strip()[:800]}",
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
