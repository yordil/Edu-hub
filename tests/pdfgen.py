"""Builds realistic exam PDFs in memory with PyMuPDF for parser and API tests."""

import textwrap

import pymupdf

W, H = 595, 842

QUESTIONS = [
    ("Which gas is produced when zinc reacts with dilute hydrochloric acid?", ["Oxygen", "Hydrogen", "Chlorine", "Nitrogen"]),
    (
        "The following are steps of titration: 1. rinse the burette 2. fill with titrant 3. record the volume. "
        "Which step comes first in a correct procedure for an accurate titration?",
        ["Step 3", "Step 2", "Step 1", "None of them"],
    ),
    ("A sample that turns blue litmus red is", ["acidic.", "basic.", "neutral.", "a salt of group B."]),
    ("What is the oxidation number of sulphur in H2SO4?", ["+2", "+4", "+6", "-2"]),
    ("Which of the following is an alkali metal?", ["Calcium", "Sodium", "Aluminium", "Iron"]),
]
KEY = "1. B 2. C 3. A 4. C 5. B"


def _header(page: pymupdf.Page, title: str) -> None:
    page.insert_text((260, 30), "Temari.et", fontname="hebo", fontsize=9)
    page.insert_text((110, 70), title, fontname="hebo", fontsize=14)
    page.insert_text((230, 90), "Grade 12 · Chemistry · 2015 E.C.", fontname="helv", fontsize=9)
    page.insert_text((40, 115), "Time allowed:", fontname="hebo", fontsize=9)
    page.insert_text((105, 115), "90 minutes", fontname="helv", fontsize=9)
    page.insert_text((290, 115), "Questions:", fontname="hebo", fontsize=9)
    page.insert_text((340, 115), str(len(QUESTIONS)), fontname="helv", fontsize=9)


def exam_pdf(
    title: str = "Chemistry, 2015 EC, natural science, entrance exam",
    prompt_override: str | None = None,
) -> bytes:
    """Three pages; question 3 is split across a page break; running header on every page; two-column choices.

    `prompt_override` replaces question 1's text, for exercising the content filter on a file
    that is structurally a valid paper.
    """
    questions = list(QUESTIONS)
    if prompt_override is not None:
        questions[0] = (prompt_override, QUESTIONS[0][1])
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=W, height=H)
    pages = [doc[i] for i in range(3)]
    _header(pages[0], title)
    for p in pages[1:]:
        p.insert_text((260, 30), "Temari.et", fontname="hebo", fontsize=9)

    def question(page, y, n, prompt, choices, split_after_prompt=None):
        page.insert_text((40, y), f"{n}.", fontname="hebo", fontsize=10)
        lines = textwrap.wrap(prompt, 95)
        for i, text in enumerate(lines):
            page.insert_text((62, y + i * 14), text, fontname="helv", fontsize=10)
        page.insert_text((545, y), "(1)", fontname="helv", fontsize=8)
        if split_after_prompt is not None:
            page, y = split_after_prompt, 60 - (len(lines) * 14) - 4
        y += len(lines) * 14 + 4
        for i, c in enumerate(choices):
            x = 62 if i % 2 == 0 else 308
            row = y + (i // 2) * 16
            page.insert_text((x, row), f"{'ABCD'[i]}.", fontname="hebo", fontsize=10)
            page.insert_text((x + 16, row), c, fontname="helv", fontsize=10)
        return y + 2 * 16 + 18

    y = question(pages[0], 160, 1, *questions[0])
    y = question(pages[0], y, 2, *questions[1])
    question(pages[0], 790, 3, *questions[2], split_after_prompt=pages[1])
    y = question(pages[1], 120, 4, *questions[3])
    question(pages[2], 80, 5, *questions[4])
    pages[2].insert_text((220, 200), "— END OF EXAMINATION —", fontname="helv", fontsize=9)
    return doc.tobytes()


def prose_pdf() -> bytes:
    """A readable PDF that is not a question paper: valid container, nothing to extract."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((60, 70), "Staff meeting notes", fontname="hebo", fontsize=14)
    body = (
        "The science department met to review laboratory safety procedures. Goggles must be worn "
        "whenever volatile reagents are in use, and the fume cupboard is to be serviced each term. "
        "Attendance was noted and the meeting closed at four o'clock."
    )
    for i, line in enumerate(textwrap.wrap(body, 85)):
        page.insert_text((60, 110 + i * 16), line, fontname="helv", fontsize=11)
    return doc.tobytes()


def key_pdf(text: str = KEY) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((60, 80), "Answer key", fontname="hebo", fontsize=14)
    for i, part in enumerate(text.split("  ") if "  " in text else [text]):
        page.insert_text((60, 110 + i * 16), part, fontname="helv", fontsize=11)
    return doc.tobytes()


def worked_answers_pdf() -> bytes:
    """'Question N' headings with a difficulty tag and 'A) text' choices, then a numbered answers+explanations section."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((70, 60), "Biology practice set", fontname="hebo", fontsize=16)
    y = 100
    qs = [
        ("What carries oxygen in the blood?", ["White blood cells", "Red blood cells", "Platelets", "Plasma proteins"], "Easy"),
        ("Which organelle makes ATP?", ["Ribosome", "Nucleus", "Mitochondrion", "Golgi body"], "Medium"),
        ("Rhizobium is involved in", ["nitrogen fixation", "denitrification", "decomposition", "nitrification"], "Hard"),
    ]
    for n, (prompt, choices, level) in enumerate(qs, 1):
        page.insert_text((67, y), f"Question {n}", fontname="hebo", fontsize=11)
        page.insert_text((480, y), level, fontname="hebo", fontsize=11)
        page.insert_text((75, y + 18), prompt, fontname="helv", fontsize=10)
        for i, c in enumerate(choices):
            page.insert_text((87, y + 36 + i * 15), f"{'ABCD'[i]}) {c}", fontname="helv", fontsize=10)
        y += 110
    page = doc.new_page(width=W, height=H)
    y = 60
    answers = [("B", "Easy", "Red blood cells contain haemoglobin, which binds oxygen."), ("C", "Medium", "Mitochondria run aerobic respiration."), ("A", "Hard", "Rhizobium fixes nitrogen in root nodules.")]
    for n, (letter, level, why) in enumerate(answers, 1):
        page.insert_text((63, y), f"Q{n}.", fontname="hebo", fontsize=10)
        page.insert_text((90, y), qs[n - 1][0], fontname="helv", fontsize=10)
        page.insert_text((172, y + 22), f"Answer: ( {letter} )", fontname="hebo", fontsize=10)
        page.insert_text((314, y + 22), "Difficulty:", fontname="hebo", fontsize=10)
        page.insert_text((388, y + 20), level, fontname="hebo", fontsize=10)
        page.insert_text((63, y + 38), "Keyword: Biology", fontname="helvetica-boldoblique", fontsize=10)
        page.insert_text((63, y + 52), why, fontname="helv", fontsize=10)
        y += 90
    return doc.tobytes()


def scanned_pdf() -> bytes:
    """A 'scan': each page is just a picture of a text page."""
    text_doc = pymupdf.open(stream=exam_pdf(), filetype="pdf")
    doc = pymupdf.open()
    for src in text_doc:
        pix = src.get_pixmap(dpi=60)
        page = doc.new_page(width=W, height=H)
        page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def encrypted_pdf() -> bytes:
    doc = pymupdf.open(stream=exam_pdf(), filetype="pdf")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
