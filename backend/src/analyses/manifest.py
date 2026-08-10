"""Strict versioned models for the static analysis manifest and metadata."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from analyses.validation import (
    normalize_in_cluster_amqp_hosts,
    normalize_label,
    validate_analysis_id,
)


class _ManifestModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class StaticClusterCapture(_ManifestModel):
    """The single cluster capture referenced by one v1 analysis."""

    definitions_path: str
    queues_path: str | None = None
    exchanges_path: str | None = None
    in_cluster_amqp_hosts: tuple[str, ...] = ()

    @field_validator("definitions_path", "queues_path", "exchanges_path")
    @classmethod
    def validate_nonempty_path(cls, value: str | None) -> str | None:
        """Reject empty path strings before filesystem confinement checks."""
        if value is not None and not value:
            raise ValueError("capture paths must not be empty")
        return value

    @field_validator("in_cluster_amqp_hosts")
    @classmethod
    def normalize_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Store host evidence in its topology-affecting canonical form."""
        return normalize_in_cluster_amqp_hosts(value)

    @model_validator(mode="after")
    def validate_observation_pair(self) -> Self:
        """Require queue and exchange observations together or not at all."""
        if (self.queues_path is None) != (self.exchanges_path is None):
            raise ValueError("queuesPath and exchangesPath must be supplied together")
        return self


class StaticAnalysisManifestEntry(_ManifestModel):
    """One selectable analysis declared by the static manifest."""

    analysis_id: str
    label: str
    clusters: Annotated[tuple[StaticClusterCapture, ...], Field(min_length=1, max_length=1)]
    metadata_path: str | None = None

    _validate_analysis_id = field_validator("analysis_id")(validate_analysis_id)
    _normalize_label = field_validator("label")(normalize_label)

    @field_validator("metadata_path")
    @classmethod
    def validate_metadata_path(cls, value: str | None) -> str | None:
        """Reject an empty optional sidecar path."""
        if value == "":
            raise ValueError("metadataPath must not be empty")
        return value


class StaticAnalysisManifest(_ManifestModel):
    """Root manifest loaded from the configured static definitions directory."""

    schema_version: Literal[1]
    analyses: tuple[StaticAnalysisManifestEntry, ...]

    @model_validator(mode="after")
    def validate_unique_analysis_ids(self) -> Self:
        """Reject duplicate catalogue keys at startup."""
        analysis_ids = [analysis.analysis_id for analysis in self.analyses]
        if len(analysis_ids) != len(set(analysis_ids)):
            raise ValueError("manifest analysisId values must be unique")
        return self


class StaticAnalysisMetadata(_ManifestModel):
    """Optional presentation-only metadata sidecar."""

    schema_version: Literal[1]
    description: str | None = None
    owner_contact: str | None = None
