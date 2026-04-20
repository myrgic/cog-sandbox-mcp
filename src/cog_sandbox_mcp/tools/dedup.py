import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from cog_sandbox_mcp.sandbox import (
    _authorized_paths,
    resolve_virtual,
    to_virtual,
)

PLAN_TTL_SECONDS = 300
HASH_CHUNK_BYTES = 1 << 20

_plans: dict[str, dict[str, Any]] = {}


def _expire_old_plans() -> None:
    now = time.time()
    stale = [k for k, v in _plans.items() if now - v["created_at"] > PLAN_TTL_SECONDS]
    for k in stale:
        del _plans[k]


def _hash_file(path: Path) -> str:
    h = hashlib.blake2b(digest_size=32)
    with path.open("rb") as f:
        while chunk := f.read(HASH_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def hash_file(path: str) -> dict[str, Any]:
    """Compute a blake2b-256 content hash of a file."""
    p = resolve_virtual(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    return {
        "path": to_virtual(p),
        "size": p.stat().st_size,
        "hash": _hash_file(p),
    }


def find_duplicates(
    path: str = "", min_size: int = 1, follow_symlinks: bool = False
) -> dict[str, Any]:
    """Scan for byte-identical files across authorized workspaces.

    If path is empty, scans every authorized workspace. Otherwise scans inside
    the given workspace path. Returns a plan_id usable with consolidate_duplicates.
    Plans expire after 5 minutes. min_size defaults to 1 (skips empty files).
    """
    _expire_old_plans()
    if not path or path.strip().strip("/") == "":
        roots = list(_authorized_paths)
    else:
        roots = [resolve_virtual(path)]

    by_size: dict[int, list[Path]] = {}
    for base in roots:
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if not follow_symlinks and p.is_symlink():
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < min_size:
                continue
            by_size.setdefault(size, []).append(p)

    by_hash: dict[str, list[Path]] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        for p in group:
            try:
                h = _hash_file(p)
            except OSError:
                continue
            by_hash.setdefault(h, []).append(p)

    duplicates = [
        {
            "hash": h,
            "size": paths[0].stat().st_size,
            "paths": [to_virtual(p) for p in paths],
        }
        for h, paths in by_hash.items()
        if len(paths) > 1
    ]

    plan_id = str(uuid.uuid4())
    _plans[plan_id] = {"created_at": time.time(), "duplicates": duplicates}

    bytes_reclaimable = sum(d["size"] * (len(d["paths"]) - 1) for d in duplicates)
    return {
        "plan_id": plan_id,
        "duplicate_groups": len(duplicates),
        "bytes_reclaimable": bytes_reclaimable,
        "duplicates": duplicates,
    }


def consolidate_duplicates(
    plan_id: str, strategy: str = "hardlink", keep: str = "oldest"
) -> dict[str, Any]:
    """Apply a dedup plan previously returned by find_duplicates.

    strategy: 'hardlink' replaces dupes with hardlinks; 'delete' removes them.
    keep: 'oldest' | 'newest' | 'first' — which file to retain per group.
    Hardlinking is done atomically via a temp path + rename.
    """
    _expire_old_plans()
    plan = _plans.get(plan_id)
    if plan is None:
        raise ValueError(f"unknown or expired plan_id: {plan_id}")
    if strategy not in ("hardlink", "delete"):
        raise ValueError(f"invalid strategy: {strategy!r}")
    if keep not in ("oldest", "newest", "first"):
        raise ValueError(f"invalid keep: {keep!r}")

    actions: list[dict[str, Any]] = []
    errors: list[str] = []

    for group in plan["duplicates"]:
        try:
            paths = [resolve_virtual(v) for v in group["paths"]]
        except FileNotFoundError as e:
            errors.append(f"{e}")
            continue
        existing = [p for p in paths if p.is_file()]
        if len(existing) < 2:
            continue
        if keep == "first":
            kept = existing[0]
        elif keep == "oldest":
            kept = min(existing, key=lambda p: p.stat().st_mtime)
        else:
            kept = max(existing, key=lambda p: p.stat().st_mtime)
        for p in existing:
            if p == kept:
                continue
            try:
                if strategy == "hardlink":
                    tmp = p.with_name(p.name + f".cogtmp-{uuid.uuid4().hex[:8]}")
                    tmp.hardlink_to(kept)
                    tmp.replace(p)
                else:
                    p.unlink()
                actions.append(
                    {
                        "removed": to_virtual(p),
                        "kept": to_virtual(kept),
                        "strategy": strategy,
                    }
                )
            except OSError as e:
                errors.append(f"{to_virtual(p)}: {e}")

    del _plans[plan_id]
    return {"applied": len(actions), "errors": errors, "actions": actions}


def register(mcp: FastMCP) -> None:
    mcp.tool(
        title="Hash file",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(hash_file)
    mcp.tool(
        title="Find duplicates",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(find_duplicates)
    mcp.tool(
        title="Consolidate duplicates",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(consolidate_duplicates)
