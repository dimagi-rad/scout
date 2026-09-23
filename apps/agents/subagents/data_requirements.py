"""Bounded, provider-neutral proposals; never authorization to change a model."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class DataRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["dimension", "measure", "dataset", "relationship"]
    need: Description
    source_datasets: list[Name] = Field(min_length=1, max_length=4)
    source_members: list[Name] = Field(max_length=12)
    grain: Description
    decisions: list[Description] = Field(max_length=4)


DATA_REQUIREMENTS = TypeAdapter(Annotated[list[DataRequirement], Field(min_length=1, max_length=8)])


def validate_data_requirements(value: object) -> list[dict]:
    return [item.model_dump() for item in DATA_REQUIREMENTS.validate_python(value)]
