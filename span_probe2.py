import sys

from app.services import pdf_parser

ex = pdf_parser.extract(open(sys.argv[1], "rb").read())
for i, ln in enumerate(ex.lines):
    if ln.text.startswith("Q33.") or ln.text.startswith("Q34."):
        for x in ex.lines[i : i + 6]:
            print(repr(x.text))
        print("--")
