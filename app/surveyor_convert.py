"""Surveyor-mode conversion CLI (standalone process).

Run by the service (app/main.py) as a SUBPROCESS, never in-process:
GDAL's Python bindings segfaulted inside the uvicorn event loop
(2026-11-26), so the native osgeo work lives in this plain process.

Usage:
  python3 surveyor_convert.py DXF SOURCE_LAYER TARGET_LAYER EPSG \
      PARCEL_LAYER SHM_LAYER LITERALS_JSON OUT_GPKG

Pipeline (verified on Tes Convert.dxf, 2026-11-26: 24/24 parcels):
  1. Polygonise the parcel layer. Two drawing styles exist and both
     are handled: (a) parcels drawn as their own CLOSED rings — each
     ring becomes its polygon directly; (b) parcels drawn as an OPEN
     shared-edge network — the open lines are polygonised together
     with ST_BuildArea(ST_Collect(...)) on a memory layer. Style (a)
     MUST NOT go through BuildArea: shared edges are traced twice
     (once per adjacent parcel's ring), and BuildArea silently drops
     faces from such duplicated linework (proven 2026-11-26 on
     "tes convert.dxf": 21 rings -> 17 faces, parcels
     01505/01537/01547/01559 lost, 39 of 120 segments drawn 2x).
     Rings are built directly; only open lines go to BuildArea.
  2. SHM polygons: closed-flag LWPOLYLINEs arrive as LINESTRING from
     GDAL's DXF driver -> rings closed by hand.
  3. TEXT label points (NIB / NAMA / LUAS / SU / Nomor Hak) joined
     into parcels with ST_Within semantics (ogr Within).
  4. Divider lines (jalan/gang/sungai/selokan) stay OPEN LINES in a
     second `dividers` layer — they must never be closed (Derana).
  5. ALL textual content becomes attributes (Derana, 2026-11-26):
     every TEXT layer is captured — the known ones into fixed fields
     (NIB/NAMA/LUAS/SU/Nomor Hak), any other TEXT layer into a field
     named after the layer (or into a matching schema field, e.g. a
     layer "A" feeds field A = NIK). TEXTs falling outside every parcel
     are preserved in a `labels` POINT layer (Layer + Text) so nothing
     is ever dropped.
Prints a JSON summary to stdout on success; exits non-zero on failure.
"""

import json
import re
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
    "PBTK", "TGLK", "PBT", "B", "KET", "Layer",
]
# "Layer" = nama layer sumber DXF per persil ("020100" biasa, "lb_lai"
# SHM). Ditambah 2026-11-27 (v0.3.6): identitas SHM datang dari layer
# sumber, BUKAN dari field 080203 — 080203 kosong bila DXF tidak punya
# teks #080203 (kasus BGAS), sementara lb_lai tetap ada. Plugin baca
# Layer='lb_lai' untuk deteksi SHM (080203 tetap fallback GPKG lama).


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


def _field_name(layer: str) -> str:
    """Sanitize a DXF layer name into a GPKG field name."""
    return re.sub(r"[^A-Za-z0-9_]", "_", layer)


def _close_ring_to_polygon(line):
    """LINESTRING ring -> POLYGON. Appends the first vertex only when
    the ring is not already closed (DXF closed-flag polylines store no
    repeated first point; geometrically closed rings do)."""
    ring = ogr.Geometry(ogr.wkbLinearRing)
    n = line.GetPointCount()
    for i in range(n):
        ring.AddPoint_2D(*line.GetPoint_2D(i))
    if line.GetPoint_2D(0) != line.GetPoint_2D(n - 1):
        ring.AddPoint_2D(*line.GetPoint_2D(0))
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _is_closed_ring(line) -> bool:
    """True when a LINESTRING is a geometrically closed ring."""
    n = line.GetPointCount()
    return n >= 4 and line.GetPoint_2D(0) == line.GetPoint_2D(n - 1)


def _buildarea_faces(open_lines):
    """Polygonise open linework with ST_BuildArea on a memory layer.

    Returns a list of POLYGONs (cloned). Same SQL as the old ogr2ogr
    step, but the input is filtered to open lines only.
    """
    mem = ogr.GetDriverByName("Memory").CreateDataSource("")
    ml = mem.CreateLayer("open", None, ogr.wkbLineString)
    for line in open_lines:
        feat = ogr.Feature(ml.GetLayerDefn())
        feat.SetGeometry(line.Clone())
        ml.CreateFeature(feat)
    res = mem.ExecuteSQL(
        "SELECT ST_BuildArea(ST_Collect(geometry)) AS geometry FROM open",
        dialect="SQLITE",
    )
    faces = []
    if res is not None:
        for feat in res:
            g = feat.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            if g.GetGeometryName() == "MULTIPOLYGON":
                for i in range(g.GetGeometryCount()):
                    faces.append(_as_polygon(g.GetGeometryRef(i)))
            else:
                faces.append(_as_polygon(g))
        mem.ReleaseResultSet(res)
    mem = None
    return faces


def main() -> int:
    (dxf_path, source_layer, target_layer, epsg,
     parcel_layer, shm_layer, literals_json, out_path) = sys.argv[1:9]
    epsg = int(epsg)
    literals = json.loads(literals_json or "{}")
    dxf_path = Path(dxf_path)
    out_path = Path(out_path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)

    # 1+2. Read the DXF once: parcel lines, SHM polygons, divider
    # lines, label points.
    parcel_lines: list = []
    parcel_polys: list = []
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
        if layer == parcel_layer:
            if gt in ("POLYGON", "MULTIPOLYGON"):
                if gt == "MULTIPOLYGON":
                    for i in range(geom.GetGeometryCount()):
                        parcel_polys.append(_as_polygon(geom.GetGeometryRef(i)))
                else:
                    parcel_polys.append(_as_polygon(geom.Clone()))
            elif gt == "MULTILINESTRING":
                for i in range(geom.GetGeometryCount()):
                    parcel_lines.append(geom.GetGeometryRef(i).Clone())
            elif gt == "LINESTRING":
                parcel_lines.append(geom.Clone())
        elif layer == shm_layer:
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

    # Polygonise the parcel lines: closed rings become polygons
    # directly; only open lines go through ST_BuildArea (see step 1
    # in the module docstring — BuildArea drops faces when shared
    # edges are traced twice). An invalid ring falls back to the
    # open-lines pool so BuildArea may still salvage it.
    faces: list = list(parcel_polys)
    open_lines: list = []
    for line in parcel_lines:
        if _is_closed_ring(line):
            poly = _close_ring_to_polygon(line)
            if poly.IsValid() and not poly.IsEmpty():
                faces.append(poly)
            else:
                open_lines.append(line)
        else:
            open_lines.append(line)
    if open_lines:
        faces.extend(_buildarea_faces(open_lines))
    if not faces:
        print(f"no closed faces found in parcel layer {parcel_layer!r}",
              file=sys.stderr)
        return 1

    def _labels_within(poly, layer_name: str) -> list:
        return [t for (t, g) in labels.get(layer_name, []) if g.Within(poly)]

    # Dynamic fields: every TEXT layer not covered by the fixed mapping
    # becomes an attribute. If its name matches a schema field (e.g. a
    # layer "A" carrying NIK texts), it feeds that field; otherwise the
    # field is named after the layer. Nothing textual is dropped.
    # NOTE: GPKG/SQLite column names are case-insensitive, so "ket" and
    # "KET" are the same column — match case-insensitively, or the
    # CREATE TABLE fails with "duplicate column name" (caught 2026-11-26).
    known_layers = set(SURVEYOR_LABELS.values())
    schema_by_upper = {name.upper(): name for name in TARGET_FIELDS}
    dynamic_fields: dict[str, str] = {}  # field name -> TEXT layer name
    for lyr_name in labels:
        if lyr_name in known_layers:
            continue
        fname = _field_name(lyr_name)
        if fname.upper() in schema_by_upper:
            fname = schema_by_upper[fname.upper()]
        if fname not in dynamic_fields:
            dynamic_fields[fname] = lyr_name

    # 3. Write the plugin-schema GPKG.
    if out_path.exists():
        out_path.unlink()
    odrv = ogr.GetDriverByName("GPKG")
    ods = odrv.CreateDataSource(str(out_path))
    olyr = ods.CreateLayer(target_layer, srs, ogr.wkbMultiPolygon)
    for name in TARGET_FIELDS:
        olyr.CreateField(ogr.FieldDefn(name, ogr.OFTString))
    for name in dynamic_fields:
        if name not in TARGET_FIELDS:
            olyr.CreateField(ogr.FieldDefn(name, ogr.OFTString))

    def _add_parcel(poly, src_layer):
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
        for fname, lyr_name in dynamic_fields.items():
            hits = _labels_within(poly, lyr_name)
            if hits:
                f.SetField(fname, "; ".join(hits))
        # Cap layer sumber SETELAH dynamic loop supaya menang bila ada
        # TEXT layer bernama "Layer" (sangat tidak mungkin, tapi aman).
        f.SetField("Layer", src_layer)
        for key in ("PBT", "PBTK", "TGLK"):
            if key in literals:
                f.SetField(key, literals[key])
        olyr.CreateFeature(f)

    for face in faces:
        _add_parcel(face, parcel_layer)
    for sp in shm_polys:
        _add_parcel(sp, shm_layer)

    dlyr = ods.CreateLayer("dividers", srs, ogr.wkbLineString)
    dlyr.CreateField(ogr.FieldDefn("KET", ogr.OFTString))
    dlyr.CreateField(ogr.FieldDefn("Layer", ogr.OFTString))
    for layer, geom in div_lines:
        f = ogr.Feature(dlyr.GetLayerDefn())
        f.SetGeometry(geom.Clone())
        f.SetField("KET", CODE_KET.get(layer))
        f.SetField("Layer", layer)
        dlyr.CreateFeature(f)

    # TEXTs falling outside every parcel are preserved here (Layer + Text)
    # so no textual content is ever dropped.
    all_parcels = faces + shm_polys
    llyr = ods.CreateLayer("labels", srs, ogr.wkbPoint)
    llyr.CreateField(ogr.FieldDefn("Layer", ogr.OFTString))
    llyr.CreateField(ogr.FieldDefn("Text", ogr.OFTString))
    unjoined = 0
    for lyr_name, items in labels.items():
        for text, geom in items:
            if not any(geom.Within(p) for p in all_parcels):
                f = ogr.Feature(llyr.GetLayerDefn())
                f.SetGeometry(geom.Clone())
                f.SetField("Layer", lyr_name)
                f.SetField("Text", text)
                llyr.CreateFeature(f)
                unjoined += 1
    ods = None
    print(json.dumps({
        "parcels": len(faces) + len(shm_polys),
        "faces": len(faces),
        "shm": len(shm_polys),
        "dividers": len(div_lines),
        "text_layers": {k: len(v) for k, v in labels.items()},
        "dynamic_fields": sorted(dynamic_fields),
        "unjoined_texts": unjoined,
        "output": str(out_path),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
