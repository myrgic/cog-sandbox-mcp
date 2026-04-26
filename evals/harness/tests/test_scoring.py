"""Tests for evals.harness.scoring — focuses on new ANY_OF rubric primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from evals.harness.cases import Rubric
from evals.harness.scoring import score


# ---------------------------------------------------------------------------
# Minimal result shim (same shape scorer expects)
# ---------------------------------------------------------------------------

@dataclass
class _FakeToolCall:
    name: str
    arguments: dict = field(default_factory=dict)
    result: str = ""


@dataclass
class _FakeResult:
    content: str = ""
    tool_calls: list[_FakeToolCall] = field(default_factory=list)
    finish_reason: str = "stop"


# ---------------------------------------------------------------------------
# content_contains_any_of_ci — ANY_OF (case-insensitive)
# ---------------------------------------------------------------------------

class TestContentContainsAnyOfCi:
    def test_passes_when_first_needle_matches_exact(self):
        rubric = Rubric(content_contains_any_of_ci=["error", "invalid"])
        result = _FakeResult(content="An error occurred.")
        v = score(rubric, result)
        assert v.passed

    def test_passes_when_second_needle_matches(self):
        rubric = Rubric(content_contains_any_of_ci=["error", "invalid"])
        result = _FakeResult(content="The URI is invalid.")
        v = score(rubric, result)
        assert v.passed

    def test_passes_case_insensitive_uppercase(self):
        rubric = Rubric(content_contains_any_of_ci=["trust", "score"])
        result = _FakeResult(content="Trust Score: 1")
        v = score(rubric, result)
        assert v.passed

    def test_passes_case_insensitive_mixed(self):
        rubric = Rubric(content_contains_any_of_ci=["trust", "score"])
        result = _FakeResult(content="TRUST SCORE is 1")
        v = score(rubric, result)
        assert v.passed

    def test_fails_when_no_needle_matches(self):
        rubric = Rubric(content_contains_any_of_ci=["error", "invalid", "not valid"])
        result = _FakeResult(content="The operation completed successfully.")
        v = score(rubric, result)
        assert not v.passed
        assert any("content_contains_any_of_ci" in f for f in v.failures)

    def test_passes_with_recognized_needle(self):
        """'recognized' appears in kernel response 'not a recognized CogDoc type'."""
        rubric = Rubric(content_contains_any_of_ci=["error", "failed", "recognized", "invalid"])
        result = _FakeResult(content="adrs is not a recognized or valid URI type")
        v = score(rubric, result)
        assert v.passed

    def test_empty_list_does_not_add_failure(self):
        """Empty content_contains_any_of_ci is a no-op — should not fail."""
        rubric = Rubric(content_contains_any_of_ci=[])
        result = _FakeResult(content="anything")
        v = score(rubric, result)
        assert v.passed

    def test_failure_message_lists_candidates(self):
        rubric = Rubric(content_contains_any_of_ci=["alpha", "beta"])
        result = _FakeResult(content="gamma delta")
        v = score(rubric, result)
        assert not v.passed
        assert len(v.failures) == 1
        assert "alpha" in v.failures[0] or "beta" in v.failures[0]


# ---------------------------------------------------------------------------
# expected_tools_any_of — already shipped; regression test
# ---------------------------------------------------------------------------

class TestExpectedToolsAnyOf:
    def test_passes_when_one_tool_matches(self):
        rubric = Rubric(expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"])
        result = _FakeResult(tool_calls=[_FakeToolCall(name="cog_resolve_uri")])
        v = score(rubric, result)
        assert v.passed

    def test_passes_when_other_tool_matches(self):
        rubric = Rubric(expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"])
        result = _FakeResult(tool_calls=[_FakeToolCall(name="cog_read_cogdoc")])
        v = score(rubric, result)
        assert v.passed

    def test_fails_when_no_tool_matches(self):
        rubric = Rubric(expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"])
        result = _FakeResult(tool_calls=[_FakeToolCall(name="cog_search_memory")])
        v = score(rubric, result)
        assert not v.passed
        assert any("expected_tools_any_of" in f for f in v.failures)

    def test_fails_when_no_tools_called(self):
        rubric = Rubric(expected_tools_any_of=["cog_read_cogdoc"])
        result = _FakeResult(tool_calls=[])
        v = score(rubric, result)
        assert not v.passed

    def test_empty_list_does_not_add_failure(self):
        rubric = Rubric(expected_tools_any_of=[])
        result = _FakeResult(tool_calls=[])
        v = score(rubric, result)
        assert v.passed


# ---------------------------------------------------------------------------
# Composition: both primitives active simultaneously
# ---------------------------------------------------------------------------

class TestComposition:
    def test_both_pass(self):
        rubric = Rubric(
            expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"],
            content_contains_any_of_ci=["error", "failed", "recognized", "invalid"],
        )
        result = _FakeResult(
            tool_calls=[_FakeToolCall(name="cog_resolve_uri")],
            content="adrs is not a recognized or valid URI type",
        )
        v = score(rubric, result)
        assert v.passed

    def test_tool_fail_content_pass(self):
        rubric = Rubric(
            expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"],
            content_contains_any_of_ci=["error", "invalid"],
        )
        result = _FakeResult(
            tool_calls=[_FakeToolCall(name="cog_search_memory")],
            content="An error occurred.",
        )
        v = score(rubric, result)
        assert not v.passed
        assert len(v.failures) == 1
        assert "expected_tools_any_of" in v.failures[0]

    def test_tool_pass_content_fail(self):
        rubric = Rubric(
            expected_tools_any_of=["cog_read_cogdoc", "cog_resolve_uri"],
            content_contains_any_of_ci=["error", "invalid"],
        )
        result = _FakeResult(
            tool_calls=[_FakeToolCall(name="cog_resolve_uri")],
            content="The operation completed normally.",
        )
        v = score(rubric, result)
        assert not v.passed
        assert len(v.failures) == 1
        assert "content_contains_any_of_ci" in v.failures[0]


# ---------------------------------------------------------------------------
# Rubric dataclass instantiation (verification gate)
# ---------------------------------------------------------------------------

class TestRubricInstantiation:
    def test_content_contains_any_of_ci_field_exists(self):
        r = Rubric(content_contains_any_of_ci=["x", "y"])
        assert r.content_contains_any_of_ci == ["x", "y"]

    def test_expected_tools_any_of_field_exists(self):
        r = Rubric(expected_tools_any_of=["a", "b"])
        assert r.expected_tools_any_of == ["a", "b"]

    def test_defaults_are_empty_lists(self):
        r = Rubric()
        assert r.content_contains_any_of_ci == []
        assert r.expected_tools_any_of == []
