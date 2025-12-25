from fastapi import APIRouter

from app.api.routes import (
    announcements,
    attempts,
    auth,
    billing,
    contests,
    health,
    learning,
    organizations,
    papers,
    platform,
    questions,
    tutor,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(organizations.router)
api_router.include_router(questions.router)
api_router.include_router(attempts.router)
api_router.include_router(learning.router)
api_router.include_router(announcements.router)
api_router.include_router(platform.router)
api_router.include_router(tutor.router)
api_router.include_router(contests.router)
api_router.include_router(contests.leaderboard_router)
api_router.include_router(contests.classroom_router)
api_router.include_router(papers.router)
api_router.include_router(billing.router)

health_router = health.router
