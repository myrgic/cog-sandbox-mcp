from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a named workspace under a tmp sandbox root, authorize it, and return its path.

    The sandbox root is tmp_path; the workspace is tmp_path/ws. Tests operate on
    virtual paths like 'ws/foo.txt' and can assert against workspace / 'foo.txt'
    on the host side.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("COG_SANDBOX_ROOT", str(tmp_path))
    monkeypatch.setenv("COG_SANDBOX_INITIAL_AUTH", "ws")
    from cog_sandbox_mcp import sandbox
    sandbox.initialize_auth()
    return ws
