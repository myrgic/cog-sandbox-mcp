import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from cog_sandbox_mcp.sandbox import (
    authorized_workspace_names,
    resolve_virtual,
    to_virtual,
    _authorized_paths,  # for listing top-level workspaces in list_directory/tree
)

MAX_READ_BYTES = 5_000_000
DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000
GREP_TIMEOUT_S = 30
MAX_TREE_ENTRIES = 500
DEFAULT_TREE_DEPTH = 3


def read(path: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
    """Read a UTF-8 text file.

    path is a virtual path — first component is a workspace name, rest is the
    path inside. Returns a window of lines [offset, offset+limit). Lines longer
    than 2000 chars are truncated with an ellipsis marker.
    """
    p = resolve_virtual(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(
            f"file is {size} bytes, exceeds read cap of {MAX_READ_BYTES}"
        )
    with p.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    window = lines[offset : offset + limit]
    out: list[str] = []
    for line in window:
        stripped = line.rstrip("\n")
        if len(stripped) > MAX_LINE_CHARS:
            stripped = stripped[:MAX_LINE_CHARS] + "... [truncated]"
        out.append(stripped)
    return "\n".join(out)


def write(path: str, content: str) -> str:
    """Write content to a file. Creates parent directories as needed. Overwrites if present.

    path is a virtual path. Writes outside authorized workspaces will fail as
    if the location does not exist.
    """
    # For writes, the target file may not exist yet but its parent workspace must.
    # Resolve the parent directory first via resolve_virtual to enforce this.
    if "/" not in path.strip("/"):
        # Would be writing at the workspace root level — treat as writing a file inside.
        raise FileNotFoundError(path)
    parent_virtual, _, filename = path.strip("/").rpartition("/")
    parent_real = resolve_virtual(parent_virtual) if parent_virtual else None
    if parent_real is None:
        raise FileNotFoundError(path)
    parent_real.mkdir(parents=True, exist_ok=True)
    target = parent_real / filename
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {to_virtual(target)}"


def edit(
    path: str, old_string: str, new_string: str, replace_all: bool = False
) -> str:
    """Exact-string edit. old_string must match uniquely unless replace_all=True.

    old_string and new_string must differ.
    """
    if old_string == new_string:
        raise ValueError("old_string and new_string must differ")
    p = resolve_virtual(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old_string)
    if count == 0:
        raise ValueError("old_string not found in file")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string matches {count} times; pass replace_all=True to replace all, "
            "or provide a longer unique substring"
        )
    if replace_all:
        new_text = text.replace(old_string, new_string)
        replaced = count
    else:
        new_text = text.replace(old_string, new_string, 1)
        replaced = 1
    p.write_text(new_text, encoding="utf-8")
    return f"replaced {replaced} occurrence(s) in {to_virtual(p)}"


def glob(pattern: str, path: str = "") -> list[str]:
    """Find files by name pattern.

    Use this for matching file names or extensions — e.g. '**/*.py', 'README.*'.
    Use list_directory for 'what's in this folder' and tree for recursive structure.

    If path is empty, searches across all authorized workspaces; otherwise searches
    inside the given workspace path. Results are virtual paths, mtime-sorted.
    """
    matches: list[tuple[float, Path]] = []
    if not path or path.strip().strip("/") == "":
        roots = sorted(_authorized_paths)
    else:
        roots = [resolve_virtual(path)]
    for base in roots:
        for m in base.glob(pattern):
            if not m.exists():
                continue
            try:
                matches.append((m.stat().st_mtime, m))
            except OSError:
                continue
    matches.sort(key=lambda t: t[0], reverse=True)
    return [to_virtual(m) for _, m in matches]


def grep(
    pattern: str,
    path: str = "",
    file_glob: str = "",
    output_mode: str = "files_with_matches",
    case_insensitive: bool = False,
    context_lines: int = 0,
    head_limit: int = 250,
) -> str:
    """Ripgrep content search.

    output_mode: 'content' | 'files_with_matches' | 'count'.
    file_glob: optional pattern to filter which files are searched.
    If path is empty, searches across all authorized workspaces.
    """
    if not path or path.strip().strip("/") == "":
        search_roots = [str(p) for p in sorted(_authorized_paths)]
    else:
        search_roots = [str(resolve_virtual(path))]
    cmd = ["rg", "--color=never"]
    if case_insensitive:
        cmd.append("-i")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    if output_mode == "content":
        cmd.append("-n")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
    elif output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:
        raise ValueError(
            f"invalid output_mode: {output_mode!r} "
            "(expected 'content' | 'files_with_matches' | 'count')"
        )
    cmd.append(pattern)
    cmd.extend(search_roots)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=GREP_TIMEOUT_S, check=False
    )
    if result.returncode not in (0, 1):
        # Don't surface rg's stderr — may leak real paths. Generic error only.
        raise RuntimeError("grep failed")
    # Translate real paths in output to virtual paths.
    lines = []
    for line in result.stdout.splitlines():
        lines.append(_translate_paths_in_line(line))
    if head_limit and len(lines) > head_limit:
        remainder = len(lines) - head_limit
        lines = lines[:head_limit] + [f"... [{remainder} more lines omitted]"]
    return "\n".join(lines)


def _translate_paths_in_line(line: str) -> str:
    """Replace any occurrence of an authorized workspace's real path with the virtual form."""
    for auth in _authorized_paths:
        real = str(auth)
        if real in line:
            line = line.replace(real, auth.name)
    return line


def list_directory(path: str = "") -> dict[str, Any]:
    """List entries in a directory.

    If path is empty or '/', lists the agent's visible workspaces (top-level).
    Otherwise, lists entries inside the given workspace path.

    Each entry: {name, type: 'file'|'directory'|'symlink', size: int|None, mtime: float}.
    """
    if not path or path.strip().strip("/") == "":
        entries = [
            {"name": name, "type": "directory", "size": None, "mtime": None}
            for name in authorized_workspace_names()
        ]
        return {"path": "", "entries": entries}
    p = resolve_virtual(path)
    if not p.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")
    entries: list[dict[str, Any]] = []
    for e in sorted(p.iterdir()):
        try:
            st = e.stat()
        except OSError:
            continue
        if e.is_symlink():
            t = "symlink"
        elif e.is_dir():
            t = "directory"
        else:
            t = "file"
        entries.append({
            "name": e.name,
            "type": t,
            "size": st.st_size if t == "file" else None,
            "mtime": st.st_mtime,
        })
    return {"path": path.strip("/"), "entries": entries}


def tree(path: str = "", max_depth: int = DEFAULT_TREE_DEPTH, max_entries: int = MAX_TREE_ENTRIES) -> str:
    """Render a bounded text tree of the given path.

    Use this when you want a recursive overview of a directory structure (what
    `ls -R` would give you, but structured and bounded). If path is empty, the
    tree starts from the agent's visible workspaces.

    max_depth: how many levels deep to descend (1 = direct children only).
    max_entries: total entries cap across the whole tree. Truncation is noted.
    """
    lines: list[str] = []
    counter = [0]
    if not path or path.strip().strip("/") == "":
        for p in sorted(_authorized_paths):
            _tree_walk(p, p.name, 0, max_depth, max_entries, counter, lines)
    else:
        resolved = resolve_virtual(path)
        _tree_walk(resolved, path.strip("/"), 0, max_depth, max_entries, counter, lines)
    return "\n".join(lines) if lines else "(empty)"


def _tree_walk(
    real: Path,
    display: str,
    depth: int,
    max_depth: int,
    max_entries: int,
    counter: list[int],
    lines: list[str],
) -> None:
    if counter[0] >= max_entries:
        if counter[0] == max_entries:
            lines.append("... [entry limit reached]")
            counter[0] += 1
        return
    counter[0] += 1
    indent = "  " * depth
    if not real.is_dir():
        lines.append(f"{indent}{real.name}")
        return
    name = real.name if depth > 0 else display
    lines.append(f"{indent}{name}/")
    if depth >= max_depth:
        return
    try:
        children = sorted(real.iterdir())
    except OSError:
        return
    for c in children:
        _tree_walk(c, c.name, depth + 1, max_depth, max_entries, counter, lines)


def register(mcp: FastMCP) -> None:
    mcp.tool(
        title="Read file",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(read)
    mcp.tool(
        title="Write file",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )(write)
    mcp.tool(
        title="Edit file",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=False
        ),
    )(edit)
    mcp.tool(
        title="Glob",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(glob)
    mcp.tool(
        title="Grep",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(grep)
    mcp.tool(
        title="List directory",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(list_directory)
    mcp.tool(
        title="Tree",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(tree)
