"""Pydantic models for every structured response we ask Gemini for.

Every model here is validated before use. If validation fails after retries the
caller falls back to needs_review rather than guessing.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MatchResult(BaseModel):
    match_score: int = Field(ge=0, le=100)
    best_resume: str
    reason: str = ""
    red_flags: list[str] = Field(default_factory=list)

    @field_validator("red_flags", mode="before")
    @classmethod
    def _listify(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v


class AnswerResult(BaseModel):
    """Answer to one screening question.

    kind tells the filler how to act:
      text     -> type `value`
      number   -> type `value` (digits only)
      select   -> pick the option equal to `value`
      radio    -> pick the option equal to `value`
      checkbox -> tick every option in `values`
      skip     -> leave the field alone
    """

    kind: Literal["text", "number", "select", "radio", "checkbox", "skip"]
    value: str = ""
    values: list[str] = Field(default_factory=list)
    confident: bool = True
    note: str = ""

    @field_validator("values", mode="before")
    @classmethod
    def _listify(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return v

    @field_validator("value", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> object:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return str(v)


class CoverLetter(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty cover letter")
        return v.strip()


class EmailDraft(BaseModel):
    subject: str
    body: str

    @field_validator("subject", "body")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty field")
        return v.strip()


class FollowUp(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty follow-up")
        return v.strip()
