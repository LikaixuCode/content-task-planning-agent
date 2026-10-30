from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class PlanningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    request_id: str = Field(min_length=1, max_length=120)
    topic: str = Field(min_length=1, max_length=200)
    budget: Decimal = Field(ge=0)
    deadline: AwareDatetime
    start_at: AwareDatetime | None = None

    @field_validator("request_id", "topic", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    def canonical(self) -> str:
        import json

        value = self.model_dump(mode="json")
        value["budget"] = format(self.budget.normalize(), "f")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Material(BaseModel):
    id: str
    title: str
    description: str
    keywords: list[str]


class CostItem(BaseModel):
    name: str
    cost: Decimal = Field(ge=0)
    duration_minutes: int = Field(gt=0)


class Catalog(BaseModel):
    material: CostItem
    variants: dict[str, CostItem]
    package: CostItem
