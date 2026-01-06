import uuid

import pytest

from app.services import knowledge_model as km


def test_predict_is_symmetric_and_monotonic():
    assert km.predict(0, 0) == 0.5
    assert km.predict(1, 0) > km.predict(0.5, 0) > 0.5
    assert km.predict(0, 1) == pytest.approx(1 - km.predict(1, 0))


def test_update_moves_toward_outcome():
    right = km.update(theta=0, theta_n=0, beta=0, beta_n=0, correct=True)
    wrong = km.update(theta=0, theta_n=0, beta=0, beta_n=0, correct=False)
    assert right.p == 0.5
    assert right.theta == pytest.approx(0.4)
    assert right.beta_delta == pytest.approx(-0.2)
    assert wrong.theta == pytest.approx(-0.4)
    assert wrong.beta_delta == pytest.approx(0.2)


def test_surprising_outcomes_move_more():
    hard_right = km.update(0, 0, beta=2, beta_n=0, correct=True)
    easy_right = km.update(0, 0, beta=-2, beta_n=0, correct=True)
    assert hard_right.theta > easy_right.theta > 0


def test_step_sizes_shrink_with_evidence_and_floor():
    assert km.student_k(0) > km.student_k(10) > km.student_k(100)
    assert km.student_k(10_000) == 0.15
    assert km.question_k(0) > km.question_k(50)
    assert km.question_k(10_000) == 0.05


@pytest.mark.parametrize(
    ("theta", "answered", "expected"),
    [
        (3.0, 2, "new"),
        (-1.0, 3, "struggling"),
        (0.5, 3, "developing"),
        (1.2, 3, "proficient"),
        (2.0, 3, "mastered"),
    ],
)
def test_levels(theta, answered, expected):
    assert km.level(theta, answered) == expected


def _c(topic="algebra", rating=0.0, subject="math"):
    return km.Candidate(id=uuid.uuid4(), subject=subject, topic=topic, rating=rating)


def test_rank_targets_seventy_percent():
    easy, medium, hard = _c(rating=-1), _c(rating=0), _c(rating=1)
    ranked = km.rank([hard, medium, easy], {}, {})
    assert [r.candidate for r in ranked] == [easy, medium, hard]
    assert ranked[0].p == pytest.approx(0.7311, abs=1e-4)


def test_rank_prefers_fresh_then_missed_then_mastered():
    fresh, missed, got_right = _c(), _c(), _c()
    ranked = km.rank([got_right, missed, fresh], {}, {missed.id: False, got_right.id: True})
    assert [r.candidate for r in ranked] == [fresh, missed, got_right]
    assert ranked[1].seen is False and ranked[2].seen is True


def test_rank_prefers_weaker_topic_at_equal_fit():
    # Same distance from target (p = 0.7 +/- 0.1) but different topic mastery.
    weak = _c(topic="geometry", rating=-1.0)
    strong = _c(topic="algebra", rating=1.0)
    thetas = {("math", "geometry"): -1.0 + km.math.log(0.8 / 0.2), ("math", "algebra"): 1.0 + km.math.log(0.6 / 0.4)}
    ranked = km.rank([strong, weak], thetas, {})
    assert ranked[0].candidate == weak
    assert ranked[0].mastery < ranked[1].mastery


def test_rank_is_deterministic():
    a, b = _c(), _c()
    assert [r.candidate for r in km.rank([a, b], {}, {})] == [r.candidate for r in km.rank([b, a], {}, {})]


def test_explain():
    c = _c(rating=-1)
    fresh = km.rank([c], {}, {})[0]
    assert km.explain(fresh) == "Predicted 73% chance of success in algebra (topic mastery 50%)"
    assert km.explain(km.rank([c], {}, {c.id: False})[0]).endswith("; you missed this one before")
    assert km.explain(km.rank([c], {}, {c.id: True})[0]).endswith("; review of a question you already got right")
