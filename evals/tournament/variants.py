"""Variant cogdoc loader.

Reads .cog.md files from the tournament cogdoc directory, parses YAML frontmatter,
and extracts variant content per variant_class.

Content extraction rules per variant_class:
  - system-prompt: body text under '## Variant content' section
  - tool-description: 'overrides:' dict from frontmatter
  - task: 'case:' dict from frontmatter

Robust to missing files; logs warnings.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Default tournament cogdoc root under the cog workspace.
# Resolution order:
#   1. COG_TOURNAMENT_ROOT env var (explicit override)
#   2. $COGOS_WORKSPACE/.cog/mem/semantic/architecture/tournament
#      where COGOS_WORKSPACE defaults to ~/workspaces/cog
_DEFAULT_TOURNAMENT_ROOT = Path(
    os.environ.get(
        "COG_TOURNAMENT_ROOT",
        os.path.join(
            os.environ.get(
                "COGOS_WORKSPACE",
                os.path.join(os.path.expanduser("~"), "workspaces", "cog"),
            ),
            ".cog",
            "mem",
            "semantic",
            "architecture",
            "tournament",
        ),
    )
)


@dataclass
class Variant:
    """A single prompt variant loaded from a .cog.md cogdoc."""

    id: str
    variant_class: str
    """'system-prompt', 'tool-description', or 'task'."""

    content: Any
    """str for system-prompt; dict for tool-description overrides; dict for task case."""

    baseline_of: str | None = None
    ablation: str | None = None
    tags: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split '---\n...\n---\n body' into (frontmatter_dict, body)."""
    if not text.startswith("---"):
        return {}, text
    # Find closing ---
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    fm_text = rest[:end]
    body = rest[end + 4:]
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        log.warning("YAML parse error in frontmatter: %s", e)
        fm = {}
    return fm, body


def _extract_section(body: str, section_name: str) -> str:
    """Extract content under '## section_name' heading from markdown body."""
    pattern = rf"## {re.escape(section_name)}\s*\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, body, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def load_variant_from_file(path: Path) -> Variant | None:
    """Load a single .cog.md file as a Variant. Returns None on error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
        return None

    fm, body = _split_frontmatter(text)
    vid = fm.get("id") or path.stem
    vc = fm.get("variant_class", "")

    if vc == "system-prompt":
        content: Any = _extract_section(body, "Variant content")
        if not content:
            log.warning("No '## Variant content' section in %s", path)
            content = ""
    elif vc == "tool-description":
        content = fm.get("overrides") or {}
        if not content:
            log.warning("No 'overrides:' in frontmatter of %s", path)
    elif vc == "task":
        content = fm.get("case") or {}
        if not content:
            log.warning("No 'case:' in frontmatter of %s", path)
    else:
        # experiment or unknown — store raw frontmatter as content
        content = fm

    return Variant(
        id=vid,
        variant_class=vc,
        content=content,
        baseline_of=fm.get("baseline_of"),
        ablation=fm.get("ablation"),
        tags=fm.get("tags") or [],
        frontmatter=fm,
        source_path=path,
    )


def load_variants(
    tournament_root: Path | None = None,
) -> dict[str, Variant]:
    """Load all .cog.md variant cogdocs from the tournament directory tree.

    Args:
        tournament_root: Root path containing system-prompts/, tool-descriptions/,
                         tasks/, experiments/ subdirectories. Defaults to the
                         workspace-local tournament directory.

    Returns:
        Dict mapping variant id → Variant. Experiment cogdocs are included with
        variant_class == '' or 'experiment' — callers filter by variant_class.
    """
    root = tournament_root or _DEFAULT_TOURNAMENT_ROOT
    if not root.exists():
        log.warning("Tournament root does not exist: %s", root)
        return {}

    variants: dict[str, Variant] = {}
    for path in sorted(root.rglob("*.cog.md")):
        v = load_variant_from_file(path)
        if v is not None:
            if v.id in variants:
                log.warning("Duplicate variant id %r — skipping %s", v.id, path)
                continue
            variants[v.id] = v

    log.info("Loaded %d variants from %s", len(variants), root)
    return variants


def load_experiment(
    experiment_id: str,
    tournament_root: Path | None = None,
) -> Variant | None:
    """Load a single experiment cogdoc by id."""
    root = tournament_root or _DEFAULT_TOURNAMENT_ROOT
    exp_dir = root / "experiments"
    if not exp_dir.exists():
        log.warning("Experiments dir not found: %s", exp_dir)
        return None
    for path in exp_dir.glob(f"{experiment_id}*.cog.md"):
        return load_variant_from_file(path)
    log.warning("Experiment %r not found in %s", experiment_id, exp_dir)
    return None
