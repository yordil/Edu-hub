# EduHub API

**EduHub**, an adaptive exam-preparation platform for Ethiopian secondary schools (grades 9–12), built around the grade 12 national entrance exam.

Each school is an isolated tenant. Students practise against a question bank that adapts to them, sit real past papers as timed exams, compete in live contests, and get help from an AI tutor grounded in their own results. Teachers see where every student is struggling, and schools pay through Ethiopia's local payment rails.

**FastAPI · PostgreSQL + pgvector · Redis · SQLAlchemy 2 (async) · Alembic · Amazon Bedrock**

## What it delivers

| Benefit | How |
|---|---|
| **Practice at exactly the right level** | A Rasch item-response model learns each student's ability *and* each question's difficulty from every answer, aiming at ~70% success |
| **Only material students have been taught** | A grade ceiling: grade 9 sees grade 9, grade 12 sees the whole entrance-exam syllabus |
| **Help about a student's own results** | An AI tutor whose tools read their real mastery and mistakes, streamed word by word |
| **Safe for minors** | Checks before and after the model; self-harm and off-curriculum requests never reach it |
| **Old exam papers become a question bank** | PDF upload with OCR, a second AI pass, and a teacher approving every question |
| **AI costs that can't run away** | Per-school budgets, per-student limits, a one-time free trial, usage recorded per call |
| **Payments that can't be forged or double-counted** | Webhooks re-verified with Chapa, fail-closed, row-locked |
| **Schools never see each other's data** | Every row belongs to a school; every query filters on it |

## Architecture

- **API** (FastAPI) serves requests, streams tutor replies over SSE, and pushes live leaderboards over WebSocket.
- **Worker** parses uploaded papers, the only CPU-heavy work, off the request path.
- **PostgreSQL** holds all state, including the job queue and vector embeddings, so restarting anything loses nothing.
- **Redis** is optional and holds only what can be rebuilt: leaderboards and rate limits.
- **Bedrock** runs the AI; **S3** (local disk in development) stores uploads.

## Design decisions

**Access control as dependencies.** Each route declares who may call it (`CurrentUser`, `StaffUser`, `AdminUser`, `PlatformAdmin`, `TutorUser`) in its signature. The user is reloaded on every request, so a demotion takes effect immediately. Non-operators get **404** from operator routes, so the console stays invisible.

**Grade and stream are separate questions.** Grade sets the *level* (a ceiling, because the exam spans grades 9–12); stream sets the *subjects*, from grade 11. Hiding above-grade content also protects the model: a grade 9 student missing grade 12 material would make that question look harder for everyone. The rule is enforced three times — signup validation, profile updates, and Postgres `CHECK` constraints.

**A grounded, bounded tutor.** An agent loop with five tools that read only the asking student's data (`get_mastery`, `get_recent_mistakes`, `recommend_questions`, `find_study_material` for RAG, `remember` for long-term memory).
- **Safety:** input checks for PII (redacted), injection, self-harm and off-curriculum requests; output checks for prompt leaks.
- **Scope:** "python" is also a snake, so the code filter fires only when a language and a code word appear together; the system prompt catches the rest.
- **Streaming:** the stream holds back its last 40 characters, so a leaked instruction is caught before any of it reaches the screen. Usage is committed after every model call, so closing the tab mid-answer can't dodge the budget.

**The table is the queue.** `POST /papers` validates, stores the file and returns **202**; a worker claims it with `SELECT … FOR UPDATE SKIP LOCKED`. Inline parsing would let a few simultaneous uploads exhaust the web process's threads; the queue is a bulkhead.
- Any number of workers run side by side without double-processing; crashed jobs are reclaimed, then failed after three tries.
- A partial index keeps claims cheap however many papers accumulate.
- It replaced arq, which silently downgraded the Redis client from 8.x to 5.3 — and durability already came from Postgres.

**AI may flag, never decide.** A second model pass solves each question blind; disagreements go to teachers to confirm or correct. Extracted text runs through the safety filter and is *flagged*, not rejected. Nothing publishes itself, and publishing is the moment the audit log records.

**Payments trust nothing they can verify.** The webhook body only names a transaction; its status is read back from Chapa, so even a validly signed forgery grants nothing. A missing signature is a rejection, never a bypass. A row lock makes the redirect and the webhook idempotent, and the paid amount must match. Only features with real running cost are Pro; tutor history stays readable after a plan lapses.

**Live leaderboards degrade, never fail.** Redis sorted sets plus pub/sub push updates over WebSocket, authenticated with a 60-second ticket scoped to one contest. Browsers can't send auth headers on a WebSocket upgrade. Without Redis, clients fall back to polling.

**Password reset without a token table.** A signed JWT carries a fingerprint of the current password hash, so changing the password kills every outstanding link. Unknown emails get the same response, and rate limits count every request, so nothing reveals which accounts exist. The token-minting route answers only the web app's internal key.

## How it's tested

**357 integration tests** against a real Postgres in Testcontainers, not mocks, and a scripted fake model that makes agent behaviour deterministic and free.
- A drift check fails the build if models and migrations disagree.
- Six workers race for three papers, and each is claimed exactly once.
- A validly signed webhook claiming a payment Chapa denies unlocks nothing.
- A leaked system prompt never reaches the screen, not even its first letters.
- Impossible grade/stream pairs written straight into Postgres are refused.
- 29 golden safety cases measure both what's blocked and what's wrongly blocked.

**Bugs the tests caught:** reordering a question's choices silently pointed its answer key at the wrong option (the key now follows the choice's text), and `"" in "ABCD"` being `True` recorded phantom AI verdicts.
