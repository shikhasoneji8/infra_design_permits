"""The site plan the designer produces. Pure data, validated with pydantic."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import config as C
from .geo import rect

Kind = Literal["data_hall", "generator", "cooling", "substation", "parking", "stormwater_basin"]


class PlacedObject(BaseModel):
    id: str
    kind: Kind
    x: float = Field(description="centre x, metres east of parcel centroid")
    y: float = Field(description="centre y, metres north of parcel centroid")
    w: float = Field(gt=0, description="width along x, metres")
    l: float = Field(gt=0, description="length along y, metres")
    h: float = Field(ge=0, description="height, metres")
    rotation_deg: float = 0.0

    def footprint(self):
        return rect(self.x, self.y, self.w, self.l, self.rotation_deg)


class Plan(BaseModel):
    it_mw: float = Field(default=C.TARGET_IT_MW, gt=0)
    cooling_type: Literal["air_cooled", "evaporative"] = "evaporative"
    cooling_noise: Literal["standard", "low_noise"] = "standard"
    cooling_barrier: bool = Field(default=False, description="acoustic screen wall around the cooling yard (-8 dB)")
    generator_tier: Literal["tier2", "tier4f"] = "tier2"
    generator_enclosure: Literal["standard", "critical_silenced"] = "standard"
    bess_mw: float = Field(default=0.0, ge=0, description="battery storage replacing diesel gensets, MW")
    water_source: Literal["municipal", "well"] = "well"
    objects: list[PlacedObject]
    rationale: str = ""

    @field_validator("objects")
    @classmethod
    def _unique_ids(cls, v):
        ids = [o.id for o in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate object ids")
        return v

    # ---- what the equipment list demands, given the knobs ------------------
    @property
    def generators_required(self) -> int:
        critical_mw = self.it_mw * 0.5  # our assumption: half the IT load is on diesel, rest on BESS/utility
        return max(0, math.ceil((critical_mw - self.bess_mw) / (C.GENERATOR_KW / 1000)))

    def by_kind(self, kind: str) -> list[PlacedObject]:
        return [o for o in self.objects if o.kind == kind]

    def compact(self) -> dict:
        return self.model_dump()


def required_counts(plan: Plan) -> dict[str, int]:
    counts = {e.kind: e.count for e in C.EQUIPMENT}
    counts["generator"] = plan.generators_required
    return counts
