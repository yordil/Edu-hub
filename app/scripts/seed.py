"""Idempotent demo data: `python -m app.scripts.seed`. Never run against production."""

import asyncio
import random
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.curriculum import STREAM_SUBJECTS, Stream
from app.core.db import get_engine, get_sessionmaker
from app.models.contest import Contest, ContestEntry
from app.models.organization import Organization
from app.models.user import User
from app.models.quiz import AttemptMode, Difficulty
from app.schemas.contest import ContestCreate, ContestSubmit
from app.schemas.platform import AnnouncementCreate
from app.schemas.quiz import AnswerIn, AttemptCreate, AttemptSubmit, ChoiceIn, QuestionCreate
from app.schemas.user import OrganizationRegister, StudentRegister
from app.services import announcement_service, contest_service, quiz_service, user_service

ORG_NAME = "Demo Academy"
PASSWORD = "Password123!"
ADMIN = "admin@demo.eduhub.dev"
TEACHER = "teacher@demo.eduhub.dev"
STUDENT = "student@demo.eduhub.dev"
GRADE10 = "grade10@demo.eduhub.dev"

E, M, H = Difficulty.easy, Difficulty.medium, Difficulty.hard

# (subject, topic, difficulty, prompt, [correct, *wrong], explanation)
BANK = [
    ("mathematics", "algebra", E, "Solve for x: 2x + 6 = 14", ["4", "3", "8", "10"], "Subtract 6 from both sides (2x = 8), then divide by 2."),
    ("mathematics", "algebra", M, "What are the roots of x² − 5x + 6 = 0?", ["2 and 3", "−2 and −3", "1 and 6", "−1 and 6"], "Factor as (x − 2)(x − 3) = 0."),
    ("mathematics", "algebra", H, "For which k does x² + kx + 9 = 0 have exactly one real root?", ["k = ±6", "k = ±3", "k = 9", "k = 0"], "One root means discriminant k² − 36 = 0, so k = ±6."),
    ("mathematics", "geometry", E, "What is the sum of interior angles of a triangle?", ["180°", "90°", "360°", "270°"], "Interior angles of any triangle add up to 180°."),
    ("mathematics", "geometry", M, "A right triangle has legs 6 and 8. What is the hypotenuse?", ["10", "12", "14", "√28"], "By Pythagoras: √(6² + 8²) = √100 = 10."),
    ("mathematics", "geometry", H, "What is the area of a circle inscribed in a square of side 10?", ["25π", "100π", "50π", "10π"], "The radius is half the side (5), so area = π·5² = 25π."),
    ("mathematics", "calculus", E, "What is d/dx of x³?", ["3x²", "x²", "3x³", "x⁴/4"], "Power rule: d/dx xⁿ = n·xⁿ⁻¹."),
    ("mathematics", "calculus", M, "What is ∫ 2x dx?", ["x² + C", "2x² + C", "x + C", "2 + C"], "The antiderivative of 2x is x², plus a constant."),
    ("mathematics", "calculus", H, "What is the limit of sin(x)/x as x → 0?", ["1", "0", "∞", "Does not exist"], "A standard limit, provable with the squeeze theorem."),
    ("physics", "mechanics", E, "What is the SI unit of force?", ["Newton", "Joule", "Watt", "Pascal"], "1 N = 1 kg·m/s²."),
    ("physics", "mechanics", M, "A 2 kg mass accelerates at 3 m/s². What net force acts on it?", ["6 N", "1.5 N", "5 N", "9 N"], "Newton's second law: F = ma = 2 × 3."),
    ("physics", "mechanics", H, "A ball is thrown straight up at 20 m/s (g = 10 m/s²). How high does it rise?", ["20 m", "40 m", "10 m", "2 m"], "v² = u² − 2gh gives h = 400 / 20 = 20 m."),
    ("physics", "electricity", E, "Ohm's law relates voltage, current and…", ["Resistance", "Power", "Charge", "Capacitance"], "V = IR."),
    ("physics", "electricity", M, "Two 6 Ω resistors in parallel have what total resistance?", ["3 Ω", "12 Ω", "6 Ω", "1.5 Ω"], "For equal resistors in parallel: R/n = 6/2."),
    ("physics", "electricity", H, "A 60 W bulb runs on 120 V. What current does it draw?", ["0.5 A", "2 A", "7200 A", "0.25 A"], "P = VI, so I = 60 / 120."),
    ("chemistry", "periodic table", E, "What is the chemical symbol for sodium?", ["Na", "S", "So", "Sd"], "From the Latin 'natrium'."),
    ("chemistry", "periodic table", M, "Which group contains the noble gases?", ["Group 18", "Group 1", "Group 17", "Group 2"], "Noble gases have full outer shells and sit in group 18."),
    ("chemistry", "periodic table", H, "Across a period, atomic radius generally…", ["Decreases", "Increases", "Stays the same", "Doubles"], "Rising nuclear charge pulls electrons closer."),
    ("chemistry", "stoichiometry", E, "How many atoms are in one molecule of H₂O?", ["3", "2", "1", "4"], "Two hydrogens plus one oxygen."),
    ("chemistry", "stoichiometry", M, "What is the molar mass of CO₂? (C = 12, O = 16)", ["44 g/mol", "28 g/mol", "32 g/mol", "40 g/mol"], "12 + 2 × 16 = 44."),
    ("chemistry", "stoichiometry", H, "How many moles are in 9 g of water? (H₂O = 18 g/mol)", ["0.5 mol", "2 mol", "9 mol", "0.18 mol"], "n = m / M = 9 / 18."),
    ("biology", "cells", E, "Which organelle is called the powerhouse of the cell?", ["Mitochondrion", "Nucleus", "Ribosome", "Golgi body"], "Mitochondria produce ATP via cellular respiration."),
    ("biology", "cells", M, "Which structure is found in plant cells but not animal cells?", ["Cell wall", "Cell membrane", "Ribosome", "Cytoplasm"], "Plant cells have a cellulose cell wall."),
    ("biology", "cells", H, "In which phase of mitosis do sister chromatids separate?", ["Anaphase", "Prophase", "Metaphase", "Telophase"], "Spindle fibres pull chromatids apart during anaphase."),
    ("biology", "genetics", E, "What molecule carries genetic information?", ["DNA", "ATP", "Glucose", "Protein"], "DNA stores hereditary information."),
    ("biology", "genetics", M, "Crossing Aa × Aa gives what fraction of aa offspring?", ["1/4", "1/2", "3/4", "0"], "A Punnett square gives AA : Aa : aa = 1 : 2 : 1."),
    ("biology", "genetics", H, "Which base pairs with adenine in RNA?", ["Uracil", "Thymine", "Cytosine", "Guanine"], "RNA uses uracil in place of thymine."),
    ("english", "grammar", E, "Choose the correct form: She ___ to school every day.", ["goes", "go", "going", "gone"], "Third-person singular in the present simple adds -s/-es."),
    ("english", "grammar", M, "Which sentence is grammatically correct?", ["Neither of the answers is correct.", "Neither of the answers are correct.", "Neither of the answer is correct.", "Neither the answers is correct."], "'Neither' is singular, so it takes 'is'."),
    ("english", "grammar", H, "Which sentence is in the passive voice?", ["The essay was written by Abebe.", "Abebe wrote the essay.", "Abebe is writing the essay.", "Abebe will write the essay."], "Passive = form of 'be' + past participle; the doer follows 'by'."),
    ("english", "vocabulary", E, "Which word is a synonym of 'rapid'?", ["Fast", "Slow", "Heavy", "Quiet"], "'Rapid' means happening quickly."),
    ("english", "vocabulary", M, "'Benevolent' most nearly means…", ["Kind and generous", "Angry", "Lazy", "Doubtful"], "From Latin bene (well) + volens (wishing)."),
    ("english", "reading", H, "In an argumentative essay, a 'counterclaim' is…", ["An opposing view the writer addresses", "The writer's main claim", "A supporting statistic", "The conclusion"], "Addressing counterclaims makes an argument stronger."),
    ("sat", "sat math", E, "If 3x − 7 = 11, what is the value of 6x?", ["36", "18", "6", "12"], "3x = 18, so 6x = 36. Spotting 6x = 2·3x saves time."),
    ("sat", "sat math", M, "A school uniform costs 400 ETB after a 20% discount. What was the original price?", ["500 ETB", "480 ETB", "320 ETB", "600 ETB"], "0.8 × price = 400 ETB, so price = 500 ETB."),
    ("sat", "sat math", H, "If f(x) = 2x² − 3 and f(a) = 15, which could be the value of a?", ["3", "9", "6", "√6"], "2a² = 18 → a² = 9 → a = ±3."),
    ("sat", "reading and writing", E, "Choose the best transition: 'The experiment failed. ___, the team learned a great deal.'", ["Nevertheless", "Therefore", "Similarly", "For example"], "The sentences contrast, so a contrast transition fits."),
    ("sat", "reading and writing", M, "Complete the text: 'The scientists ___ results were published last week spoke at the conference.'", ["whose", "who's", "whom", "which"], "'Whose' shows possession; 'who's' means 'who is'."),
    ("history", "world history", E, "In which year did World War II end?", ["1945", "1939", "1918", "1950"], "Germany surrendered in May and Japan in September 1945."),
    ("history", "world history", M, "The Industrial Revolution began in which country?", ["Great Britain", "France", "United States", "Germany"], "Coal, iron and new textile machines drove it in late-18th-century Britain."),
    ("history", "world history", H, "The Treaty of Versailles (1919) formally ended which war?", ["World War I", "World War II", "The Crimean War", "The Napoleonic Wars"], "It set the peace terms for Germany after World War I."),
    ("history", "african history", E, "The Battle of Adwa (1896) was fought between Ethiopia and which country?", ["Italy", "Britain", "France", "Germany"], "Forces under Emperor Menelik II defeated Italy, preserving Ethiopia's independence."),
    ("history", "african history", M, "Which organization, founded in Addis Ababa in 1963, preceded the African Union?", ["Organisation of African Unity", "ECOWAS", "SADC", "The Arab League"], "The OAU was replaced by the AU in 2002."),
    ("history", "african history", H, "The ancient Kingdom of Aksum is famous for…", ["Giant carved stone stelae", "The pyramids of Giza", "The Great Wall", "The terracotta army"], "Aksum's stelae are UNESCO World Heritage monuments."),
    ("geography", "physical geography", E, "What is the longest river in Africa?", ["Nile", "Congo", "Niger", "Zambezi"], "The Nile flows about 6,650 km to the Mediterranean."),
    ("geography", "physical geography", M, "The Great Rift Valley was formed mainly by…", ["Tectonic plates pulling apart", "Glacial erosion", "River deposition", "Wind erosion"], "The African plate is splitting, creating a rift."),
    ("geography", "physical geography", H, "Which layer of the atmosphere contains most weather?", ["Troposphere", "Stratosphere", "Mesosphere", "Thermosphere"], "The troposphere holds most water vapour and clouds."),
    ("geography", "map skills", E, "Lines of latitude measure distance…", ["North or south of the equator", "East or west of Greenwich", "Above sea level", "Between time zones"], "Latitude runs parallel to the equator."),
    ("geography", "map skills", M, "On a 1:50,000 map, 2 cm represents…", ["1 km", "100 m", "10 km", "2 km"], "2 × 50,000 cm = 100,000 cm = 1 km."),
    ("geography", "map skills", H, "It is 12:00 at 0° longitude. Ignoring time zones, what is the local time at 45° E?", ["15:00", "09:00", "12:45", "16:30"], "Earth turns 15° per hour, so 45° E is 3 hours ahead."),
]

# Per-topic chance the demo students answer correctly, so dashboards show strengths and gaps.
SKILL = {
    "algebra": 0.9, "geometry": 0.75, "calculus": 0.5, "probability": 0.6, "mechanics": 0.7, "electricity": 0.45,
    "periodic table": 0.85, "stoichiometry": 0.4, "cells": 0.8, "genetics": 0.3, "grammar": 0.8, "vocabulary": 0.7,
    "reading": 0.55, "sat math": 0.65, "reading and writing": 0.5, "world history": 0.7, "african history": 0.6,
    "physical geography": 0.5, "map skills": 0.45,
}

GRADE = {
    "algebra": 9, "geometry": 10, "calculus": 12, "mechanics": 11, "electricity": 11, "periodic table": 9,
    "stoichiometry": 10, "cells": 9, "genetics": 10, "grammar": 9, "vocabulary": 10, "reading": 11,
    "sat math": 12, "reading and writing": 12, "world history": 10, "african history": 11,
    "physical geography": 9, "map skills": 10,
}

# Demo Academy's own questions, private to that school.
SCHOOL_BANK = [
    ("mathematics", "probability", E, "A fair die is rolled. What is P(rolling a 6)?", ["1/6", "1/3", "1/2", "6"], "One favourable outcome out of six."),
    ("mathematics", "probability", M, "Two fair coins are tossed. What is P(exactly one head)?", ["1/2", "1/4", "3/4", "1/3"], "HT and TH are 2 of the 4 equally likely outcomes."),
]

PLATFORM_ADMIN = "owner@eduhub.dev"
OTHER_SCHOOL = "Riverside High"


def _question(row, grade: int | None, source: str | None) -> QuestionCreate:
    subject, topic, difficulty, prompt, (right, *wrong), explanation = row
    choices = [ChoiceIn(text=right, is_correct=True), *[ChoiceIn(text=w) for w in wrong]]
    random.Random(prompt).shuffle(choices)
    return QuestionCreate(
        subject=subject, topic=topic, difficulty=difficulty, grade=grade, source=source,
        prompt=prompt, explanation=explanation, choices=choices,
    )


async def _session(db, student, rng: random.Random, subject: str, size: int, when: datetime) -> None:
    attempt = await quiz_service.start_attempt(db, AttemptCreate(mode=AttemptMode.adaptive, subject=subject, size=size), student)
    answers = []
    for item in attempt.items:
        q = item.question
        pick = q.correct_choice if rng.random() < SKILL[q.topic] else rng.choice([c for c in q.choices if not c.is_correct])
        answers.append(AnswerIn(question_id=q.id, choice_id=pick.id))
    await quiz_service.submit_attempt(db, attempt.id, AttemptSubmit(answers=answers), student)
    # Backdate so activity heatmaps and streaks show realistic history.
    attempt.started_at = when - timedelta(minutes=2 * size)
    attempt.submitted_at = when
    await db.commit()


async def _history(db, student, rng: random.Random, active_days: set[int]) -> None:
    """One or two sessions on each `days ago` value, using only the student's stream subjects."""
    subjects = sorted(STREAM_SUBJECTS[student.stream])
    now = datetime.now(timezone.utc)
    for days_ago in sorted(active_days, reverse=True):
        for _ in range(rng.choice([1, 1, 2])):
            if days_ago == 0:
                when = max(now - timedelta(minutes=rng.randint(5, 90)), now.replace(hour=0, minute=1, second=0, microsecond=0))
            else:
                day = (now - timedelta(days=days_ago)).date()
                when = datetime.combine(day, time(rng.randint(7, 20), rng.randint(0, 59)), tzinfo=timezone.utc)
            await _session(db, student, rng, rng.choice(subjects), rng.choice([3, 4, 5]), when)


async def _compete(db, contest, student, rng: random.Random, skill: float) -> None:
    await contest_service.start(db, student, contest.id)
    answers = []
    for cq in contest.questions:
        q = cq.question
        pick = q.correct_choice if rng.random() < skill else rng.choice([c for c in q.choices if not c.is_correct])
        answers.append(AnswerIn(question_id=q.id, choice_id=pick.id))
    await contest_service.submit(db, student, contest.id, ContestSubmit(answers=answers))
    entry = await db.scalar(select(ContestEntry).where(ContestEntry.contest_id == contest.id, ContestEntry.user_id == student.id))
    entry.time_taken_seconds = rng.randint(90, contest.duration_minutes * 60)
    await db.commit()


async def _contests(db, owner, teacher, students: list, rivals: list, rng: random.Random) -> None:
    now = datetime.now(timezone.utc)
    _, *classmates = students

    # Live EduHub-wide contest: rivals from both schools are already on the board; Liya can still join.
    live = await contest_service.create(
        db,
        owner,
        ContestCreate(
            title="EduHub Weekly Challenge #12",
            description="Maths, English and SAT. Open to every school. Top 3 get a shout-out!",
            starts_at=now - timedelta(minutes=40),
            ends_at=now + timedelta(hours=6),
            duration_minutes=10,
            question_count=8,
        ),
        platform_wide=True,
    )
    for s in [*classmates, *rivals]:
        await _compete(db, live, s, rng, skill=rng.uniform(0.4, 0.9))

    # A finished school contest Liya took part in; created live, then shifted into the past.
    ended = await contest_service.create(
        db,
        teacher,
        ContestCreate(
            title="Natural Science Sprint",
            description="Physics, chemistry and biology for the natural stream.",
            stream=Stream.natural,
            starts_at=now - timedelta(minutes=5),
            ends_at=now + timedelta(hours=2),
            duration_minutes=10,
            question_count=6,
        ),
    )
    for s in students:
        if s.stream == Stream.natural:
            await _compete(db, ended, s, rng, skill=rng.uniform(0.5, 0.9))
    shift = timedelta(days=4)
    ended.starts_at -= shift
    ended.ends_at = ended.starts_at + timedelta(hours=2)
    for e in await db.scalars(select(ContestEntry).where(ContestEntry.contest_id == ended.id)):
        e.started_at -= shift
        e.submitted_at = e.started_at + timedelta(seconds=e.time_taken_seconds)
    await db.commit()

    days_to_saturday = (5 - now.weekday()) % 7 or 7
    saturday = (now + timedelta(days=days_to_saturday)).replace(hour=15, minute=0, second=0, microsecond=0)
    await contest_service.create(
        db,
        teacher,
        ContestCreate(
            title="Saturday Maths Challenge",
            description="Algebra, geometry and calculus. 15 minutes, 10 questions.",
            subject="mathematics",
            starts_at=saturday,
            ends_at=saturday + timedelta(hours=3),
            duration_minutes=15,
            question_count=10,
        ),
    )


async def seed() -> None:
    if get_settings().environment == "production":
        raise SystemExit("Refusing to seed demo data in production")

    async with get_sessionmaker()() as db:
        if await db.scalar(select(Organization).where(Organization.name == ORG_NAME)):
            if await db.scalar(select(func.count()).select_from(Contest)):
                print("Demo data already present")
                return
            # Older demo databases predate contests: add them without touching existing data.
            by_email = {u.email: u for u in await db.scalars(select(User).where(User.email.like("%eduhub.dev")))}
            students = [by_email[STUDENT], *[by_email[f"student{i}@demo.eduhub.dev"] for i in range(2, 5)]]
            rivals = [by_email[f"student{i}@riverside.eduhub.dev"] for i in range(1, 5)]
            await _contests(db, by_email[PLATFORM_ADMIN], by_email[TEACHER], students, rivals, random.Random(7))
            print("Added demo contests to existing demo data")
            return

        owner = await user_service.create_platform_admin(db, PLATFORM_ADMIN, PASSWORD, "EduHub Owner")
        await quiz_service.create_library_questions(
            db, [_question(row, GRADE[row[1]], "EduHub practice set") for row in BANK], owner
        )
        await announcement_service.create(
            db,
            owner,
            AnnouncementCreate(title="Welcome to EduHub!", body="New: grade 9–12 practice sets are now in the shared library for every school.", pinned=True),
            platform_wide=True,
        )

        admin = await user_service.register_organization(
            db, OrganizationRegister(organization_name=ORG_NAME, full_name="Amara Okafor", email=ADMIN, password=PASSWORD)
        )
        org = admin.organization
        teacher = await user_service.register_student(
            db, StudentRegister(join_code=org.teacher_code, full_name="Daniel Mekonnen", email=TEACHER, password=PASSWORD)
        )
        student = await user_service.register_student(
            db, StudentRegister(join_code=org.join_code, full_name="Liya Tesfaye", email=STUDENT, password=PASSWORD, grade=12, stream=Stream.natural)
        )
        classmates = [
            await user_service.register_student(
                db,
                StudentRegister(join_code=org.join_code, full_name=name, email=f"student{i + 2}@demo.eduhub.dev", password=PASSWORD, grade=12, stream=stream),
            )
            for i, (name, stream) in enumerate([("Noah Kim", Stream.natural), ("Sara Haile", Stream.social), ("Yusuf Ali", Stream.social)])
        ]
        # Grade 10: the common curriculum, so no stream, and nothing above grade 10 is drawn.
        await user_service.register_student(
            db, StudentRegister(join_code=org.join_code, full_name="Abel Girma", email=GRADE10, password=PASSWORD, grade=10)
        )
        for row in SCHOOL_BANK:
            await quiz_service.create_question(db, _question(row, 10, None), teacher)
        await announcement_service.create(
            db, admin, AnnouncementCreate(title="Midterms start Monday", body="Mathematics and physics papers are first. Use adaptive practice this weekend!", pinned=True)
        )
        await announcement_service.create(
            db, teacher, AnnouncementCreate(title="New probability questions", body="I added a short probability set for grade 10. Try it from the Practice page.")
        )

        rng = random.Random(42)
        # Liya: a live 9-day streak plus scattered practice over ~5 months.
        await _history(db, student, rng, set(range(9)) | set(rng.sample(range(10, 150), 45)))
        for classmate in classmates:
            await _history(db, classmate, rng, set(rng.sample(range(0, 90), 12)))

        other_admin = await user_service.register_organization(
            db, OrganizationRegister(organization_name=OTHER_SCHOOL, full_name="Grace Mensah", email="admin@riverside.eduhub.dev", password=PASSWORD)
        )
        rivals = []
        for i in range(4):
            s = await user_service.register_student(
                db,
                StudentRegister(
                    join_code=other_admin.organization.join_code,
                    full_name=f"Riverside Student {i + 1}",
                    email=f"student{i + 1}@riverside.eduhub.dev",
                    password=PASSWORD,
                    grade=12,
                    stream=Stream.natural if i % 2 == 0 else Stream.social,
                ),
            )
            await _history(db, s, rng, set(rng.sample(range(0, 60), 6)))
            rivals.append(s)

        await _contests(db, owner, teacher, [student, *classmates], rivals, rng)

        print(f"Seeded {ORG_NAME} + {OTHER_SCHOOL}. Student {STUDENT} / {PASSWORD}; platform owner {PLATFORM_ADMIN}")

    await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(seed())
