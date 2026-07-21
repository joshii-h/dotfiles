#!/usr/bin/env bash
# Launcher für den Media-MCP-Server (Whisper + SDXL) — RDNA3-Env-Flags.
# chezmoi-managed: ai/media-mcp/executable_run.sh
set -euo pipefail
export HOME="${HOME:-/home/joshii}"                  # im OpenRC-Service-Kontext gesetzt
export HSA_TOOLS_LIB=""                              # roctracer-Assertion vermeiden
export MIOPEN_FIND_MODE=2
export MIOPEN_USER_DB_PATH="${HOME}/ai/.miopen"
export MIOPEN_CUSTOM_CACHE_DIR="${HOME}/ai/.miopen"
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True
export MEDIA_MCP_PORT="${MEDIA_MCP_PORT:-8765}"
export MEDIA_MCP_IDLE_UNLOAD_S="${MEDIA_MCP_IDLE_UNLOAD_S:-180}"
mkdir -p "${HOME}/ai/.miopen" "${HOME}/ai/incoming" "${HOME}/ai/media-mcp/outputs"
exec "${HOME}/ai/venv/bin/python" "${HOME}/ai/media-mcp/server.py"
