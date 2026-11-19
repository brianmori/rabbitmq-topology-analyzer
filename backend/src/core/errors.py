"""Common safe application-error envelope and exception handlers."""

import logging
import re
from typing import TYPE_CHECKING

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.exceptions import HTTPException

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fastapi import FastAPI, Request
    from fastapi.dependencies.models import Dependant
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOGGER = logging.getLogger(__name__)
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

_CODE_MESSAGES: Mapping[str, str] = {
    "analysis_capacity_unavailable": "Analysis capacity is temporarily unavailable.",
    "analysis_has_active_work": "The analysis has active work and cannot be changed.",
    "authentication_capacity_unavailable": "Authentication capacity is temporarily unavailable.",
    "authentication_failed": "Authentication could not be completed.",
    "capture_upload_too_large": "The combined capture files exceed the configured limit.",
    "cycle_run_not_succeeded": "Cycle results are available only after a successful run.",
    "idempotency_key_reused": "The idempotency key was already used for another request.",
    "owner_has_active_run": "This browser session already has an active analysis run.",
    "result_storage_full": "The completed-result storage quota is full.",
    "session_analysis_limit_reached": "This browser session reached its retained-capture limit.",
    "session_store_capacity_reached": "The service reached its retained-capture capacity.",
    "snapshot_revision_mismatch": "The selected snapshot revision is no longer current.",
    "upload_request_cancelled": "Capture processing was cancelled.",
}

_STATUS_ERRORS: Mapping[int, tuple[str, str]] = {
    400: ("invalid_request", "The request is invalid."),
    401: ("unauthorized", "Authentication is required."),
    403: ("forbidden", "The current principal cannot perform this action."),
    404: ("not_found", "The requested resource was not found."),
    409: ("conflict", "The request conflicts with current application state."),
    413: ("payload_too_large", "The request payload exceeds the configured limit."),
    422: ("validation_failed", "Request validation failed."),
    499: ("request_cancelled", "The request was cancelled."),
    500: ("internal_error", "The request could not be completed."),
    503: ("service_unavailable", "The service is temporarily unavailable."),
}

_DETAIL_CODES: Mapping[str, str] = {
    "Analysis not found.": "not_found",
    "Run not found.": "not_found",
    "Cycle not found.": "not_found",
    "Uploaded files are not a valid RabbitMQ capture.": "invalid_capture",
    "The uploaded capture could not be processed.": "capture_processing_failed",
    "The completed SCC result is unavailable.": "result_unavailable",
    "The completed cycle result is unavailable.": "result_unavailable",
    "X-Session-Token header is required for session operations.": "session_token_required",
    "X-Session-Token is required for a session analysis.": "session_token_required",
    "X-Session-Token must be a UUIDv4.": "invalid_session_token",
    "X-Session-Token must be a canonical UUIDv4.": "invalid_session_token",
    "X-Session-Token header must appear only once.": "invalid_session_token",
}

_DETAIL_MESSAGES: Mapping[str, str] = {
    "invalid_capture": "The uploaded files are not a valid RabbitMQ capture.",
    "capture_processing_failed": "The uploaded capture could not be processed.",
    "result_unavailable": "The completed analysis result is unavailable.",
    "session_token_required": "A browser-session identifier is required for this request.",
    "invalid_session_token": "The browser-session identifier must be a canonical UUIDv4.",
}


def _status_error(status_code: int) -> tuple[str, str]:
    return _STATUS_ERRORS.get(
        status_code, ("request_failed", "The request could not be completed.")
    )


def error_content(
    code: str,
    message: str,
    issues: Sequence[Mapping[str, str]] = (),
) -> dict[str, object]:
    """Build the canonical safe application-error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "issues": list(issues),
        },
    }


def _http_error(exc: HTTPException) -> tuple[str, str]:
    fallback_code, fallback_message = _status_error(exc.status_code)
    if not isinstance(exc.detail, str):
        return fallback_code, fallback_message
    code = _DETAIL_CODES.get(exc.detail)
    if code is None and _CODE_PATTERN.fullmatch(exc.detail) and exc.detail in _CODE_MESSAGES:
        code = exc.detail
    if code is None:
        return fallback_code, fallback_message
    return code, _CODE_MESSAGES.get(code, _DETAIL_MESSAGES.get(code, fallback_message))


def _validation_fields(request: Request) -> dict[str, set[str]]:
    """Get public input names from the matched route and its dependencies."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return {}

    fields: dict[str, set[str]] = {}

    def collect(dependant: Dependant) -> None:
        for source, params in (
            ("body", dependant.body_params),
            ("query", dependant.query_params),
            ("path", dependant.path_params),
            ("header", dependant.header_params),
            ("cookie", dependant.cookie_params),
        ):
            names = fields.setdefault(source, set())
            for param in params:
                annotation = param.field_info.annotation
                if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                    for name, model_field in annotation.model_fields.items():
                        names.add(name)
                        for alias in (model_field.alias, model_field.validation_alias):
                            if isinstance(alias, str):
                                names.add(alias)
                else:
                    names.add(param.alias)
        for dependency in dependant.dependencies:
            collect(dependency)

    collect(route.dependant)
    return fields


def _validation_issues(request: Request, exc: RequestValidationError) -> tuple[dict[str, str], ...]:
    allowed_fields = _validation_fields(request)
    issues: list[dict[str, str]] = []
    for error in exc.errors():
        location = error.get("loc", ())
        match location:
            case [str(source), str(name), *_] if name in allowed_fields.get(source, ()):
                field = name
            case [str(source), *_] if source in allowed_fields:
                field = source
            case _:
                field = "request"
        raw_code = str(error.get("type", "invalid"))
        code = raw_code if _CODE_PATTERN.fullmatch(raw_code) else "invalid"
        message = {
            "missing": "This field is required.",
            "extra_forbidden": "Unknown field.",
            "duplicate_field": "This field may appear only once.",
        }.get(code, "The value is invalid.")
        issues.append(
            {
                "field": field,
                "code": code,
                "message": message,
            },
        )
    return tuple(issues)


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise AssertionError("HTTP handler received another exception type")
    code, message = _http_error(exc)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_content(code, message),
        headers=exc.headers,
    )


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise AssertionError("validation handler received another exception type")
    return JSONResponse(
        status_code=422,
        content=error_content(
            "validation_failed",
            "Request validation failed.",
            _validation_issues(request, exc),
        ),
    )


class SafeExceptionMiddleware:
    """Contain unexpected request errors before Starlette re-raises them to Uvicorn."""

    def __init__(self, app: ASGIApp) -> None:
        """Retain the inner ASGI application."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send one safe error if possible, without logging exception data."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False

        async def send_with_state(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, receive, send_with_state)
        except Exception as exc:
            # SDK failures can contain submitted definitions, authorities, or
            # credentials in both exception text and chained causes.
            _LOGGER.error("Unhandled application exception type=%s", type(exc).__name__)
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content=error_content("internal_error", "The request could not be completed."),
                )
                await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    """Install the canonical envelope for validation, route, and unexpected errors."""
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_middleware(SafeExceptionMiddleware)
