import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Route all logs to stderr. stdout is reserved for MCP JSON-RPC framing."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
