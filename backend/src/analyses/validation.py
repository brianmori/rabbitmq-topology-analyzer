"""Validation shared by manifest and multipart analysis identifiers."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

_ANALYSIS_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_MAX_LABEL_LENGTH = 120


def validate_analysis_id(value: str) -> str:
    """Require the approved lowercase ASCII analysis slug."""
    if _ANALYSIS_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("analysis_id must be a lowercase ASCII slug of 1 to 64 characters")
    return value


def normalize_label(value: str) -> str:
    """Trim and validate a human-readable analysis label."""
    normalized = value.strip()
    if not 1 <= len(normalized) <= _MAX_LABEL_LENGTH:
        raise ValueError("label must contain 1 to 120 characters after trimming")
    return normalized


def normalize_in_cluster_amqp_hosts(values: Collection[str]) -> tuple[str, ...]:
    """Normalize, deduplicate, and order local AMQP host evidence."""
    normalized = {value.strip().casefold() for value in values}
    if "" in normalized:
        raise ValueError("in-cluster AMQP hosts must not be empty")
    return tuple(sorted(normalized))
