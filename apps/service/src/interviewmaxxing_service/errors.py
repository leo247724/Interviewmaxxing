"""Structured errors returned as ``{"error": {"code", "message", "fieldErrors"?}}``.

Codes are the frontend's ``ServiceErrorCode`` values so its HTTP client can map them
without guessing: ``unavailable``, ``invalid``, ``not_found``, ``conflict``, ``unknown``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

ErrorCode = Literal["unavailable", "invalid", "not_found", "conflict", "unknown"]


class ApiError(Exception):
    """An error the HTTP layer turns into a structured JSON response."""

    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str,
        field_errors: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code: ErrorCode = code
        self.message = message
        self.field_errors = dict(field_errors or {})

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field_errors:
            error["fieldErrors"] = self.field_errors
        return {"error": error}


def invalid(message: str, field_errors: Mapping[str, str] | None = None) -> ApiError:
    return ApiError(422 if field_errors else 400, "invalid", message, field_errors)


def not_found(message: str) -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str, field_errors: Mapping[str, str] | None = None) -> ApiError:
    return ApiError(409, "conflict", message, field_errors)


def forbidden(message: str) -> ApiError:
    # 403 with the frontend's "invalid" code: the request itself is not acceptable.
    return ApiError(403, "invalid", message)


def unavailable(message: str) -> ApiError:
    return ApiError(503, "unavailable", message)
