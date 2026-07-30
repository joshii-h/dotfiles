#!/usr/bin/env bash
# Launcher für den Media-MCP-Server (Whisper + SDXL) — RDNA3-Env-Flags.
# chezmoi-managed: ai/media-mcp/executable_run.sh
set -euo pipefail
export HOME="${HOME:-/home/joshii}"                  # im OpenRC-Service-Kontext gesetzt
export HSA_TOOLS_LIB=""                              # roctracer-Assertion vermeiden
export HSA_ENABLE_SDMA=0                             # SDMA-Async-Copies AUS -> umgeht die
                                                     # roctracer 'hsa_amd_profiling_async_copy_enable'-
                                                     # Assertion, die pyannote-Diarization auf der GPU
                                                     # sonst mit SIGABRT killt (Whisper allein triggert
                                                     # sie nicht, pyannote schon). Kopien laufen dann
                                                     # ueber Blit-Kernels statt DMA -> minimal langsamer.
export MIOPEN_FIND_MODE=2
export MIOPEN_USER_DB_PATH="${HOME}/ai/.miopen"
export MIOPEN_CUSTOM_CACHE_DIR="${HOME}/ai/.miopen"
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True
export SEARXNG_URL="${SEARXNG_URL:-http://localhost:8888}"   # SearXNG lokal (Wohn-IP; vServer nur Fallback wg. Datacenter-IP-CAPTCHAs)
export MEDIA_MCP_PORT="${MEDIA_MCP_PORT:-8765}"
export MEDIA_MCP_IDLE_UNLOAD_S="${MEDIA_MCP_IDLE_UNLOAD_S:-180}"
mkdir -p "${HOME}/ai/.miopen" "${HOME}/ai/incoming" "${HOME}/ai/media-mcp/outputs"
exec "${HOME}/ai/venv/bin/python" "${HOME}/ai/media-mcp/server.py"
