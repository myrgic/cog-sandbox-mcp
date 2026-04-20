# Build cog-sandbox-mcp and print the mcp.json entry to paste into LM Studio.
# Non-invasive: does NOT modify LM Studio's config.
#
# Usage: .\install.ps1 [-Runtime docker|podman|...]
#        (default: docker, or $env:COG_RUNTIME if set)

[CmdletBinding()]
param(
    [string]$Runtime = $(if ($env:COG_RUNTIME) { $env:COG_RUNTIME } else { 'docker' })
)

$ErrorActionPreference = 'Stop'

$ImageTag  = 'cog-sandbox-mcp:0.1'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir

if (-not (Get-Command $Runtime -ErrorAction SilentlyContinue)) {
    Write-Error "container runtime '$Runtime' not found in PATH"
    exit 1
}

$RuntimeFullPath = (Get-Command $Runtime).Source

Write-Host "==> Building image $ImageTag with $Runtime ($RuntimeFullPath)"
& $Runtime build -t $ImageTag $RepoRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$CandidateA = Join-Path $HOME '.cache\lm-studio\mcp.json'
$CandidateB = Join-Path $HOME '.lmstudio\mcp.json'
if (Test-Path $CandidateA) {
    $McpJson = $CandidateA
} elseif (Test-Path $CandidateB) {
    $McpJson = $CandidateB
} else {
    $McpJson = "$CandidateA (expected; create when you wire up your first MCP server in LM Studio)"
}

$WorkspacesRootPlaceholder = '<ABSOLUTE_PATH_TO_WORKSPACES_ROOT>'
$InitialAuthPlaceholder    = '<INITIAL_SUBDIR_OR_DOT>'

Write-Host ''
Write-Host '==> Image built.'
Write-Host "==> Target LM Studio config file: $McpJson"
Write-Host ''
Write-Host '==> Paste this entry into the top-level "mcpServers" object:'
Write-Host ''
# Use the fully-resolved runtime path and a Windows-specific env block. LM Studio's
# plugin subsystem can miss user env vars if it was launched before the runtime was
# installed; the env block sidesteps that. Backslashes are JSON-escaped.
$EscRuntimePath = $RuntimeFullPath -replace '\\','\\'
$EscAppData     = $env:APPDATA       -replace '\\','\\'
$EscLocal       = $env:LOCALAPPDATA  -replace '\\','\\'
$EscUserProfile = $env:USERPROFILE   -replace '\\','\\'

@"
  "cog-sandbox": {
    "command": "$EscRuntimePath",
    "args": [
      "run", "--rm", "-i",
      "--network=none",
      "-v", "${WorkspacesRootPlaceholder}:/workspace:rw",
      "-e", "COG_SANDBOX_INITIAL_AUTH=${InitialAuthPlaceholder}",
      "$ImageTag"
    ],
    "env": {
      "APPDATA": "$EscAppData",
      "LOCALAPPDATA": "$EscLocal",
      "USERPROFILE": "$EscUserProfile"
    }
  }
"@
Write-Host ''
Write-Host "==> Replace $WorkspacesRootPlaceholder with the absolute host path of the parent"
Write-Host '    directory that contains your workspaces (e.g. "C:\\Users\\chazm\\work").'
Write-Host '    The container mounts this at /workspace and the agent can reach anything under it'
Write-Host '    only if explicitly granted.'
Write-Host "==> Replace $InitialAuthPlaceholder with the initial authorized subdirectory"
Write-Host "    name (e.g. 'cog-workspace'), or '.' to authorize the whole mount at startup."
Write-Host '    The agent can grant/revoke additional paths at runtime via tools.'
Write-Host ''
Write-Host '==> After saving mcp.json, if the plugin errors with "dead network" / socket failures,'
Write-Host '    it is almost certainly because LM Studio was launched before the container runtime'
Write-Host '    was installed and has a stale env cache. Fix:'
Write-Host '       Stop-Process -Name "LM Studio" -Force; Start-Sleep 4;'
Write-Host '       Start-Process "C:\Program Files\LM Studio\LM Studio.exe"'
