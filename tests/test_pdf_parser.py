"""PyMuPDF exam-paper parser: layout handling, metadata, answer keys, worked-answer sections, scans."""

import pytest

from app.services import pdf_parser
from tests import pdfgen


def parse(data: bytes):
    return pdf_parser.parse_paper(pdf_parser.extract(data))


def test_parses_questions_and_two_column_choices():
    paper = parse(pdfgen.exam_pdf())
    assert [i.number for i in paper.items] == [1, 2, 3, 4, 5]
    assert all(not i.issues for i in paper.items)
    for item, (prompt, choices) in zip(paper.items, pdfgen.QUESTIONS):
        assert item.prompt == prompt
        assert item.choices == choices


def test_layout_noise_is_removed():
    paper = parse(pdfgen.exam_pdf())
    text = " ".join(i.prompt + " " + " ".join(i.choices) for i in paper.items)
    assert "Temari.et" not in text  # running header on every page
    assert "(1)" not in text  # marks column
    assert "END OF EXAMINATION" not in text
    # Inline numbered list inside question 2 stays in the prompt instead of becoming questions.
    assert "1. rinse the burette 2. fill with titrant 3. record" in paper.items[1].prompt
    # "group B." inside a choice is text, not a choice label.
    assert paper.items[2].choices[3] == "a salt of group B."


def test_question_split_across_pages():
    item = parse(pdfgen.exam_pdf()).items[2]
    assert item.page == 1 and item.choices == pdfgen.QUESTIONS[2][1]


def test_reads_paper_metadata():
    meta = parse(pdfgen.exam_pdf()).meta
    assert meta.title == "Chemistry, 2015 EC, natural science, entrance exam"
    assert (meta.subject, meta.grade, meta.year, meta.time_limit_minutes, meta.expected_count) == ("chemistry", 12, "2015 E.C.", 90, 5)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1. B 2. D 3. A", {1: 1, 2: 3, 3: 0}),
        ("1-b, 2-c, 3-a", {1: 1, 2: 2, 3: 0}),
        ("1)A 2)B 10)D", {1: 0, 2: 1, 10: 3}),
        ("Q1: C\nQ2: A", {1: 2, 2: 0}),
        ("1 A\n2 B\n3 C", {1: 0, 2: 1, 3: 2}),
        ("Answer key: B D C A", {1: 1, 2: 3, 3: 2, 4: 0}),
        ("BDCA", {1: 1, 2: 3, 3: 2, 4: 0}),
        ("1. 2  2. 4  3. 1", {1: 1, 2: 3, 3: 0}),
        ("Grade 12 Biology 2014 E.C.\n1. B 2. C", {1: 1, 2: 2}),
    ],
)
def test_answer_key_formats(text, expected):
    key, warnings = pdf_parser.parse_answer_key(text)
    assert key == expected and warnings == []


def test_answer_key_duplicates_warn():
    key, warnings = pdf_parser.parse_answer_key("1. A 2. B 1. C")
    assert key == {1: 2, 2: 1}
    assert warnings == ["answer key lists question 1 twice; using the last one"]


def test_question_headings_and_worked_answers_section():
    paper = parse(pdfgen.worked_answers_pdf())
    assert [i.number for i in paper.items] == [1, 2, 3]
    assert paper.items[0].prompt == "What carries oxygen in the blood?"
    assert paper.items[1].choices == ["Ribosome", "Nucleus", "Mitochondrion", "Golgi body"]
    assert [i.difficulty for i in paper.items] == ["easy", "medium", "hard"]
    assert {n: a.answer for n, a in paper.answers.items()} == {1: 1, 2: 2, 3: 0}
    assert paper.answers[1].explanation == "Red blood cells contain haemoglobin, which binds oxygen."
    assert paper.answers[3].difficulty == "hard"
    assert paper.meta.title == "Biology practice set" and paper.meta.subject == "biology"


def test_scanned_pdf_is_detected(monkeypatch):
    monkeypatch.setattr(pdf_parser, "ocr_available", lambda: False)
    with pytest.raises(pdf_parser.PdfError, match="scan"):
        pdf_parser.extract(pdfgen.scanned_pdf())


def test_scanned_pdf_is_ocrd_when_tesseract_present(monkeypatch):
    calls = []
    monkeypatch.setattr(pdf_parser, "ocr_available", lambda: True)
    monkeypatch.setattr(pdf_parser.pymupdf.Page, "get_textpage_ocr", lambda self, **kw: calls.append(kw) or self.get_textpage())
    ex = pdf_parser.extract(pdfgen.scanned_pdf())
    assert ex.scanned and ex.ocr_used and len(calls) == 3


@pytest.mark.parametrize(
    "data, message",
    [(b"hello", "isn't a PDF"), (b"%PDF-1.7 garbage", "couldn't be opened"), (pdfgen.encrypted_pdf(), "password")],
)
def test_bad_files(data, message):
    with pytest.raises(pdf_parser.PdfError, match=message):
        pdf_parser.extract(data)
