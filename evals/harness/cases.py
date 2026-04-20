"""YAML case definitions for eval runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Rubric:
    """Scoring criteria for a single case."""
    expected_tools: list[str] = field(default_factory=list)
    """Tool names that MUST appear in the tool-call sequence (any order)."""

    expected_tools_any_of: list[str] = field(default_factory=list)
    """At least ONE of these tool names must appear."""

    forbidden_tools: list[str] = field(default_factory=list)
    """Tool names that MUST NOT appear."""

    content_contains: list[str] = field(default_factory=list)
    """Strings that must appear in the assistant's final text content."""

    content_must_not_contain: list[str] = field(default_factory=list)
    """Strings that must NOT appear in the final content (e.g. leaked sandbox_root)."""

    first_tool_one_of: list[str] = field(default_factory=list)
    """The FIRST tool call (if any) must be one of these — catches idiom choice."""


@dataclass
class Case:
    name: str
    prompt: str
    rubric: Rubric
    system_prompt: str | None = None
    tags: list[str] = field(default_factory=list)
    max_tokens: int = 1024
    temperature: float | None = None


def load_case(path: Path) -> Case:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rubric_data = data.pop("rubric", None) or {}
    rubric = Rubric(**rubric_data)
    name = data.pop("name", path.stem)
    return Case(name=name, rubric=rubric, **data)


def load_cases(cases_dir: Path) -> list[Case]:
    cases: list[Case] = []
    for p in sorted(cases_dir.glob("*.yaml")):
        cases.append(load_case(p))
    return cases
