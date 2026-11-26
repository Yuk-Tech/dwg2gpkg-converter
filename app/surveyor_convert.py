"""Surveyor-mode conversion CLI (standalone process).

Run by the service (app/main.py) as a SUBPROCESS, never in-process:
GDAL's Python bindings segfaulted inside the uvicorn event loop
(2026-11-26), so the native osgeo work lives in this plain process.

Usage:
  python3 surveyor_convert.py DXF SOURCE_LAYER TARGET_LAYER EPSG \
      PARCEL_LAYER SHM_LAYER LITERALS_JSON OUT_GPKG

Pipeline (verified on Tes Convert.dxf, 2026-11-26: 24/24 parcels):
  1. Polygonise the parcel layer's line network with
     ST_BuildArea(ST_Collect(...)) (ogr2ogr, -explodecollections).
  2. SHM polygons: closed-flag LWPOLYLINEs arrive as LINESTRING from
     GDAL's DXF driver -> rings closed by hand.
  3. TEXT label points (NIB / NAMA / LUAS / SU / Nomor Hak) joined
     into parcels with ST_Within semantics (ogr Within).
  4. Divider lines (jalan/gang/sungai/selokan) stay OPEN LINES in a
     second `dividers` layer — they must never be closed (Derana).
Prints a JSON summary to stdout on success; exits non-zero on failure.
"""

import json
import subprocess
import sys
from pathlib import Path

from osgeo import ogr, osr

# Layer codes are LAYER NAMES in the surveyor's DXF (Derana, 2026-11-26).
CODE_KET = {
    "040100": "JALAN",
    "040600": "GANG",
    "03011": "SUNGAI",
    "030100": "SUNGAI",
    "030300": "SELOKAN",
}

# target field -> TEXT layer carrying its labels.
SURVEYOR_LABELS = {
    "080201": "080201",  # NIB
    "NAMA": "tn_nama",
    "LUAS": "luas",
    "080202": "080202",  # No SU (wajib di PBT)
    "080203": "#080203",  # Nomor Hak (M.00xxx)
}

TARGET_FIELDS = [
    "080201", "NAMA", "LUAS", "M2", "A", "080202", "080203",
    "PBTK", "TGLK", "PBT", "B", "KET",
]


def _as_polygon(geom):
    """Return a single POLYGON from a POLYGON or a 1-part MULTIPOLYGON.

    NOTE: the part must be CLONED, not returned by reference — the parent
    multipolygon here is a temporary (g.Clone()), so a borrowed reference
    to its child dangles once the parent is freed (segfault, 2026-11-26).
    """
    if geom is None:
        return None
    if geom.GetGeometryName() == "MULTIPOLYGON" and geom.GetGeometryCount() > 0:
        return geom.GetGeometryRef(0).Clone()
    return geom.Clone()


def _close_ring_to_polygon(line):
    """Close an open LINESTRING ring (append first vertex) -> POLYGON."""
    ring = ogr.Geometry(ogr.wkbLinearRing)
    n = line.GetPointCount()
    for i in range(n):
        ring.AddPoint_2D(*line.GetPoint_2D(i))
    ring.AddPoint_2D(*line.GetPoint_2D(0))
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def main() -> int:
    (dxf_path, source_layer, target_layer, epsg,
     parcel_layer, shm_layer, literals_json, out_path) = sys.argv[1:9]
    epsg = int(epsg)
    literals = json.loads(literals_json or "{}")
    dxf_path = Path(dxf_path)
    out_path = Path(out_path)
    workdir = out_path.parent
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)

    # 1. Polygonise the parcel network.
    faces_path = workdir / "faces.gpkg"
    res = subprocess.run(
        [
            "ogr2ogr", "-f", "GPKG", str(faces_path), str(dxf_path),
            "-nln", "faces", "-nlt", "MULTIPOLYGON", "-explodecollections",
            "-s_srs", f"EPSG:{epsg}", "-t_srs", f"EPSG:{epsg}",
            "-dialect", "SQLITE", "-skipfailures", "-sql",
            "SELECT ST_BuildArea(ST_Collect(geometry)) AS geometry "
            f"FROM \"{source_layer}\" WHERE Layer = '{parcel_layer}'",
        ],
        capture_output=True, text=True, timeout=300,
    )
    if res.returncode != 0 or not faces_path.exists():
        print(f"polygonise failed: {(res.stderr or res.stdout).strip()[:500]}",
              file=sys.stderr)
        return 1
    faces = []
    fds = ogr.Open(str(faces_path))
    if fds is not None:
        flyr = fds.GetLayer("faces")
        for feat in flyr:
            g = feat.GetGeometryRef()
            if g is not None and not g.IsEmpty():
                faces.append(_as_polygon(g.Clone()))
    fds = None
    if not faces:
        print(f"no closed faces found in parcel layer {parcel_layer!r}",
              file=sys.stderr)
        return 1

    # 2. SHM polygons, divider lines, label points from the DXF.
    shm_polys: list = []
    div_lines: list = []
    labels: dict = {}
    ds = ogr.Open(str(dxf_path))
    if ds is None:
        print("cannot open DXF", file=sys.stderr)
        return 1
    lyr = ds.GetLayer(source_layer)
    for feat in lyr:
        geom = feat.GetGeometryRef()
        layer = feat.GetField("Layer")
        if geom is None or layer is None:
            continue
        gt = geom.GetGeometryName()
        if layer == shm_layer:
            if gt in ("POLYGON", "MULTIPOLYGON"):
                shm_polys.append(_as_polygon(geom.Clone()))
            elif gt == "LINESTRING":
                shm_polys.append(_close_ring_to_polygon(geom))
        elif layer in CODE_KET:
            if gt in ("POLYGON", "MULTIPOLYGON"):
                div_lines.append((layer, _as_polygon(geom.Clone()).Boundary()))
            elif gt.startswith("LINESTRING"):
                div_lines.append((layer, geom.Clone()))
        elif gt.startswith("POINT"):
            labels.setdefault(layer, []).append((feat.GetField("Text"), geom.Clone()))
    ds = None

    def _labels_within(poly, layer_name: str) -> list:
        return [t for (t, g) in labels.get(layer_name, []) if g.Within(poly)]

    # 3. Write the plugin-schema GPKG.
    if out_path.exists():
        out_path.unlink()
    odrv = ogr.GetDriverByName("GPKG")
    ods = odrv.CreateDataSource(str(out_path))
    olyr = ods.CreateLayer(target_layer, srs, ogr.wkbMultiPolygon)
    for name in TARGET_FIELDS:
        olyr.CreateField(ogr.FieldDefn(name, ogr.OFTString))

    def _add_parcel(poly):
        mp = ogr.Geometry(ogr.wkbMultiPolygon)
        mp.AddGeometry(poly.Clone())
        area = poly.GetArea()
        found: dict = {}
        for target, lyr_name in SURVEYOR_LABELS.items():
            hits = _labels_within(poly, lyr_name)
            found[target] = hits[0] if hits else None
        f = ogr.Feature(olyr.GetLayerDefn())
        f.SetGeometry(mp)
        f.SetField("080201", found["080201"])
        f.SetField("NAMA", found["NAMA"])
        f.SetField("LUAS", found["LUAS"] or f"L={int(round(area))}M2")
        f.SetField("M2", str(int(round(area))))
        f.SetField("080202", found["080202"])
        f.SetField("080203", found["080203"])
        for key in ("PBT", "PBTK", "TGLK"):
            if key in literals:
                f.SetField(key, literals[key])
        olyr.CreateFeature(f)

    for face in faces:
        _add_parcel(face)
    for sp in shm_polys:
        _add_parcel(sp)

    dlyr = ods.CreateLayer("dividers", srs, ogr.wkbLineString)
    dlyr.CreateField(ogr.FieldDefn("KET", ogr.OFTString))
    dlyr.CreateField(ogr.FieldDefn("Layer", ogr.OFTString))
    for layer, geom in div_lines:
        f = ogr.Feature(dlyr.GetLayerDefn())
        f.SetGeometry(geom.Clone())
        f.SetField("KET", CODE_KET.get(layer))
        f.SetField("Layer", layer)
        dlyr.CreateFeature(f)
    ods = None
    print(json.dumps({
        "parcels": len(faces) + len(shm_polys),
        "faces": len(faces),
        "shm": len(shm_polys),
        "dividers": len(div_lines),
        "output": str(out_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
