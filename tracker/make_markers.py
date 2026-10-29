"""Printable sheets for the headset tracker (head_tracker.py), as a true-size vector PDF.

Page 1: the head marker, ArUco DICT_4X4_50 id 0, 150 mm black square.
Page 2: the ChArUco calibration board (4 x 6 squares of 40 mm, 28 mm markers, DICT_5X5_100).

Print at ACTUAL SIZE (100 %, no fit-to-page), then measure the marker's black square
with a ruler and pass that to head_tracker.py --marker-mm. Each page has a 100 mm ruler
to check the printer's scaling.

  python make_markers.py [out.pdf]
"""
import sys

import cv2

PT = 72 / 25.4            # PDF points per millimetre
A4_MM = (210.0, 297.0)

MARKER_DICT = cv2.aruco.DICT_4X4_50
MARKER_ID = 0
MARKER_MM = 150.0

BOARD_DICT = cv2.aruco.DICT_5X5_100
BOARD_SQUARES = (4, 6)    # (columns, rows)
BOARD_SQUARE_MM = 40.0
BOARD_MARKER_MM = 28.0    # 7 cells of 4 mm (5x5 bits + border): every edge lands on a whole mm


def make_board(square_mm: float = BOARD_SQUARE_MM) -> cv2.aruco.CharucoBoard:
    """Shared with head_tracker.py --calibrate / --calibrate-mount: both must describe the
    same board. square_mm = how big a square really is where it is shown (a screen scales it)."""
    return cv2.aruco.CharucoBoard(BOARD_SQUARES, square_mm / 1000.0, square_mm * BOARD_MARKER_MM / BOARD_SQUARE_MM / 1000.0,
                                  cv2.aruco.getPredefinedDictionary(BOARD_DICT))


def _rect(x: float, y: float, w: float, h: float) -> str:
    return f"{x * PT:.3f} {y * PT:.3f} {w * PT:.3f} {h * PT:.3f} re f\n"


def _text(x: float, y: float, size: float, s: str) -> str:
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x * PT:.2f} {y * PT:.2f} Td ({s}) Tj ET\n"


def _image(img, left_mm: float, top_mm: float, mm_per_px: float) -> str:
    """Black pixels as filled rectangles, one per horizontal run (PDF y grows upward)."""
    out = []
    rows, cols = img.shape[:2]
    for r in range(rows):
        c = 0
        while c < cols:
            if img[r, c] >= 128:
                c += 1
                continue
            start = c
            while c < cols and img[r, c] < 128:
                c += 1
            out.append(_rect(left_mm + start * mm_per_px, top_mm - (r + 1) * mm_per_px,
                             (c - start) * mm_per_px, mm_per_px))
    return "".join(out)


def _ruler(left_mm: float, y_mm: float) -> str:
    out = [_rect(left_mm, y_mm, 100.0, 0.4)]
    for i in range(11):
        out.append(_rect(left_mm + i * 10.0 - 0.15, y_mm, 0.3, 5.0 if i % 5 == 0 else 2.5))
    out.append(_text(left_mm + 102.0, y_mm, 9, "100 mm"))
    return "".join(out)


def marker_page() -> str:
    cells = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(MARKER_DICT), MARKER_ID, 6)
    left = (A4_MM[0] - MARKER_MM) / 2.0
    top = A4_MM[1] - 40.0
    return (_image(cells, left, top, MARKER_MM / 6.0)
            + _text(30, 90, 12, "ScrapVR head marker - ArUco DICT_4X4_50, id 0")
            + _text(30, 82, 10, "Print at ACTUAL SIZE (100 %, no fit to page).")
            + _text(30, 76, 10, "The black square should measure 150 mm. Measure it and run:")
            + _text(30, 70, 10, "head_tracker.py --marker-mm <measured>")
            + _text(30, 64, 10, "Mount flat on the wall in front of you, at about eye height, well lit.")
            + _ruler(30, 45))


def board_page() -> str:
    cols, rows = BOARD_SQUARES
    size_px = (int(cols * BOARD_SQUARE_MM), int(rows * BOARD_SQUARE_MM))   # 1 px = 1 mm
    img = make_board().generateImage(size_px, marginSize=0, borderBits=1)
    left = (A4_MM[0] - size_px[0]) / 2.0
    top = A4_MM[1] - 20.0
    return (_image(img, left, top, 1.0)
            + _text(25, 26, 10, "Calibration board - ChArUco 4x6, 40 mm squares, 28 mm markers, DICT_5X5_100")
            + _text(25, 20, 10, "Print at ACTUAL SIZE. Keep it flat (tape it to a table or a book).")
            + _ruler(25, 8))


def write_pdf(path: str, pages: list) -> None:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    w, h = A4_MM[0] * PT, A4_MM[1] * PT
    for i, content in enumerate(pages):
        data = content.encode("latin-1")
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>").encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    with open(path, "wb") as f:
        f.write(out)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "head_tracker_print.pdf"
    write_pdf(target, [marker_page(), board_page()])
    print("Wrote", target)
