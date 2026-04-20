#!/usr/bin/env bash
# Build cog-sandbox-mcp and print the mcp.json entry to paste into LM Studio.
# Non-invasive: does NOT modify LM Studio's config.

set -euo pipefail

RUNTIME="${COG_RUNTIME:-docker}"

while [ $# -gt 0 ]; do
    case "$1" in
        --runtime)
            RUNTIME="$2"
            shift 2
            ;;
        --runtime=*)
            RUNTIME="${1#*=}"
            shift
            ;;
        -h|--help)
            cat <<USAGE
Usage: install.sh [--runtime docker|podman|...]

Builds the cog-sandbox-mcp image using the given OCI runtime (default: docker;
override via --runtime or COG_RUNTIME env var) and prints an mcp.json entry
to paste into LM Studio's config.
USAGE
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

IMAGE_TAG="cog-sandbox-mcp:0.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
    echo "error: container runtime '$RUNTIME' not found in PATH" >&2
    exit 1
fi

echo "==> Building image $IMAGE_TAG with $RUNTIME"
"$RUNTIME" build -t "$IMAGE_TAG" "$REPO_ROOT"

# Detect LM Studio config location (docs-vs-reality bug; see reference_lm_studio.md).
if [ -f "$HOME/.cache/lm-studio/mcp.json" ]; then
    MCP_JSON="$HOME/.cache/lm-studio/mcp.json"
elif [ -f "$HOME/.lmstudio/mcp.json" ]; then
    MCP_JSON="$HOME/.lmstudio/mcp.json"
else
    MCP_JSON="$HOME/.cache/lm-studio/mcp.json (expected; create when you wire up your first MCP server in LM Studio)"
fi

WORKSPACES_ROOT_PLACEHOLDER="<ABSOLUTE_PATH_TO_WORKSPACES_ROOT>"
INITIAL_AUTH_PLACEHOLDER="<INITIAL_SUBDIR_OR_DOT>"

echo ""
echo "==> Image built."
echo "==> Target LM Studio config file: $MCP_JSON"
echo ""
echo "==> Paste this entry into the top-level \"mcpServers\" object:"
echo ""
cat <<EOF
  "cog-sandbox": {
    "command": "$RUNTIME",
    "args": [
      "run", "--rm", "-i",
      "--network=none",
      "-v", "$WORKSPACES_ROOT_PLACEHOLDER:/workspace:rw",
      "-e", "COG_SANDBOX_INITIAL_AUTH=$INITIAL_AUTH_PLACEHOLDER",
      "$IMAGE_TAG"
    ]
  }
EOF
echo ""
echo "==> Replace $WORKSPACES_ROOT_PLACEHOLDER with the absolute host path of the"
echo "    parent directory containing your workspaces (container mounts this at /workspace)."
echo "==> Replace $INITIAL_AUTH_PLACEHOLDER with the subdir name to authorize first"
echo "    (e.g. 'cog-workspace'), or '.' to authorize the whole mount."
echo "    The agent can grant/revoke additional paths via tools at runtime."
echo "==> Restart LM Studio after editing so it re-spawns the server."
