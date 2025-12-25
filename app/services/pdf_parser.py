"""Exam-paper PDF parsing with PyMuPDF.

Text PDFs: questions and choices are found from span-level font data (bold "12." / "A." labels), which survives
two-column choice layouts. Scanned PDFs (image-only pages) are detected and OCR'd through PyMuPDF + Tesseract when
available; OCR output has no font data, so a pattern-based fallback parses it.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from app.core.curriculum import ALIASES, SUBJECTS

MAX_PAGES = 80
LETTERS = "ABCDEF"
_BOLD_FLAG = 1 << 4

_QNUM = re.compile(r"(?:question|q)\s*(\d{1,3})\s*[.):]?|(\d{1,3})[.)]", re.I)
_OPT = re.compile(r"([A-Fa-f])[.)]")
_OPT_WITH_TEXT = re.compile(r"([A-F])[.)]\s+(.+)")
_DIFFICULTY = re.compile(r"easy|medium|hard|difficult", re.I)
_MARKS = re.compile(r"\(\d{1,2}\)")
_NOISE = re.compile(r"end\s*of\s*exam|e\s*n\s*d\s+o\s*f\s+e\s*x\s*a\s*m|generated on|^page\s+\d+(\s+of\s+\d+)?$", re.I)
_ANSWER_HEADING = re.compile(r"^\s*answers?(\s+key)?\s*:?\s*$", re.I)


class PdfError(Exception):
    """The file can't be used: not a PDF, encrypted, too long, or scanned with no OCR available."""


@dataclass
class Span:
    text: str
    bold: bool


@dataclass
class Line:
    spans: list[Span]
    page: int
    edge: bool = False  # in the top/bottom margin, where running headers and footers live

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.spans if s.text.strip())


@dataclass
class Extraction:
    lines: list[Line]
    page_count: int
    scanned: bool
    ocr_used: bool
    first_page: list[Line] = field(default_factory=list)  # before running-header removal, for title/metadata


@dataclass
class ParsedItem:
    number: int
    prompt: str
    choices: list[str]
    page: int
    issues: list[str] = field(default_factory=list)
    difficulty: str | None = None


@dataclass
class PaperMeta:
    title: str | None = None
    subject: str | None = None
    grade: int | None = None
    year: str | None = None
    time_limit_minutes: int | None = None
    expected_count: int | None = None


@dataclass
class SectionAnswer:
    answer: int | None = None
    explanation: str | None = None
    difficulty: str | None = None


@dataclass
class ParsedPaper:
    items: list[ParsedItem]
    meta: PaperMeta
    key_text: str | None  # an "Answer key" block found at the end of the paper itself
    answers: dict[int, SectionAnswer] = field(default_factory=dict)  # a worked-answers section (answer + explanation)


# ---------- extraction ----------


def ocr_available() -> bool:
    try:
        return bool(pymupdf.get_tessdata())
    except Exception:  # noqa: BLE001 - PyMuPDF raises different errors when Tesseract is missing
        return False


def _is_scanned_page(page: pymupdf.Page) -> bool:
    if len(page.get_text("text").strip()) >= 30:
        return False
    area = abs(page.rect)
    covered = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
    return area > 0 and covered / area > 0.5


def _page_lines(page: pymupdf.Page, number: int, textpage=None) -> list[Line]:
    raw = []
    for block in page.get_text("dict", textpage=textpage)["blocks"]:
        for line in block.get("lines", []):
            spans = [
                Span(s["text"], bool(s["flags"] & _BOLD_FLAG) or "bold" in s["font"].lower())
                for s in line["spans"]
                if s["text"].strip()
            ]
            if spans:
                raw.append((line["bbox"][1], line["bbox"][0], spans))
    # Group lines into visual rows (y within 4pt), then read each row left to right, so a question number sits
    # before its text and two-column choices read A, B then C, D.
    raw.sort(key=lambda r: r[0])
    rows: list[list[tuple[float, float, list[Span]]]] = []
    for r in raw:
        if rows and abs(r[0] - rows[-1][0][0]) <= 4:
            rows[-1].append(r)
        else:
            rows.append([r])
    height = page.rect.height or 1
    return [
        Line(spans, number, edge=not 0.07 < y / height < 0.93)
        for row in rows
        for y, _, spans in sorted(row, key=lambda r: r[1])
    ]


def probe(data: bytes) -> int:
    """Validate the container and return the page count, without rendering or OCR.

    Cheap enough to run inside the upload request, so a file that can never parse is refused
    immediately instead of occupying a worker slot.
    """
    if not data.startswith(b"%PDF-"):
        raise PdfError("That file isn't a PDF.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise PdfError("The PDF couldn't be opened (it may be damaged).") from e
    with doc:
        if doc.needs_pass:
            raise PdfError("The PDF is password-protected.")
        if doc.page_count == 0:
            raise PdfError("The PDF has no pages.")
        if doc.page_count > MAX_PAGES:
            raise PdfError(f"The PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.")
        # Reading the text layer is cheap — it's OCR that isn't. A scan this server can't OCR could
        # never succeed, so refuse it now rather than spend a worker on a doomed job.
        if _mostly_scanned(doc) and not ocr_available():
            raise PdfError(_NO_OCR)
        return doc.page_count


_NO_OCR = (
    "This PDF is a scan (pages are images, not text). OCR isn't installed on the server: "
    "install Tesseract to import scans, or upload the original text PDF."
)


def _mostly_scanned(doc: pymupdf.Document) -> bool:
    scanned = sum(1 for page in doc if _is_scanned_page(page))
    return scanned * 2 >= doc.page_count


def extract(data: bytes) -> Extraction:
    if not data.startswith(b"%PDF-"):
        raise PdfError("That file isn't a PDF.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise PdfError("The PDF couldn't be opened (it may be damaged).") from e
    with doc:
        if doc.needs_pass:
            raise PdfError("The PDF is password-protected.")
        if doc.page_count == 0:
            raise PdfError("The PDF has no pages.")
        if doc.page_count > MAX_PAGES:
            raise PdfError(f"The PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.")

        scanned_pages = [i for i, page in enumerate(doc) if _is_scanned_page(page)]
        scanned = len(scanned_pages) * 2 >= doc.page_count
        if scanned and not ocr_available():
            raise PdfError(_NO_OCR)
        lines: list[Line] = []
        for i, page in enumerate(doc):
            textpage = page.get_textpage_ocr(dpi=300, full=True) if i in scanned_pages and scanned else None
            lines.extend(_page_lines(page, i + 1, textpage))
        return Extraction(
            _drop_running_headers(lines, doc.page_count), doc.page_count, scanned, scanned, [ln for ln in lines if ln.page == 1]
        )


def _drop_running_headers(lines: list[Line], pages: int) -> list[Line]:
    """Remove margin text repeated on most pages (site names, page headers/footers)."""
    if pages < 3:
        return lines
    counts = Counter(text for _, text in {(ln.page, ln.text.lower()) for ln in lines if ln.edge})
    repeated = {t for t, n in counts.items() if n * 2 > pages}
    return [ln for ln in lines if not (ln.edge and ln.text.lower() in repeated)]


def text_of(data: bytes) -> str:
    """Plain text of a PDF (used for answer-key files)."""
    return "\n".join(ln.text for ln in extract(data).lines)


# ---------- question parsing ----------


@dataclass
class _Token:
    kind: str  # "q", "opt", "text"
    value: str
    page: int


def _is_label(span: Span, first: bool, pattern: re.Pattern) -> bool:
    """A label is set apart from body text: its own span at the start of a line (typeset papers put it in a separate
    font or column), or bold. Body text that merely starts with "A." is one longer span, so it never matches."""
    return bool(pattern.fullmatch(span.text.strip())) and (first or span.bold)


def _tokens_spans(lines: list[Line]) -> list[_Token]:
    out = []
    for ln in lines:
        for i, span in enumerate(ln.spans):
            s = span.text.strip()
            if _MARKS.fullmatch(s):
                continue
            if _is_label(span, i == 0, _QNUM):
                m = _QNUM.fullmatch(s)
                out.append(_Token("q", m.group(1) or m.group(2), ln.page))
            elif _is_label(span, i == 0, _OPT):
                out.append(_Token("opt", s[0].upper(), ln.page))
            elif i == 0 and (m := _OPT_WITH_TEXT.fullmatch(s)):
                out.append(_Token("opt", m.group(1), ln.page))
                out.append(_Token("text", m.group(2), ln.page))
            else:
                out.append(_Token("text", s, ln.page))
    return out


_LINE_Q = re.compile(r"^\s*(?:(?:question|q)\s*(\d{1,3})\s*[.):]?|(\d{1,3})[.)])\s+(.*)$", re.I)
_INLINE_OPT = re.compile(r"(?:^|(?<=\s))([A-F])[.)]\s+")


def _tokens_plain(lines: list[Line]) -> list[_Token]:
    out = []
    for ln in lines:
        text = _MARKS.sub(" ", ln.text).strip()
        if not text:
            continue
        if m := _LINE_Q.match(text + " "):
            out.append(_Token("q", m.group(1) or m.group(2), ln.page))
            text = m.group(3).strip()
        # Trailing space so a label alone on its line ("A.") still matches.
        text += " "
        pos = 0
        for m in _INLINE_OPT.finditer(text):
            if text[pos : m.start()].strip():
                out.append(_Token("text", text[pos : m.start()], ln.page))
            out.append(_Token("opt", m.group(1), ln.page))
            pos = m.end()
        if text[pos:].strip():
            out.append(_Token("text", text[pos:], ln.page))
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _MARKS.sub(" ", s)).strip()


def _assemble(tokens: list[_Token]) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    cur: ParsedItem | None = None
    parts: list[list[str]] = []  # [prompt parts, choice A parts, ...]

    def close() -> None:
        if cur is not None:
            cur.prompt = _clean(" ".join(parts[0]))
            cur.choices = [_clean(" ".join(p)) for p in parts[1:]]
            items.append(cur)

    for tok in tokens:
        if tok.kind == "q":
            n = int(tok.value)
            expected = cur.number + 1 if cur else None
            # Numbers must run in order, so numbered lists inside a question ("1. ... 2. ...") stay text.
            if expected is None or n in (expected, expected + 1):
                close()
                cur = ParsedItem(number=n, prompt="", choices=[], page=tok.page)
                if expected is not None and n == expected + 1:
                    cur.issues.append(f"question {expected} was not found")
                parts = [[]]
                continue
            tok = _Token("text", f"{tok.value}.", tok.page)
        if cur is None:
            continue
        # A difficulty tag right after the number ("Question 3   Easy") is metadata, not question text.
        if tok.kind == "text" and len(parts) == 1 and not parts[0] and _DIFFICULTY.fullmatch(tok.value.strip()):
            cur.difficulty = "hard" if tok.value.strip().lower() == "difficult" else tok.value.strip().lower()
            continue
        if tok.kind == "opt" and LETTERS.index(tok.value) == len(parts) - 1:
            parts.append([])
            continue
        if tok.kind == "opt":
            tok = _Token("text", f"{tok.value}.", tok.page)
        parts[-1].append(tok.value)
    close()
    return items


def _check(item: ParsedItem) -> None:
    if len(item.prompt) < 3:
        item.issues.append("question text is empty")
    if len(item.prompt) > 5000:
        item.issues.append("question text is too long")
    if len(item.choices) < 2:
        item.issues.append("fewer than 2 choices found")
    if len(item.choices) > 6:
        item.issues.append("more than 6 choices")
    if any(not c for c in item.choices):
        item.issues.append("a choice is empty")
    if any(len(c) > 500 for c in item.choices):
        item.issues.append("a choice is too long")
    if len({c.lower() for c in item.choices}) != len(item.choices):
        item.issues.append("two choices are identical")


def _meta(header: list[str]) -> PaperMeta:
    text = " \n".join(header)
    low = text.lower()
    meta = PaperMeta()
    titles = [h for h in header if len(h) > 12 and not re.search(r"time allowed|questions:|name\b", h, re.I)]
    meta.title = max(titles, key=len)[:200] if titles else None
    words = set(re.findall(r"[a-z]+", low))
    for s in SUBJECTS:
        if s in words:
            meta.subject = s
            break
    else:
        meta.subject = next((v for k, v in ALIASES.items() if k in words), None)
    if m := re.search(r"grade\s*(\d{1,2})", low):
        meta.grade = int(m.group(1)) if 1 <= int(m.group(1)) <= 12 else None
    if m := re.search(r"\b((?:19|20)\d{2})\s*(e\.?\s?c\.?|g\.?\s?c\.?)?", low):
        meta.year = m.group(1) + (" E.C." if m.group(2) and m.group(2).startswith("e") else "")
    if m := re.search(r"time\s*allowed\s*:?\s*(\d{1,3})\s*min", low):
        meta.time_limit_minutes = int(m.group(1))
    elif m := re.search(r"time\s*allowed\s*:?\s*(\d(?:\.\d)?)\s*h", low):
        meta.time_limit_minutes = round(float(m.group(1)) * 60)
    if m := re.search(r"questions\s*:?\s*(\d{1,3})", low):
        meta.expected_count = int(m.group(1))
    return meta


def parse_paper(ex: Extraction) -> ParsedPaper:
    lines = [ln for ln in ex.lines if not _NOISE.search(ln.text)]
    key_text = None
    for i, ln in enumerate(lines):
        if _ANSWER_HEADING.match(ln.text):
            key_text = "\n".join(x.text for x in lines[i + 1 :])
            lines = lines[:i]
            break

    span_mode = sum(1 for ln in lines for i, s in enumerate(ln.spans) if _is_label(s, i == 0, _QNUM)) >= 3

    def question_number(ln: Line) -> int | None:
        if span_mode:
            first = ln.spans[0]
            m = _QNUM.fullmatch(first.text.strip()) if _is_label(first, True, _QNUM) else None
            if m is None and (m2 := re.match(r"\s*(?:question|q)\s*(\d{1,3})[.):]?\s", first.text + " ", re.I)) and first.bold:
                return int(m2.group(1))
        else:
            m = _LINE_Q.match(_MARKS.sub(" ", ln.text) + " ")
        return int(m.group(1) or m.group(2)) if m else None

    # Numbering that restarts (… 99, 100, then 1 again) means a worked-answers section follows the questions.
    answers: dict[int, SectionAnswer] = {}
    highest = 0
    for i, ln in enumerate(lines):
        n = question_number(ln)
        if n is None:
            continue
        if highest >= 2 and n <= highest // 2:
            answers = _parse_answer_section(lines[i:], question_number)
            lines = lines[:i]
            break
        highest = max(highest, n)

    tokens = _tokens_spans(lines) if span_mode else _tokens_plain(lines)
    items = _assemble(tokens)
    for item in items:
        _check(item)

    def starts_question(ln: Line) -> bool:
        if span_mode:
            return any(_is_label(s, i == 0, _QNUM) for i, s in enumerate(ln.spans))
        return bool(_LINE_Q.match(_MARKS.sub(" ", ln.text) + " "))

    first = next((i for i, ln in enumerate(ex.first_page) if starts_question(ln)), len(ex.first_page))
    return ParsedPaper(items, _meta([ln.text for ln in ex.first_page[:first] if not _NOISE.search(ln.text)]), key_text, answers)


_SECTION_ANSWER = re.compile(r"answer\s*[:\-]?\s*\(?\s*([A-F])\s*\)?(?![a-z])", re.I)
_SECTION_DIFFICULTY = re.compile(r"difficulty\s*:?\s*(easy|medium|hard)", re.I)
_SECTION_META = re.compile(r"^(answer|difficulty|keyword|topic|easy|medium|hard)\b", re.I)


def _parse_answer_section(lines: list[Line], question_number) -> dict[int, SectionAnswer]:
    """Blocks like: 'Q33. <question>' / 'Answer: ( C )  Difficulty: Medium' / 'Keyword: ...' / <explanation>."""
    blocks: dict[int, list[str]] = {}
    current: int | None = None
    for ln in lines:
        if (n := question_number(ln)) is not None:
            current = n
            blocks[n] = []
        elif current is not None:
            blocks[current].append(ln.text)
    out: dict[int, SectionAnswer] = {}
    for n, texts in blocks.items():
        joined = " ".join(texts)
        ans = SectionAnswer()
        if m := _SECTION_ANSWER.search(joined):
            ans.answer = LETTERS.index(m.group(1).upper())
        if m := _SECTION_DIFFICULTY.search(joined):
            ans.difficulty = m.group(1).lower()
        # The explanation is what follows the answer line, minus labelled metadata lines.
        after = next((i for i, t in enumerate(texts) if _SECTION_ANSWER.search(t)), -1)
        body = [t for t in texts[after + 1 :] if not _SECTION_META.match(t.strip())]
        ans.explanation = _clean(" ".join(body))[:5000] or None
        out[n] = ans
    return out


# ---------- answer keys ----------

_PAIR = re.compile(r"(?<!\d)(\d{1,3})\s*[.):\-=]?\s*([A-F])(?![A-Z])")
_NUM_PAIR = re.compile(r"(?<!\d)(\d{1,3})\s*[.):\-=]\s*([1-6])(?!\d)")
_LONE_LETTER = re.compile(r"(?<![A-Z])([A-F])(?![A-Z])")


def parse_answer_key(text: str) -> tuple[dict[int, int], list[str]]:
    """Question number -> choice index. Accepts "1. B 2. D", "1-B", "1)B", tables, or a bare "BDBC..." sequence."""
    upper = re.sub(r"ANSWERS?(\s+KEY)?", " ", text.upper())
    warnings: list[str] = []
    key: dict[int, int] = {}
    pairs = _PAIR.findall(upper)
    if pairs:
        for n, letter in pairs:
            if int(n) in key and key[int(n)] != LETTERS.index(letter):
                warnings.append(f"answer key lists question {n} twice; using the last one")
            key[int(n)] = LETTERS.index(letter)
    elif num_pairs := _NUM_PAIR.findall(upper):
        key = {int(n): int(d) - 1 for n, d in num_pairs}
    else:
        letters = _LONE_LETTER.findall(re.sub(r"\d", " ", upper)) or list(re.sub(r"[^A-F]", "", upper))
        key = {i + 1: LETTERS.index(c) for i, c in enumerate(letters)}
    return key, warnings
