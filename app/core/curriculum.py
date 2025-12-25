"""The curriculum: which subjects exist, which grades study them, and which ones each stream studies.

Ethiopian secondary school runs grades 9-12. Grades 9 and 10 follow one common curriculum; the
natural/social science split begins in grade 11, and grade 12 ends in the national entrance exam.
So a student has a grade always, and a stream only once they have reached grade 11.
"""

import enum


class Stream(str, enum.Enum):
    natural = "natural"
    social = "social"


GRADES: tuple[int, ...] = (9, 10, 11, 12)
STREAM_FROM_GRADE = 11


def validate_study_level(grade: int | None, stream: "Stream | None") -> None:
    """Grades 9-10 have no stream; grades 11-12 must have one.

    `grade is None` is accepted: accounts created before grades existed, and API clients that do
    not send one. Such a student keeps their old behaviour until they set a grade.
    """
    if grade is None:
        return
    if grade not in GRADES:
        raise ValueError(f"grade must be one of {', '.join(map(str, GRADES))}")
    if grade < STREAM_FROM_GRADE and stream is not None:
        raise ValueError(f"streams start in grade {STREAM_FROM_GRADE}; grade {grade} follows the common curriculum")
    if grade >= STREAM_FROM_GRADE and stream is None:
        raise ValueError(f"choose a stream (natural or social) for grade {grade}")


SUBJECTS: tuple[str, ...] = ("mathematics", "english", "sat", "physics", "chemistry", "biology", "history", "geography")

COMMON_SUBJECTS = frozenset({"mathematics", "english", "sat"})
STREAM_SUBJECTS: dict[Stream, frozenset[str]] = {
    Stream.natural: COMMON_SUBJECTS | {"physics", "chemistry", "biology"},
    Stream.social: COMMON_SUBJECTS | {"history", "geography"},
}

ALIASES = {"math": "mathematics", "maths": "mathematics", "bio": "biology", "chem": "chemistry", "phys": "physics", "geo": "geography"}


def normalize_tag(v: str) -> str:
    return " ".join(v.split()).lower()


def normalize_subject(v: str) -> str:
    tag = normalize_tag(v)
    return ALIASES.get(tag, tag)


def validate_subject(v: str) -> str:
    subject = normalize_subject(v)
    if subject not in SUBJECTS:
        raise ValueError(f"subject must be one of: {', '.join(SUBJECTS)}")
    return subject


def allowed_subjects(role: str, stream: Stream | None) -> frozenset[str] | None:
    """Students only practise their stream's subjects; staff and stream-less students see everything.

    No change was needed for grades: a grade 9-10 student has no stream by invariant, and the
    common curriculum covers every subject, so "stream-less sees everything" is already right.
    """
    if role == "student" and stream is not None:
        return STREAM_SUBJECTS[stream]
    return None
