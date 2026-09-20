"""Error envelope (LLD §9): {"error": {code, message, details?}}."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details),
    )


def not_found(message: str = "resource not found") -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def forbidden(message: str = "access denied") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def unauthorized(message: str = "invalid or missing credentials") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message)


def bad_request(code: str, message: str, details: dict | None = None) -> ApiError:
    return ApiError(400, code, message, details)
