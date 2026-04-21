import logging
import os

from mcp.server.fastmcp import FastMCP

from cog_sandbox_mcp.logging_setup import configure_logging
from cog_sandbox_mcp.sandbox import initialize_auth
from cog_sandbox_mcp.tools import register_all

log = logging.getLogger(__name__)

# Env-var knobs for HTTP transport. stdio remains the default; HTTP is opt-in.
ENV_TRANSPORT = "MCP_TRANSPORT"
ENV_HTTP_HOST = "MCP_HTTP_HOST"
ENV_HTTP_PORT = "MCP_HTTP_PORT"
ENV_HTTP_PATH = "MCP_HTTP_PATH"

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 7823
DEFAULT_HTTP_PATH = "/mcp"


def select_transport(env: dict[str, str] | None = None) -> str:
    """Return the FastMCP transport name selected by env.

    ``MCP_TRANSPORT`` values (case-insensitive):
      - unset / empty / "stdio"   -> "stdio"   (default; preserves existing behavior)
      - "http" / "streamable-http" / "streamable_http" -> "streamable-http"

    Anything else raises ``ValueError`` early so a typo doesn't silently fall back
    to stdio when the operator intended HTTP.
    """
    src = env if env is not None else os.environ
    raw = (src.get(ENV_TRANSPORT) or "").strip().lower()
    if raw in ("", "stdio"):
        return "stdio"
    if raw in ("http", "streamable-http", "streamable_http"):
        return "streamable-http"
    raise ValueError(
        f"Unsupported {ENV_TRANSPORT}={raw!r}; expected 'stdio' or 'http'."
    )


def http_settings(env: dict[str, str] | None = None) -> dict[str, object]:
    """Resolve host/port/path for HTTP transport from env, with defaults."""
    src = env if env is not None else os.environ
    host = src.get(ENV_HTTP_HOST) or DEFAULT_HTTP_HOST
    port_raw = src.get(ENV_HTTP_PORT)
    try:
        port = int(port_raw) if port_raw else DEFAULT_HTTP_PORT
    except ValueError as e:
        raise ValueError(
            f"Invalid {ENV_HTTP_PORT}={port_raw!r}: {e}"
        ) from e
    path = src.get(ENV_HTTP_PATH) or DEFAULT_HTTP_PATH
    if not path.startswith("/"):
        path = "/" + path
    return {"host": host, "port": port, "streamable_http_path": path}


def build_server(**fastmcp_kwargs: object) -> FastMCP:
    mcp = FastMCP("cog-sandbox", **fastmcp_kwargs)
    register_all(mcp)
    return mcp


def main() -> None:
    configure_logging()
    initialize_auth()
    transport = select_transport()
    if transport == "stdio":
        build_server().run()
        return
    # streamable-http: configure host/port/path on the FastMCP instance, then
    # hand off to FastMCP's own uvicorn runner.
    settings = http_settings()
    url = f"http://{settings['host']}:{settings['port']}{settings['streamable_http_path']}"
    log.info("cog-sandbox-mcp HTTP transport listening at %s", url)
    build_server(**settings).run(transport="streamable-http")
