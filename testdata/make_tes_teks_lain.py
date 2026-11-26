"""Generate a test DXF with UNKNOWN text layers (proves the all-text rule).

- 1 square parcel (100x100 m) on layer 020100 (closed LWPOLYLINE).
- TEXT 080201 "00999" (NIB) inside the parcel.
- TEXT "nik" "3203121501900001" (unknown layer) inside the parcel
  -> must become a field named "nik" on the parcel.
- TEXT "A" "3203121501900002" (layer named like schema field A)
  inside the parcel -> must feed field A (NIK).
- TEXT "ket" "catatan pinggir" OUTSIDE the parcel
  -> must land in the `labels` preservation layer, not be dropped.
"""

OUT = "/home/hatch/workspace/work/dwg2gpkg-converter/testdata/tes_teks_lain.dxf"


def main():
    lines = []
    w = lines.append
    w("  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1009\n  0\nENDSEC")
    w("  0\nSECTION\n  2\nTABLES\n  0\nTABLE\n  2\nLAYER\n 70\n     5")
    for layer in ("020100", "080201", "nik", "A", "ket"):
        w(f"  0\nLAYER\n  2\n{layer}\n 70\n     0\n 62\n     7\n  6\nContinuous")
    w("  0\nENDTAB\n  0\nENDSEC")
    w("  0\nSECTION\n  2\nENTITIES")
    # square parcel 100x100 at (283400, 761400)
    ring = [(283400, 761400), (283500, 761400), (283500, 761500), (283400, 761500)]
    w("  0\nLWPOLYLINE\n  8\n020100\n 90\n     4\n 70\n     1")
    for x, y in ring:
        w(f" 10\n{x:.3f}\n 20\n{y:.3f}")
    # texts inside the parcel (centroid area)
    for layer, text, x, y in [
        ("080201", "00999", 283450, 761450),
        ("nik", "3203121501900001", 283460, 761450),
        ("A", "3203121501900002", 283470, 761450),
    ]:
        w(f"  0\nTEXT\n  8\n{layer}\n 10\n{x:.3f}\n 20\n{y:.3f}\n 30\n0.0\n 40\n2.0\n  1\n{text}")
    # stray text outside the parcel
    w("  0\nTEXT\n  8\nket\n 10\n283600.0\n 20\n761600.0\n 30\n0.0\n 40\n2.0\n  1\ncatatan pinggir")
    w("  0\nENDSEC\n  0\nEOF\n")
    with open(OUT, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
