"""Search's wire contract and configurable work ceilings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SearchFilter(SearchModel):
    field: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "between", "intersects", "within"]
    value: JsonValue

    @model_validator(mode="after")
    def validate_location(self) -> SearchFilter:
        if self.field in {"state", "state2"}:
            if self.op not in {"eq", "ne", "in"}:
                raise ValueError("state supports eq, ne, or in with two-letter US state codes")
            values = self.value if self.op == "in" else [self.value]
            if not isinstance(values, list) or not values:
                raise ValueError("state in requires a nonempty list of two-letter US state codes")
            if any(not isinstance(v, str) or v.strip().upper() not in _STATES for v in values):
                raise ValueError("state must be a two-letter US state code (50 states and DC)")
            normalized: list[JsonValue] = [str(v).strip().upper() for v in values]
            self.value = normalized if self.op == "in" else normalized[0]
        if self.field in {"county", "city"}:
            if self.op not in {"eq", "ne", "in"}:
                raise ValueError("county and city support eq, ne, or in")
            values = self.value if self.op == "in" else [self.value]
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
            ):
                raise ValueError("county and city require nonblank strings")
        return self


class SearchRequest(SearchModel):
    filters: list[SearchFilter] = Field(
        min_length=1,
        description=(
            "All conditions are required; source routing is automatic. Include state (or state2) "
            "with eq or a nonempty in list of two-letter US state codes (50 states and DC). "
            "A ne filter alone does not select a state. County is optional. Only fields in "
            "the Search filter catalog may be filters; output extras do not expand that catalog."
        ),
    )
    limit: int = Field(
        default=10,
        ge=1,
        strict=True,
        description="Parcels on this page, from 1 to 100; may change between cursor requests.",
    )
    enrich: bool = Field(
        default=False, strict=True,
        description=(
            "False returns identity plus plain fields for filters and requested extras. True "
            "attempts the full current Enrich catalog for each match, regardless of fields, "
            "and returns direct compact cells in enriched_fields instead of fields. Failed "
            "fields and most null values are omitted under the standalone Enrich policy."
        ),
    )
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    fields: list[Annotated[str, Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")]] = Field(
        default_factory=list,
        description=(
            "Output-only extras from any public Search or Enrich field; never used to filter "
            "parcels. With enrich false, fields contains filter values and these extras, "
            "excluding top-level identity duplicates; missing or failed extras are null. "
            "Extras are included in normal Search pricing. With enrich true, fields does "
            "not select an enrichment subset: the full catalog is attempted."
        ),
    )

    @field_validator("filters")
    @classmethod
    def require_state(cls, filters: list[SearchFilter]) -> list[SearchFilter]:
        if not any(f.field in {"state", "state2"} and f.op in {"eq", "in"} for f in filters):
            raise ValueError(
                "Include a state (or state2) filter with eq or a nonempty in list of "
                "two-letter US state codes (50 states and DC); ne alone is not sufficient."
            )
        return filters
