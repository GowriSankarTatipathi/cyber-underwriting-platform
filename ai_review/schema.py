from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

FactField = Literal["restore_duration_minutes", "recovered_data_age_minutes", "functional_checks_passed", "service", "scope", "test_date", "backup_status", "mfa_coverage", "access_review_date"]


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passage_id: str = Field(max_length=40)
    quote: str = Field(min_length=1, max_length=1600)


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FactField
    value: str = Field(min_length=1, max_length=1200)
    citations: list[Citation] = Field(min_length=1, max_length=4)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[Fact] = Field(max_length=50)
    warnings: list[str] = Field(max_length=12)
