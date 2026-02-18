import sys

import pymupdf

doc = pymupdf.open(sys.argv[1])
page = doc[int(sys.argv[2]) if len(sys.argv) > 2 else 0]
for b in page.get_text("dict", sort=True)["blocks"][:40]:
    for ln in b.get("lines", []):
        print(round(ln["bbox"][0]), round(ln["bbox"][1]), [(s["text"], s["font"], s["flags"]) for s in ln["spans"]])
