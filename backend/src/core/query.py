"""Strict snake-case query contracts shared by product API routes."""

from typing import Annotated

from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field


class StrictQuery(BaseModel):
    """Reject undeclared query fields; the bare model accepts no query fields."""

    model_config = ConfigDict(extra="forbid")


NoQuery = Annotated[StrictQuery, Query()]


def reject_query_fields(request: Request) -> None:
    """Reject a query after a route's explicit authentication/owner preflight."""
    if request.query_params:
        raise RequestValidationError(
            [{"loc": ("query",), "type": "extra_forbidden", "input": None}],
        )


class SnapshotRevisionQuery(StrictQuery):
    """Require one exact snapshot revision for a snapshot-scoped request."""

    snapshot_revision: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
