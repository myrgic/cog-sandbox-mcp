from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from cog_sandbox_mcp.sandbox import (
    authorized_workspace_names,
    grant_workspace,
    revoke_workspace,
)


def list_authorized_paths() -> dict[str, Any]:
    """List the workspaces currently authorized for your tool access — your complete visible world.

    The returned authorized_paths array is the EXHAUSTIVE list of top-level directories
    you can reach. Any workspace name NOT in this list is topologically non-existent
    from your perspective; attempts to read, list, write, grep, tree, or otherwise
    touch such a workspace will error as 'not found' (indistinguishable from a truly
    missing path).

    CALL THIS FIRST when orienting yourself in any task that references filesystem
    paths. Compare the names in the user's request against this list. If the user
    names a workspace that is NOT in your authorized_paths, you MUST call
    grant_path_access BEFORE attempting any other filesystem operation on it — do not
    substitute a similarly-named authorized workspace.
    """
    return {"authorized_paths": authorized_workspace_names()}


def grant_path_access(path: str, reason: str) -> dict[str, Any]:
    """Request access to a workspace that is not currently in your authorized list.

    CALL THIS WHEN: the user's task or instructions reference a workspace name that
    does not appear in list_authorized_paths. This is the ONLY correct response to
    an unauthorized reference. Do NOT silently substitute a different, authorized
    workspace that happens to have a similar name — that is a reasoning error, not
    a helpful adaptation. If the name might be a typo, still call this tool with a
    clear reason; the user can deny if they intended a different name.

    HOW IT WORKS: the user is shown a confirmation dialog with the path and reason.
    Approval of this tool call IS the authorization — their click is the grant.
    Write the reason argument so a human reviewer can decide intelligently: state
    plainly what you plan to do and why the workspace is needed.

    PATH RULES: path must be a single workspace name — a direct child of the sandbox
    root, no path separators, no parent references. "cog-workspace" ok;
    "cog-workspace/docs" not ok.

    ERROR SEMANTICS: if the named workspace does not exist, this returns 'not found'.
    This error is indistinguishable from "exists but is not made visible to you" — by
    design, so that probing for workspace existence is not a side channel. Do NOT
    interpret 'not found' as information about which workspaces do or do not exist.
    """
    if not reason or not reason.strip():
        raise ValueError("reason must be non-empty — the user needs to know why")
    target = grant_workspace(path)
    return {
        "granted": target.name,
        "reason": reason,
        "authorized_paths": authorized_workspace_names(),
    }


def revoke_path_access(path: str) -> dict[str, Any]:
    """Remove a workspace from your authorized list; it becomes topologically invisible to you.

    CALL THIS WHEN: a task involving a workspace is complete and you no longer need
    access, or when you want to narrow your reach for safety before a sensitive
    operation. Only ever narrows capability — never destructive to files or state.

    After this call, the workspace will not appear in list_authorized_paths and any
    subsequent attempt to operate on it will error as 'not found'. The workspace
    directory itself and its contents are untouched on disk — this only changes
    your view.
    """
    was_present = revoke_workspace(path)
    return {
        "removed": path,
        "was_authorized": was_present,
        "authorized_paths": authorized_workspace_names(),
    }


def register(mcp: FastMCP) -> None:
    mcp.tool(
        title="List authorized paths",
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, openWorldHint=False
        ),
    )(list_authorized_paths)
    mcp.tool(
        title="Grant path access",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )(grant_path_access)
    mcp.tool(
        title="Revoke path access",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )(revoke_path_access)
