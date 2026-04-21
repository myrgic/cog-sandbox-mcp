"""Unit tests for the HTTP-transport selection logic in ``server``.

These tests never bind a socket; they exercise the pure-function knobs
(``select_transport``, ``http_settings``) that translate env vars into the
values handed to FastMCP.
"""

from __future__ import annotations

import pytest

from cog_sandbox_mcp import server


# ---------- select_transport ----------


@pytest.mark.parametrize("value", [None, "", "stdio", "STDIO", " stdio "])
def test_select_transport_defaults_to_stdio(value: str | None) -> None:
    env = {} if value is None else {server.ENV_TRANSPORT: value}
    assert server.select_transport(env) == "stdio"


@pytest.mark.parametrize(
    "value", ["http", "HTTP", "streamable-http", "streamable_http", "Streamable-HTTP"]
)
def test_select_transport_http_aliases(value: str) -> None:
    assert server.select_transport({server.ENV_TRANSPORT: value}) == "streamable-http"


def test_select_transport_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unsupported MCP_TRANSPORT"):
        server.select_transport({server.ENV_TRANSPORT: "sse"})


# ---------- http_settings ----------


def test_http_settings_defaults() -> None:
    s = server.http_settings({})
    assert s == {
        "host": server.DEFAULT_HTTP_HOST,
        "port": server.DEFAULT_HTTP_PORT,
        "streamable_http_path": server.DEFAULT_HTTP_PATH,
    }


def test_http_settings_overrides() -> None:
    s = server.http_settings(
        {
            server.ENV_HTTP_HOST: "0.0.0.0",
            server.ENV_HTTP_PORT: "9100",
            server.ENV_HTTP_PATH: "/custom",
        }
    )
    assert s == {"host": "0.0.0.0", "port": 9100, "streamable_http_path": "/custom"}


def test_http_settings_prepends_leading_slash() -> None:
    s = server.http_settings({server.ENV_HTTP_PATH: "mcp"})
    assert s["streamable_http_path"] == "/mcp"


def test_http_settings_invalid_port() -> None:
    with pytest.raises(ValueError, match="Invalid MCP_HTTP_PORT"):
        server.http_settings({server.ENV_HTTP_PORT: "not-a-number"})


# ---------- build_server wires host/port through to FastMCP ----------


def test_build_server_passes_kwargs_through() -> None:
    mcp = server.build_server(host="127.0.0.1", port=7823, streamable_http_path="/mcp")
    assert mcp.settings.host == "127.0.0.1"
    assert mcp.settings.port == 7823
    assert mcp.settings.streamable_http_path == "/mcp"


def test_build_server_default_has_no_custom_bindings() -> None:
    mcp = server.build_server()
    # Default FastMCP host/port are fine; just verify the name is set and the
    # instance is usable without any HTTP-specific kwargs.
    assert mcp.name == "cog-sandbox"
