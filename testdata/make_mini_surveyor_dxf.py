"""Generate a small, realistic surveyor-style DXF (R12) by hand.

Reads parcels from the reference GPKG and writes:
  - LWPOLYLINE (closed) on layer BIDANG  (5 parcels)
  - LWPOLYLINE (closed) on layer JALAN   (1 divider, KET=JALAN)
  - TEXT on layer NIB at each parcel centroid with the NIB value

GDAL's own DXF writer cannot emit TEXT entities, so this file is
hand-written to prove the service's label-join path (TEXT -> 080201).
"""

import sys
from osgeo import ogr

SRC = "/home/hatch/workspace/user/files/PBT_2091.gpkg"
OUT = "/home/hatch/workspace/work/dwg2gpkg-converter/testdata/PBT_mini_surveyor.dxf"
N_PARCELS = 5


def ring_wkt(geom):
    """Outer ring coords of a (multi)polygon."""
    if geom.GetGeometryName() == "MULTIPOLYGON":
        geom = geom.GetGeometryRef(0)
    ring = geom.GetGeometryRef(0)
    return [(ring.GetX(i), ring.GetY(i)) for i in range(ring.GetPointCount())]


def main():
    ogr.DontUseExceptions()
    ds = ogr.Open(SRC)
    lyr = ds.GetLayerByName("PBT_2091")
    parcels = []
    jalan = None
    for feat in lyr:
        ket = feat.GetField("KET")
        nib = feat.GetField("080201")
        geom = feat.GetGeometryRef()
        if ket == "JALAN" and jalan is None:
            jalan = (ring_wkt(geom), nib)
        elif len(parcels) < N_PARCELS and geom is not None:
            parcels.append((ring_wkt(geom), nib))
        if len(parcels) >= N_PARCELS and jalan is not None:
            break

    lines = []
    w = lines.append
    w("  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1009\n  0\nENDSEC")
    w("  0\nSECTION\n  2\nTABLES\n  0\nTABLE\n  2\nLAYER\n 70\n     2")
    for layer in ("BIDANG", "JALAN", "NIB"):
        w(f"  0\nLAYER\n  2\n{layer}\n 70\n     0\n 62\n     7\n  6\nContinuous")
    w("  0\nENDTAB\n  0\nENDSEC")
    w("  0\nSECTION\n  2\nENTITIES")

    def polyline(layer, ring):
        w(f"  0\nLWPOLYLINE\n  8\n{layer}\n 90\n{len(ring):6d}\n 70\n     1")
        for x, y in ring:
            w(f" 10\n{x:.3f}\n 20\n{y:.3f}")

    for ring, _nib in parcels:
        polyline("BIDANG", ring)
    if jalan:
        polyline("JALAN", jalan[0])

    for ring, nib in parcels:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        w(f"  0\nTEXT\n  8\nNIB\n 10\n{cx:.3f}\n 20\n{cy:.3f}\n 30\n0.0\n 40\n2.0\n  1\n{nib}")

    w("  0\nENDSEC\n  0\nEOF\n")
    with open(OUT, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {OUT}: {len(parcels)} BIDANG + {1 if jalan else 0} JALAN + {len(parcels)} TEXT")


if __name__ == "__main__":
    main()
