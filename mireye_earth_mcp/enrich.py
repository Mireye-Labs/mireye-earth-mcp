"""Dependency-light mirror of the HTTP Enrich request, pinned by parity tests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._generated_presets import EnrichPresetName


class EnrichRequest(BaseModel):
    """Address XOR lat/lng; select individual Enrich fields and/or one preset.

    Omitted/null selectors retain the all-fields default. Preset expansion and
    field-ID validation happen before geocoding, parcel lookup or producers.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    lat: float | None = None
    lng: float | None = None
    address: str | None = Field(default=None, min_length=1, max_length=256)
    fields: list[str] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Individual Enrich field IDs, for example elevation or owner. "
            "Combined with preset by union; duplicates are returned once. "
            "Omit or use null for both selectors to request all Enrich fields."
        ),
    )
    preset: EnrichPresetName | None = Field(
        default=None,
        description="One named Enrich bundle, expanded before any additional fields.",
    )

    @model_validator(mode="after")
    def _validate_enrich_location(self) -> EnrichRequest:
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be provided together")
        if (self.lat is not None) == (self.address is not None):
            raise ValueError("Provide either lat+lng or address, not both.")
        if self.address is not None:
            if not self.address.strip():
                raise ValueError("address must not be blank")
        else:
            assert self.lat is not None and self.lng is not None
            # Mirrors validate_us_coordinate, including western Aleutians.
            legacy_envelope = -180 <= self.lng <= -65 and 18 <= self.lat <= 72
            western_aleutians = 172 <= self.lng < 180 and 51 <= self.lat <= 54
            if not (legacy_envelope or western_aleutians):
                raise ValueError(
                    f"V1 supports US coordinates only; received lat={self.lat}, lng={self.lng}"
                )
        return self
