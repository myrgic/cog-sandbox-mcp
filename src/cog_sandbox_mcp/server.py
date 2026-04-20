from mcp.server.fastmcp import FastMCP

from cog_sandbox_mcp.logging_setup import configure_logging
from cog_sandbox_mcp.sandbox import initialize_auth
from cog_sandbox_mcp.tools import register_all


def build_server() -> FastMCP:
    mcp = FastMCP("cog-sandbox")
    register_all(mcp)
    return mcp


def main() -> None:
    configure_logging()
    initialize_auth()
    build_server().run()
