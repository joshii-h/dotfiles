#!/usr/bin/env bash
# vLLM On-Demand — OpenAI-kompatibler Server auf RX 7900 XTX (gfx1100/RDNA3).
# chezmoi-managed: ai/vllm/executable_serve.sh
#
# WANN vLLM statt Ollama? NUR für NEBENLÄUFIGKEIT (viele parallele Agenten-
# Requests gleichzeitig). Bei Einzelanfragen ist Ollama auf dieser GPU SCHNELLER
# (~96 vs ~21 tok/s) — vLLM lohnt sich erst unter Last (PagedAttention/Batching).
#
# VRAM: belegt ~22 GB -> kann NICHT gleichzeitig mit Ollama/ComfyUI laufen.
# Daher ON-DEMAND: dieses Script startet vLLM im Vordergrund; Ctrl-C stoppt es
# und gibt den VRAM sofort frei. Vorher ggf. Ollama-Modelle entladen (ollama stop).
#
# Nutzung:   ~/ai/vllm/serve.sh [HF-Modell]     (default: Qwen/Qwen3-8B)
# Endpoint:  http://localhost:8000/v1   (aus Odysseus-Container: host.docker.internal:8000)
# Erststart zieht das ~20 GB ROCm-Image automatisch (danach instant).
set -euo pipefail
IMG="${VLLM_IMAGE:-rocm/vllm:rocm7.14.0_rdna_ubuntu24.04_py3.14_pytorch_2.11.0_vllm_0.23.0}"
MODEL="${1:-Qwen/Qwen3-8B}"
MAXLEN="${VLLM_MAXLEN:-8192}"

echo ">> vLLM startet mit Modell: $MODEL  (Ctrl-C stoppt + gibt VRAM frei)"
echo ">> Tipp: laufende Ollama-Modelle vorher entladen — for m in \$(ollama ps|awk 'NR>1{print \$1}'); do ollama stop \$m; done"
# Gruppen numerisch (Docker sucht Namen im Container, der render/video nicht kennt)
VIDEO_GID="$(getent group video | cut -d: -f3)"
RENDER_GID="$(getent group render | cut -d: -f3)"
exec docker run -it --rm \
  --device /dev/kfd --device /dev/dri \
  --group-add "${VIDEO_GID}" --group-add "${RENDER_GID}" \
  --network host --ipc host \
  --cap-add SYS_PTRACE --security-opt seccomp=unconfined \
  -v "${HOME}/.cache/huggingface:/root/.cache/huggingface" \
  -e VLLM_USE_TRITON_FLASH_ATTN=0 \
  "$IMG" \
  vllm serve "$MODEL" \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization 0.92 \
    --max-model-len "$MAXLEN" \
    --host 0.0.0.0 --port 8000
