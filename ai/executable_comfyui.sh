#!/usr/bin/env bash
# ComfyUI-Launcher für RX 7900 XTX (gfx1100) — RDNA3-Env-Flags
# (wird später chezmoi-managed als dot_ai/executable_comfyui.sh)
set -euo pipefail

export HSA_TOOLS_LIB=""                              # roctracer-Profiling aus → verhindert HSA-Finalize-Assertion
export MIOPEN_FIND_MODE=2                            # schnellere Kernel-Suche
export MIOPEN_USER_DB_PATH="${HOME}/ai/.miopen"      # persistenter MIOpen-Kernel-Cache (First-Run-Overhead nur einmal)
export MIOPEN_CUSTOM_CACHE_DIR="${HOME}/ai/.miopen"
export MALLOC_MMAP_THRESHOLD_=65536                  # glibc-Fragmentierung bei langen Sessions
export MALLOC_TRIM_THRESHOLD_=65536
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True

mkdir -p "${HOME}/ai/.miopen"
cd "${HOME}/ai/ComfyUI"
exec "${HOME}/ai/venv/bin/python" main.py --listen 127.0.0.1 --port 8188 "$@"
