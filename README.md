# cog-sandbox-mcp

Cog OS filesystem-coding-agent sandbox exposed via the Model Context Protocol.

## Transports

### stdio (default)

Unchanged. Launch via `python -m cog_sandbox_mcp` (or the `cog-sandbox-mcp`
console script). One subprocess per MCP client.

### HTTP (Streamable-HTTP) — opt-in

Run one centralized server and connect multiple Claude Code sessions as
independent clients via `mcp-remote`.

Env-var switches (all optional, stdio remains the default):

| Env var           | Default     | Purpose                                         |
|-------------------|-------------|-------------------------------------------------|
| `MCP_TRANSPORT`   | `stdio`     | `http` / `streamable-http` to enable HTTP mode  |
| `MCP_HTTP_HOST`   | `127.0.0.1` | Bind address                                    |
| `MCP_HTTP_PORT`   | `7823`      | Bind port                                       |
| `MCP_HTTP_PATH`   | `/mcp`      | URL path for the Streamable-HTTP endpoint       |

Launch:

```bash
MCP_TRANSPORT=http python -m cog_sandbox_mcp
# -> INFO  cog-sandbox-mcp HTTP transport listening at http://127.0.0.1:7823/mcp
```

Wire into `.mcp.json` (Claude Code) using `mcp-remote`:

```json
{
  "mcpServers": {
    "cog-sandbox": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:7823/mcp"]
    }
  }
}
```

Each session spawns its own `mcp-remote` stdio shim that proxies JSON-RPC to the
shared HTTP server — the server sees each Claude Code session as an independent
client.

## Environment for the sandbox itself

- `COG_SANDBOX_ROOT` — parent directory of per-session workspaces.
- `COG_SANDBOX_INITIAL_AUTH` — colon-separated list of workspace names to
  pre-authorize on startup.
- `COG_OS_BASE_URL` — if set, Cog OS bridge tools are registered. See
  [`docs/BRIDGE_PATTERN.md`](docs/BRIDGE_PATTERN.md).
