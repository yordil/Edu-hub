from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status_code = 404


class ForbiddenError(DomainError):
    status_code = 403


class ConflictError(DomainError):
    status_code = 409


class ValidationFailed(DomainError):
    status_code = 400


class TooManyRequests(DomainError):
    status_code = 429


class ServiceUnavailable(DomainError):
    status_code = 503


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
