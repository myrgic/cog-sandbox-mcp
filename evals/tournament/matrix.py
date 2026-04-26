"""Tournament matrix expansion.

Loads an experiment cogdoc, resolves variant pointers, and expands the
Cartesian product of (system_prompt × tool_description × task) into
a list of TrialSpec objects that the runner consumes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from evals.harness.cases import Case, Rubric
from evals.tournament.variants import Variant, load_experiment, load_variants

log = logging.getLogger(__name__)


@dataclass
class Experiment:
    """Parsed experiment cogdoc."""

    id: str
    title: str
    baseline_variant: str
    """Composite baseline key — e.g. 'sp-1-production+td-1-current'."""

    variant_axes: dict[str, list[str]]
    """Axis → list of variant ids — e.g. {'system_prompt': ['sp-1-production', 'sp-3-stripped']}."""

    task_ids: list[str]
    target: str
    tags: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrialSpec:
    """A single trial to execute: one variant config × one task."""

    trial_id: str
    experiment_id: str
    task_variant: Variant
    """The task variant — content is the 'case:' dict from frontmatter."""

    variant_ids: dict[str, str]
    """Axis → variant_id for each non-task axis in this trial."""

    system_prompt_variant: Variant | None
    """Resolved system-prompt variant (None if axis not in experiment)."""

    tool_description_variant: Variant | None
    """Resolved tool-description variant (None if axis not in experiment or Phase 1)."""

    target: str


def _build_case(task_variant: Variant) -> Case:
    """Build a Case from a task variant's 'case:' content dict."""
    content = task_variant.content or {}
    prompt = content.get("prompt", "")
    rubric_data = content.get("rubric") or {}
    rubric = Rubric(
        expected_tools=rubric_data.get("expected_tools") or [],
        expected_tools_any_of=rubric_data.get("expected_tools_any_of") or [],
        forbidden_tools=rubric_data.get("forbidden_tools") or [],
        content_contains=rubric_data.get("content_contains") or [],
        content_must_not_contain=rubric_data.get("content_must_not_contain") or [],
        first_tool_one_of=rubric_data.get("first_tool_one_of") or [],
    )
    max_tokens = content.get("max_tokens", 1024)
    return Case(
        name=task_variant.id,
        prompt=prompt,
        rubric=rubric,
        max_tokens=max_tokens,
        tags=task_variant.tags,
    )


def load_experiment_from_cogdoc(
    experiment_id: str,
    variants_by_id: dict[str, Variant] | None = None,
) -> Experiment | None:
    """Load and parse an experiment cogdoc into an Experiment dataclass."""
    exp_variant = load_experiment(experiment_id)
    if exp_variant is None:
        return None

    fm = exp_variant.frontmatter
    raw_variants = fm.get("variants") or {}
    # Normalize axis keys: yaml may use 'system_prompt' or 'system-prompt'
    axes: dict[str, list[str]] = {}
    for key, val in raw_variants.items():
        normalized = key.replace("-", "_")
        axes[normalized] = list(val) if isinstance(val, list) else [val]

    task_ids = list(fm.get("tasks") or [])

    return Experiment(
        id=fm.get("id") or experiment_id,
        title=fm.get("title") or "",
        baseline_variant=fm.get("baseline_variant") or "",
        variant_axes=axes,
        task_ids=task_ids,
        target=fm.get("target") or "laptop-lms",
        tags=fm.get("tags") or [],
        frontmatter=fm,
    )


def expand_matrix(
    experiment: Experiment,
    variants_by_id: dict[str, Variant],
) -> list[TrialSpec]:
    """Expand experiment into a flat list of TrialSpec objects.

    Computes the Cartesian product of all variant axes × all tasks.
    Missing variant ids are logged as warnings and skipped.
    """
    sp_ids = experiment.variant_axes.get("system_prompt") or []
    td_ids = experiment.variant_axes.get("tool_description") or []
    task_ids = experiment.task_ids

    # Resolve variant objects
    sp_variants = _resolve_variants(sp_ids, variants_by_id, "system-prompt")
    td_variants = _resolve_variants(td_ids, variants_by_id, "tool-description")
    task_variants = _resolve_variants(task_ids, variants_by_id, "task")

    if not task_variants:
        log.warning("No task variants resolved for experiment %s", experiment.id)
        return []

    # Build Cartesian product
    # If no SP or TD axis, treat as a single-element list with None
    sp_list = sp_variants if sp_variants else [None]
    td_list = td_variants if td_variants else [None]

    specs: list[TrialSpec] = []
    trial_counter = 0

    for sp_v in sp_list:
        for td_v in td_list:
            for task_v in task_variants:
                trial_counter += 1
                variant_ids: dict[str, str] = {}
                if sp_v:
                    variant_ids["system_prompt"] = sp_v.id
                if td_v:
                    variant_ids["tool_description"] = td_v.id

                trial_id = (
                    f"{experiment.id}"
                    f"__{variant_ids.get('system_prompt', 'sp-default')}"
                    f"+{variant_ids.get('tool_description', 'td-default')}"
                    f"__{task_v.id}"
                )

                specs.append(
                    TrialSpec(
                        trial_id=trial_id,
                        experiment_id=experiment.id,
                        task_variant=task_v,
                        variant_ids=variant_ids,
                        system_prompt_variant=sp_v,
                        tool_description_variant=td_v,
                        target=experiment.target,
                    )
                )

    log.info(
        "Expanded %d trial specs for experiment %s (%d sp × %d td × %d tasks)",
        len(specs),
        experiment.id,
        len(sp_list),
        len(td_list),
        len(task_variants),
    )
    return specs


def _resolve_variants(
    ids: list[str],
    by_id: dict[str, Variant],
    expected_class: str,
) -> list[Variant]:
    """Resolve variant ids to Variant objects, filtering by variant_class."""
    resolved: list[Variant] = []
    for vid in ids:
        v = by_id.get(vid)
        if v is None:
            log.warning("Variant %r not found in loaded variants", vid)
            continue
        if v.variant_class != expected_class:
            log.warning(
                "Variant %r has class %r, expected %r — including anyway",
                vid,
                v.variant_class,
                expected_class,
            )
        resolved.append(v)
    return resolved
