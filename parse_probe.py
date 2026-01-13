"""Dev probe: parse a real exam PDF and print what the importer sees. `python parse_probe.py <file.pdf>`"""

import sys

from app.services import pdf_parser

data = open(sys.argv[1], "rb").read()
ex = pdf_parser.extract(data)
paper = pdf_parser.parse_paper(ex)
print(f"pages={ex.page_count} scanned={ex.scanned} items={len(paper.items)} meta={paper.meta}")
print(f"answer section: {len(paper.answers)} answers, {sum(1 for a in paper.answers.values() if a.explanation)} with explanations")
for n in (1, 33, 100):
    if n in paper.answers:
        a = paper.answers[n]
        print(f"  A{n}: {a.answer} {a.difficulty} | {(a.explanation or '')[:120]}")
bad = [i for i in paper.items if i.issues]
print(f"items with issues: {len(bad)}")
for i in bad[:10]:
    print("  ISSUE", i.number, i.issues, "|", i.prompt[:80], "|", i.choices)
for n in (1, 7, 13, 45, 55, 77, 91, 95):
    it = next((i for i in paper.items if i.number == n), None)
    if it:
        print(f"Q{it.number} p{it.page}: {it.prompt[:110]}")
        for letter, c in zip("ABCDEF", it.choices):
            print(f"     {letter}. {c[:90]}")
