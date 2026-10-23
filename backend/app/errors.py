"""Единый формат ошибок: {"error": {"code", "message", "details"}}.

Коды ошибок перечислены в docs/CONTRACT.md.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class UnprocessableError(AppError):
    status_code = 422
    code = "VALIDATION_ERROR"


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"


# Коды для ошибок, которые поднимает сам FastAPI/Starlette.
_HTTP_CODES = {
    400: ("BAD_REQUEST", "Некорректный запрос"),
    401: ("UNAUTHORIZED", "Требуется авторизация"),
    404: ("NOT_FOUND", "Ресурс не найден"),
    405: ("METHOD_NOT_ALLOWED", "Метод не поддерживается"),
    413: ("FILE_TOO_LARGE", "Слишком большой запрос"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Неподдерживаемый тип данных"),
}


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(error_body(exc.code, exc.message, exc.details), exc.status_code)


async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "Ошибка запроса"))
    if isinstance(exc.detail, str) and exc.detail and exc.status_code not in (404, 405):
        message = exc.detail
    return JSONResponse(error_body(code, message), exc.status_code, headers=exc.headers)


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Только loc/msg/type: в input и ctx бывают несериализуемые объекты.
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    body = error_body("VALIDATION_ERROR", "Данные запроса не прошли проверку", {"errors": errors})
    return JSONResponse(body, 422)


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    return JSONResponse(error_body("INTERNAL_ERROR", "Внутренняя ошибка сервера"), 500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled_error)
