"""Past papers end to end: upload (PDF + answer key), review, publish, take as an exam, AI explanations and disputes."""

import json

import pytest

from app.ai.llm import LLMResult, text
from app.services import pdf_parser
from tests import pdfgen
from tests.conftest import create_org, join_code, org_id_of, register_student, subscribe
from tests.test_tutor import fake_llm  # noqa: F401

PAPERS = "/api/v1/papers"
PDF = "application/pdf"


def upload(client, headers, exam=None, key=None, **form):
    files = {"exam": ("chem-2015.pdf", exam if exam is not None else pdfgen.exam_pdf(), PDF)}
    if key is not None:
        files["answer_key"] = ("key.pdf", key, PDF)
    return client.post(PAPERS, files=files, data={k: str(v) for k, v in form.items()}, headers=headers)


def published_paper(client, school, **form):
    r = upload(client, school["teacher"], answer_key_text=pdfgen.KEY, **form)
    assert r.status_code == 202, r.text
    p = client.post(f"{PAPERS}/{r.json()['id']}/publish", headers=school["teacher"])
    assert p.status_code == 200, p.text
    return r.json()


def test_upload_reads_questions_metadata_and_pasted_key(client, school):
    r = upload(client, school["teacher"], answer_key_text="1. B 2. C 3. A 4. C 5. B")
    assert r.status_code == 202, r.text
    p = r.json()
    assert (p["status"], p["scope"], p["questions"], p["ready"]) == ("review", "school", 5, 5)
    assert (p["title"], p["subject"], p["grade"], p["year"], p["time_limit_minutes"]) == (
        "Chemistry, 2015 EC, natural science, entrance exam", "chemistry", 12, "2015 E.C.", 90,
    )
    assert (p["page_count"], p["scanned"], p["has_key"], p["warnings"]) == (3, False, True, [])
    assert [i["answer"] for i in p["items"]] == [1, 2, 0, 2, 1]
    assert p["items"][0]["choices"] == ["Oxygen", "Hydrogen", "Chlorine", "Nitrogen"]
    assert all(i["problems"] == [] and i["include"] for i in p["items"])


def test_key_as_pdf_and_form_overrides(client, school):
    r = upload(client, school["teacher"], key=pdfgen.key_pdf("1-B 2-C 3-A 4-C 5-B"), title="Mock A", time_limit_minutes=45, default_topic="Acids")
    p = r.json()
    assert r.status_code == 202 and [i["answer"] for i in p["items"]] == [1, 2, 0, 2, 1]
    assert (p["title"], p["time_limit_minutes"], p["default_topic"]) == ("Mock A", 45, "Acids")


def test_key_problems_are_flagged_per_question(client, school):
    p = upload(client, school["teacher"], answer_key_text="1. B 2. F 3. A 9. C").json()
    items = {i["number"]: i for i in p["items"]}
    assert items[2]["answer"] is None and "the key says F but only 4 choices were found" in items[2]["issues"]
    assert "no answer in the key" in items[4]["issues"] and items[4]["problems"] == ["no answer set"]
    assert p["ready"] == 2
    assert "The answer key has answers for questions that weren't found: 9." in p["warnings"]


def test_no_key_warns_and_nothing_is_ready(client, school):
    p = upload(client, school["teacher"]).json()
    assert p["has_key"] is False and p["ready"] == 0
    assert "No answer key was provided. Set each answer before publishing." in p["warnings"]


def test_worked_answers_section_supplies_answers_explanations_difficulty(client, school):
    p = upload(client, school["teacher"], exam=pdfgen.worked_answers_pdf()).json()
    assert [i["answer"] for i in p["items"]] == [1, 2, 0]
    assert [i["difficulty"] for i in p["items"]] == ["easy", "medium", "hard"]
    assert p["items"][0]["explanation"] == "Red blood cells contain haemoglobin, which binds oxygen."
    assert any("answer section" in w for w in p["warnings"])


def test_review_edits(client, school):
    pid = upload(client, school["teacher"]).json()["id"]
    url = f"{PAPERS}/{pid}/items/1"
    t = school["teacher"]
    r = client.patch(url, json={"answer": 1, "topic": "  Reactions of Metals ", "prompt": "Which gas forms when zinc meets HCl?"}, headers=t)
    item = r.json()["items"][0]
    assert (item["answer"], item["topic"], item["prompt"], item["problems"]) == (1, "reactions of metals", "Which gas forms when zinc meets HCl?", [])
    assert client.patch(url, json={"answer": 5}, headers=t).status_code == 400
    # The key follows the choice it named, not its position: answer 1 was "Hydrogen", which now
    # sits at index 0. Keeping index 1 would have silently made "Oxygen" the correct answer.
    item = client.patch(url, json={"choices": ["Hydrogen", "Oxygen"]}, headers=t).json()["items"][0]
    assert item["choices"] == ["Hydrogen", "Oxygen"] and item["answer"] == 0
    # Removing the correct choice drops the key rather than guessing.
    item = client.patch(url, json={"choices": ["Oxygen", "Chlorine"]}, headers=t).json()["items"][0]
    assert item["answer"] is None and item["problems"] == ["no answer set"]
    assert client.patch(url, json={"choices": ["Same", "same"], "answer": 0}, headers=t).json()["items"][0]["problems"] == ["choices must be different"]
    assert client.patch(f"{PAPERS}/{pid}/items/99", json={"include": False}, headers=t).status_code == 404
    assert client.patch(f"{PAPERS}/{pid}", json={"subject": "Physics", "grade": 11}, headers=t).json()["subject"] == "physics"


def test_publish_creates_questions_and_skips_unready(client, school):
    pid = upload(client, school["teacher"], answer_key_text="1. B 2. C 3. A 4. C").json()["id"]  # Q5 has no answer
    client.patch(f"{PAPERS}/{pid}/items/2", json={"include": False}, headers=school["teacher"])
    r = client.post(f"{PAPERS}/{pid}/publish", headers=school["teacher"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["paper"]["status"], body["paper"]["questions"]) == (3, [5], "published", 3)

    bank = client.get("/api/v1/questions", params={"scope": "school", "limit": 50}, headers=school["teacher"]).json()
    sources = sorted(q["source"] for q in bank)
    assert sources == [f"Chemistry, 2015 EC, natural science, entrance exam · Q{n}" for n in (1, 3, 4)]
    assert {q["subject"] for q in bank} == {"chemistry"} and {q["topic"] for q in bank} == {"general"}

    assert client.post(f"{PAPERS}/{pid}/publish", headers=school["teacher"]).status_code == 409
    assert client.patch(f"{PAPERS}/{pid}/items/1", json={"include": False}, headers=school["teacher"]).status_code == 409
    assert client.delete(f"{PAPERS}/{pid}", headers=school["teacher"]).status_code == 409


def test_publish_requires_something_ready(client, school):
    pid = upload(client, school["teacher"]).json()["id"]
    r = client.post(f"{PAPERS}/{pid}/publish", headers=school["teacher"])
    assert r.status_code == 400 and "No question is ready" in r.json()["detail"]
    assert client.delete(f"{PAPERS}/{pid}", headers=school["teacher"]).status_code == 204


def test_students_see_published_papers_only_and_take_them_as_exams(client, school):
    draft = upload(client, school["teacher"]).json()
    paper = published_paper(client, school)
    s = school["student"]
    listed = client.get(PAPERS, headers=s).json()
    assert [p["id"] for p in listed] == [paper["id"]]
    assert "items" not in client.get(f"{PAPERS}/{paper['id']}", headers=s).json()  # no answer key for students
    assert client.get(f"{PAPERS}/{draft['id']}", headers=s).status_code == 404
    assert client.post(f"{PAPERS}/{draft['id']}/start", headers=s).status_code == 404
    assert upload(client, s).status_code == 403

    r = client.post(f"{PAPERS}/{paper['id']}/start", headers=s)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["mode"] == "exam" and a["total"] == 5 and a["subject"] == "chemistry"
    assert [i["question"]["prompt"] for i in a["items"]] == [q for q, _ in pdfgen.QUESTIONS]
    from tests.test_exam import minutes

    assert minutes(a) == 90


def test_papers_are_private_to_the_school(client, school, database_url):
    paper = published_paper(client, school)
    other_admin = create_org(client, "admin@other.edu", "Other High")
    outsider = register_student(client, join_code(client, other_admin), "kid@other.edu")
    # Paid, so what follows is about tenant isolation rather than the premium gate.
    subscribe(database_url, org_id_of(client, other_admin))
    assert client.get(PAPERS, headers=outsider).json() == []
    assert client.post(f"{PAPERS}/{paper['id']}/start", headers=outsider).status_code == 404
    assert client.get(f"{PAPERS}/{paper['id']}", headers=other_admin).status_code == 404


def test_library_papers_need_a_platform_admin_and_reach_every_school(client, school, platform_admin):
    assert upload(client, school["teacher"], library="true").status_code == 403
    r = upload(client, platform_admin, library="true", answer_key_text=pdfgen.KEY)
    assert r.status_code == 202 and r.json()["scope"] == "library"
    assert client.post(f"{PAPERS}/{r.json()['id']}/publish", headers=school["teacher"]).status_code == 404
    assert client.post(f"{PAPERS}/{r.json()['id']}/publish", headers=platform_admin).status_code == 200
    assert [p["scope"] for p in client.get(PAPERS, headers=school["student"]).json()] == ["library"]


def test_rejects_non_pdf_and_scans_without_ocr(client, school, monkeypatch):
    r = client.post(PAPERS, files={"exam": ("notes.pdf", b"just text", PDF)}, headers=school["teacher"])
    assert r.status_code == 400 and r.json()["detail"] == "That file isn't a PDF."
    monkeypatch.setattr(pdf_parser, "ocr_available", lambda: False)
    r = upload(client, school["teacher"], exam=pdfgen.scanned_pdf())
    assert r.status_code == 400 and "scan" in r.json()["detail"]
    assert client.post(PAPERS, data={"title": "x"}, headers=school["teacher"]).status_code == 422


def test_ocr_scans_are_flagged_for_careful_review(client, school, monkeypatch):
    monkeypatch.setattr(pdf_parser, "ocr_available", lambda: True)
    # Stand-in for Tesseract: the "OCR text" of each scanned page is the original page's text.
    originals = pdf_parser.pymupdf.open(stream=pdfgen.exam_pdf(), filetype="pdf")
    monkeypatch.setattr(pdf_parser.pymupdf.Page, "get_textpage_ocr", lambda self, **kw: originals[self.number].get_textpage())
    monkeypatch.setattr(pdf_parser, "_page_lines", _ocr_lines(originals))
    p = upload(client, school["teacher"], exam=pdfgen.scanned_pdf(), answer_key_text=pdfgen.KEY).json()
    assert p["scanned"] and p["ocr_used"] and p["questions"] == 5
    assert p["warnings"][0].startswith("Scanned paper: text was read with OCR")


def _ocr_lines(originals):
    real = pdf_parser._page_lines

    def lines(page, number, textpage=None):
        # OCR output carries no font information: flatten each line into one plain span.
        out = real(originals[page.number], number)
        return [pdf_parser.Line([pdf_parser.Span(ln.text, False)], ln.page, ln.edge) for ln in out]

    return lines


# ---------- explanations and answer-key disputes ----------


def answered_question(client, school):
    """Publish the paper, have the student take and submit it; returns (attempt, question 1 item)."""
    paper = published_paper(client, school)
    a = client.post(f"{PAPERS}/{paper['id']}/start", headers=school["student"]).json()
    item = a["items"][0]
    answers = [{"question_id": item["question"]["id"], "choice_id": item["question"]["choices"][0]["id"]}]
    r = client.post(f"/api/v1/attempts/{a['id']}/submit", json={"answers": answers}, headers=school["student"])
    return r.json(), r.json()["items"][0]


def review(letter, confidence="high", explanation="Zinc displaces hydrogen from the acid, so hydrogen gas bubbles off."):
    return text(json.dumps({"your_answer": letter, "confidence": confidence, "explanation": explanation}), tokens=(300, 80))


def explain(client, headers, qid):
    return client.post(f"/api/v1/questions/{qid}/explain", headers=headers)


def test_student_must_answer_before_explaining(client, school, fake_llm):  # noqa: F811
    paper = published_paper(client, school)
    a = client.post(f"{PAPERS}/{paper['id']}/start", headers=school["student"]).json()
    qid = a["items"][0]["question"]["id"]
    assert explain(client, school["student"], qid).status_code == 403  # exam still in progress
    assert fake_llm.requests == []


def test_ai_explanation_is_generated_once_and_agrees_with_key(client, school, fake_llm):  # noqa: F811
    attempt, item = answered_question(client, school)
    assert item["explanation"] is None and item["correct_choice_id"]
    fake_llm.script = [review("B")]
    r = explain(client, school["student"], item["question"]["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["source"], body["ai_check"], body["ai_suggested_choice_id"]) == ("ai", "agrees", None)
    assert body["explanation"].startswith("Zinc displaces hydrogen")
    assert body["official_choice_id"] == item["correct_choice_id"]
    prompt = fake_llm.requests[0][0]["content"]
    assert "Which gas is produced when zinc" in prompt and "B. Hydrogen" in prompt and "Official answer key: B" in prompt

    # Cached: no second model call, and the review page now shows it.
    assert explain(client, school["teacher"], item["question"]["id"]).json()["explanation"] == body["explanation"]
    assert len(fake_llm.requests) == 1
    shown = client.get(f"/api/v1/attempts/{attempt['id']}", headers=school["student"]).json()["items"][0]
    assert shown["explanation"] == body["explanation"] and shown["ai_check"] == "agrees"
    usage = client.get("/api/v1/tutor/usage", headers=school["admin"]).json()
    assert {f["feature"]: f["tokens"] for f in usage["by_feature"]} == {"explain": 380}


def test_confident_disagreement_opens_a_dispute_staff_can_resolve(client, school, fake_llm):  # noqa: F811
    _, item = answered_question(client, school)
    q = item["question"]
    fake_llm.script = [review("C", explanation="Chlorine is wrong... actually the official key may contain an error.")]
    body = explain(client, school["student"], q["id"]).json()
    assert body["ai_check"] == "disputed" and body["ai_suggested_choice_id"] == q["choices"][2]["id"]
    assert body["official_choice_id"] == q["choices"][1]["id"]  # the official key is never changed silently

    disputes = client.get("/api/v1/questions/disputed", headers=school["teacher"]).json()
    assert [(d["question"]["id"], d["ai_suggested_choice_id"]) for d in disputes] == [(q["id"], q["choices"][2]["id"])]
    assert client.get("/api/v1/questions/disputed", headers=school["student"]).status_code == 403

    r = client.post(f"/api/v1/questions/{q['id']}/answer-review", json={"action": "accept_ai"}, headers=school["teacher"])
    assert r.status_code == 200
    fixed = r.json()
    assert fixed["ai_check"] == "corrected"
    assert next(c["id"] for c in fixed["choices"] if c["is_correct"]) == q["choices"][2]["id"]
    assert client.get("/api/v1/questions/disputed", headers=school["teacher"]).json() == []
    assert client.post(f"/api/v1/questions/{q['id']}/answer-review", json={"action": "keep"}, headers=school["teacher"]).status_code == 409


def test_keeping_the_key_regenerates_the_explanation(client, school, fake_llm):  # noqa: F811
    _, item = answered_question(client, school)
    qid = item["question"]["id"]
    fake_llm.script = [review("D"), review("B", explanation="Hydrogen is released.")]
    explain(client, school["student"], qid)
    r = client.post(f"/api/v1/questions/{qid}/answer-review", json={"action": "keep"}, headers=school["teacher"])
    assert r.json()["ai_check"] == "confirmed"
    body = explain(client, school["student"], qid).json()
    assert body["explanation"] == "Hydrogen is released." and body["ai_check"] == "agrees"


@pytest.mark.parametrize(
    "reply, check",
    [
        (review("C", confidence="low"), "unsure"),
        (text("Hydrogen forms because zinc is more reactive than hydrogen."), None),
    ],
)
def test_unsure_or_unstructured_answers_never_dispute(client, school, fake_llm, reply, check):  # noqa: F811
    _, item = answered_question(client, school)
    fake_llm.script = [reply]
    body = explain(client, school["student"], item["question"]["id"]).json()
    assert body["ai_check"] == check and body["ai_suggested_choice_id"] is None and body["explanation"]


def test_teacher_explanations_skip_the_model(client, school, fake_llm):  # noqa: F811
    pid = upload(client, school["teacher"], exam=pdfgen.worked_answers_pdf(), subject="biology").json()["id"]
    paper = client.post(f"{PAPERS}/{pid}/publish", headers=school["teacher"]).json()["paper"]
    a = client.post(f"{PAPERS}/{paper['id']}/start", headers=school["student"]).json()
    client.post(f"/api/v1/attempts/{a['id']}/submit", json={"answers": []}, headers=school["student"])
    body = explain(client, school["student"], a["items"][0]["question"]["id"]).json()
    assert body == {**body, "source": "teacher", "explanation": "Red blood cells contain haemoglobin, which binds oxygen."}
    assert fake_llm.requests == []


def test_model_outage_returns_503_and_is_logged(client, school, fake_llm, platform_admin):  # noqa: F811
    _, item = answered_question(client, school)
    fake_llm.script = []  # provider down
    r = explain(client, school["student"], item["question"]["id"])
    assert r.status_code == 503
    m = client.get("/api/v1/platform/ai-metrics", headers=platform_admin).json()
    assert (m["calls"], m["errors"]) == (1, 1)


def test_explanations_respect_the_school_ai_budget(client, school, fake_llm, monkeypatch):  # noqa: F811
    from app.core.config import get_settings

    _, item = answered_question(client, school)
    monkeypatch.setattr(get_settings(), "ai_school_monthly_token_budget", 0)
    assert explain(client, school["student"], item["question"]["id"]).status_code == 429
    assert fake_llm.requests == []


def test_unparseable_model_output_is_a_plain_explanation():
    from app.ai.explain import _parse

    assert _parse('```json\n{"your_answer": "B", "confidence": "high", "explanation": "x"}\n```') == {"your_answer": "B", "confidence": "high", "explanation": "x"}
    assert _parse("no json here") == {} and _parse("{broken") == {}
    assert isinstance(LLMResult, type)
