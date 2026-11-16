"""Shared base model for API response schemas.

Every domain's response models should inherit from ``CamelModel`` instead of
``pydantic.BaseModel`` directly. This keeps Python field names snake_case
(idiomatic Python) while JSON output is camelCase (idiomatic TypeScript) --
so ``is_session`` in Python becomes ``isSession`` on the wire, matching the
frontend's ``types/topology.ts`` interfaces without hand-written aliases on
every field.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model that serializes fields as camelCase JSON.

    ``populate_by_name=True`` means the Python side can still be constructed
    with snake_case keyword arguments (e.g. ``AnalysisSummary(analysis_id=...)``)
    even though the wire format is camelCase.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
