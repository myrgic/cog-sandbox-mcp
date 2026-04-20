import os
from pathlib import Path


class AuthorizationError(FileNotFoundError):
    """Raised when a virtual path does not resolve inside any authorized workspace.

    Inherits FileNotFoundError so the agent sees unauthorized paths as simply
    non-existent — topologically invisible. Never surface the raw sandbox_root
    or authorized-path list through error messages.
    """


_authorized_paths: set[Path] = set()


def _sandbox_root() -> Path:
    """The real mount point inside the container. NOT exposed to agent-facing output."""
    root = Path(os.environ.get("COG_SANDBOX_ROOT", "/workspace")).resolve()
    if not root.exists():
        raise RuntimeError(f"sandbox root does not exist: {root}")
    return root


def _normalize_workspace_name(name: str) -> str:
    """Accept 'cog-workspace', '/cog-workspace', './cog-workspace' — return 'cog-workspace'.

    Rejects anything that isn't a plain direct-child name (no separators, no parent refs).
    """
    s = name.strip().strip("/")
    if s in ("", ".", ".."):
        raise ValueError(f"invalid workspace name: {name!r}")
    if "/" in s or "\\" in s:
        raise ValueError(
            f"workspace name must be a single component, not a path: {name!r}"
        )
    return s


def _workspace_path(name: str) -> Path:
    """Resolve a workspace name to its absolute real-filesystem path.

    Must be a direct child of the sandbox root. Raises FileNotFoundError if the
    named workspace doesn't exist — this is the masking behavior, agents never
    learn what does or doesn't exist outside their authorized view.
    """
    clean = _normalize_workspace_name(name)
    root = _sandbox_root()
    target = (root / clean).resolve()
    if target.parent != root or not target.exists():
        raise FileNotFoundError(f"workspace {clean!r} does not exist")
    return target


def initialize_auth() -> None:
    """Populate authorized workspaces from COG_SANDBOX_INITIAL_AUTH at server start.

    The env var is a colon-separated list of workspace *names* (direct children
    of the sandbox root). Required. Mutates the module-level set in place so
    consumers that import `_authorized_paths` by name see updates.
    """
    _authorized_paths.clear()
    raw = os.environ.get("COG_SANDBOX_INITIAL_AUTH", "").strip()
    if not raw:
        raise RuntimeError(
            "COG_SANDBOX_INITIAL_AUTH is required. Set it in mcp.json via -e. "
            "Value is a colon-separated list of workspace names (e.g. 'cog-workspace' "
            "or 'cog-workspace:downloads')."
        )
    for chunk in raw.split(":"):
        chunk = chunk.strip()
        if not chunk:
            continue
        _authorized_paths.add(_workspace_path(chunk))
    if not _authorized_paths:
        raise RuntimeError(
            "COG_SANDBOX_INITIAL_AUTH parsed to zero workspaces; at least one is required."
        )


def authorized_workspace_names() -> list[str]:
    """Visible workspace names — the agent's top-level directory entries."""
    return sorted(p.name for p in _authorized_paths)


def grant_workspace(name: str) -> Path:
    """Add a workspace to the authorized set. Returns the real resolved path.

    Raises FileNotFoundError if the named workspace doesn't exist as a direct
    child of the sandbox root (masks real cause).
    """
    target = _workspace_path(name)
    _authorized_paths.add(target)
    return target


def revoke_workspace(name: str) -> bool:
    """Remove a workspace from the authorized set. Returns True if it was present."""
    clean = _normalize_workspace_name(name)
    for p in list(_authorized_paths):
        if p.name == clean:
            _authorized_paths.remove(p)
            return True
    return False


def _authorized_root_for(path_under_ws: Path) -> Path | None:
    """Return the authorized workspace root that contains path_under_ws, if any."""
    for auth in _authorized_paths:
        try:
            path_under_ws.relative_to(auth)
            return auth
        except ValueError:
            continue
    return None


def resolve_virtual(path: str) -> Path:
    """Resolve an agent-view virtual path to a real filesystem path.

    Virtual paths start with an authorized workspace name as the first component,
    e.g. 'cog-workspace/README.md'. Absolute-looking paths ('/cog-workspace/...')
    are tolerated — the leading slash is stripped.

    Raises FileNotFoundError (masking) for anything that isn't under an
    authorized workspace. This error is identical to what you'd get for a path
    that truly doesn't exist — the agent cannot distinguish "not authorized"
    from "not present."
    """
    if not path:
        raise FileNotFoundError("empty path")
    cleaned = path.strip().lstrip("/")
    if not cleaned or cleaned in (".",):
        raise FileNotFoundError(path)
    parts = cleaned.split("/", 1)
    ws_name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""

    ws_root = None
    for auth in _authorized_paths:
        if auth.name == ws_name:
            ws_root = auth
            break
    if ws_root is None:
        raise FileNotFoundError(path)

    target = (ws_root / rest) if rest else ws_root
    resolved = target.resolve()
    if _authorized_root_for(resolved) is None:
        # Either a ../ escape out of the workspace, or a symlink out. Mask.
        raise FileNotFoundError(path)
    return resolved


def to_virtual(real_path: Path) -> str:
    """Convert a real filesystem path to its agent-view virtual path.

    Used by tools that need to echo back paths (e.g. glob results, tree).
    Raises ValueError if the path isn't under any authorized workspace — callers
    should ensure they only hand us paths that originated from authorized ops.
    """
    resolved = real_path.resolve() if not real_path.is_absolute() else real_path
    ws = _authorized_root_for(resolved)
    if ws is None:
        raise ValueError(f"{real_path} is not under any authorized workspace")
    rel = resolved.relative_to(ws)
    return f"{ws.name}/{rel}" if str(rel) != "." else ws.name
