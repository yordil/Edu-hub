"""Elo-style knowledge model (Pelánek, "Applications of the Elo rating system in adaptive educational systems", 2016).

Each student has a skill `theta` per topic and each question has a difficulty `beta`.
P(correct) = sigmoid(theta - beta). After an answer both move toward the observed outcome,
with step sizes that shrink as evidence accumulates.
"""

import math
import uuid
from dataclasses import dataclass

from app.models.quiz import Difficulty

TARGET_SUCCESS = 0.7

INITIAL_QUESTION_RATING = {Difficulty.easy: -1.0, Difficulty.medium: 0.0, Difficulty.hard: 1.0}

# Weight on topic mastery when ranking: pulls practice toward weaker topics.
WEAK_TOPIC_WEIGHT = 0.3
# Already-seen questions are pushed down; ones answered correctly much more so.
SEEN_CORRECT_PENALTY = 0.5
SEEN_WRONG_PENALTY = 0.2

MIN_EVIDENCE = 3


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def predict(theta: float, beta: float) -> float:
    return sigmoid(theta - beta)


def student_k(answered: int) -> float:
    return max(0.15, 0.8 / (1 + 0.1 * answered))


def question_k(answered: int) -> float:
    return max(0.05, 0.4 / (1 + 0.05 * answered))


@dataclass(frozen=True)
class Update:
    p: float
    theta: float
    beta_delta: float


def update(theta: float, theta_n: int, beta: float, beta_n: int, correct: bool) -> Update:
    p = predict(theta, beta)
    error = (1.0 if correct else 0.0) - p
    return Update(p=p, theta=theta + student_k(theta_n) * error, beta_delta=-question_k(beta_n) * error)


def mastery(theta: float) -> float:
    """Probability of answering an average (beta=0) question in the topic."""
    return sigmoid(theta)


def level(theta: float, answered: int) -> str:
    if answered < MIN_EVIDENCE:
        return "new"
    m = mastery(theta)
    if m < 0.5:
        return "struggling"
    if m < 0.7:
        return "developing"
    if m < 0.85:
        return "proficient"
    return "mastered"


@dataclass(frozen=True)
class Candidate:
    id: uuid.UUID
    subject: str
    topic: str
    rating: float


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    p: float
    mastery: float
    seen: bool | None  # None = never answered, else whether it was answered correctly
    score: float


def rank(
    candidates: list[Candidate],
    theta_by_topic: dict[tuple[str, str], float],
    seen: dict[uuid.UUID, bool],
    target: float = TARGET_SUCCESS,
) -> list[Ranked]:
    """Lower score is better. Ties break on id so ordering is deterministic."""
    ranked = []
    for c in candidates:
        theta = theta_by_topic.get((c.subject, c.topic), 0.0)
        p = predict(theta, c.rating)
        m = mastery(theta)
        prior = seen.get(c.id)
        penalty = 0.0 if prior is None else (SEEN_CORRECT_PENALTY if prior else SEEN_WRONG_PENALTY)
        score = abs(p - target) + WEAK_TOPIC_WEIGHT * m + penalty
        ranked.append(Ranked(candidate=c, p=p, mastery=m, seen=prior, score=score))
    ranked.sort(key=lambda r: (r.score, str(r.candidate.id)))
    return ranked


def explain(r: Ranked) -> str:
    text = f"Predicted {r.p:.0%} chance of success in {r.candidate.topic} (topic mastery {r.mastery:.0%})"
    if r.seen is False:
        text += "; you missed this one before"
    elif r.seen is True:
        text += "; review of a question you already got right"
    return text
