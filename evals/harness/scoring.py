"""Rubric evaluation: compare a result (or result-shaped shim) to a Case.Rubric.

The result only needs .content (str), .tool_calls (iterable of objects with .name),
and .finish_reason (str). Kept generic so both the old ChatResult and the new
AgenticResult (via a shim) can flow through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from evals.harness.cases import Rubric


@dataclass
class Verdict:
    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def score(rubric: Rubric, result: Any) -> Verdict:
    failures: list[str] = []
    notes: list[str] = []
    called = [tc.name for tc in result.tool_calls]
    notes.append(f"tool_calls: {called or '[]'}")
    notes.append(f"finish_reason: {result.finish_reason}")

    for req in rubric.expected_tools:
        if req not in called:
            failures.append(f"expected_tools: missing {req!r}; got {called}")

    if rubric.expected_tools_any_of:
        if not any(t in called for t in rubric.expected_tools_any_of):
            failures.append(
                f"expected_tools_any_of: none of {rubric.expected_tools_any_of} appeared; got {called}"
            )

    for forbid in rubric.forbidden_tools:
        if forbid in called:
            failures.append(f"forbidden_tools: {forbid!r} was called")

    if rubric.first_tool_one_of:
        first = called[0] if called else None
        if first not in rubric.first_tool_one_of:
            failures.append(
                f"first_tool_one_of: first call was {first!r}; expected one of {rubric.first_tool_one_of}"
            )

    content = result.content or ""
    for needle in rubric.content_contains:
        if needle not in content:
            failures.append(f"content_contains: {needle!r} not in content")

    for forbid in rubric.content_must_not_contain:
        if forbid in content:
            failures.append(f"content_must_not_contain: {forbid!r} appeared in content")

    content_lower = content.lower()
    for needle in rubric.content_contains_ci:
        if needle.lower() not in content_lower:
            failures.append(f"content_contains_ci: {needle!r} not in content (case-insensitive)")

    for forbid in rubric.content_must_not_contain_ci:
        if forbid.lower() in content_lower:
            failures.append(f"content_must_not_contain_ci: {forbid!r} appeared in content (case-insensitive)")

    return Verdict(passed=not failures, failures=failures, notes=notes)
