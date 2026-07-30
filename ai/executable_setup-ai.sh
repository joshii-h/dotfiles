#!/usr/bin/env bash
# setup-ai.sh — REPRODUZIERBARER Aufbau des lokalen AI-venv (~/ai/venv) von Null.
# Zweck: nach SSD-Verlust / auf frischer Maschine das komplette media-mcp-Gerüst
# wiederherstellen, OHNE alles neu zu recherchieren. Danach hält ai-update.sh den
# App-Layer aktuell.
#
# torch/torchvision/torchaudio = repo.radeon.com ".lw"-Wheels, gekoppelt an die
# emerge-gepflegte System-ROCm-Version (aktuell 7.2.3). Steigt ROCm via @world,
# die drei WHL-URLs + torch-constraints.txt hier nachziehen (neue git-Hashes).
#
# chezmoi-managed: ai/executable_setup-ai.sh  (Quelle der Wahrheit)
# Voraussetzungen: python3.13, System-ROCm 7.2 (emerge), Ollama, ffmpeg, git.
# Reihenfolge ist WICHTIG (Chatterbox würde sonst torch==2.6 CUDA reinziehen).
set -uo pipefail
AI="${HOME}/ai"
VENV="${AI}/venv"
PY="${PY:-python3.13}"                     # venv-Python (cp313-Wheels!)
CONSTR="${AI}/torch-constraints.txt"

ROCM_IDX="https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2.3"
TORCH_WHL="${ROCM_IDX}/torch-2.10.0%2Brocm7.2.3.lw.git1a270074-cp313-cp313-linux_x86_64.whl"
TAUDIO_WHL="${ROCM_IDX}/torchaudio-2.10.0%2Brocm7.2.3.git5047768f-cp313-cp313-linux_x86_64.whl"
TVISION_WHL="${ROCM_IDX}/torchvision-0.25.0%2Brocm7.2.3.git82df5f59-cp313-cp313-linux_x86_64.whl"

# --- 0) torch-constraints.txt (die Pins, die den Stack zusammenhalten) -----------
# numpy<2.5: numba 0.66 (via librosa/chatterbox) verträgt kein numpy>=2.5.
# setuptools<81: resemble-perth/chatterbox brauchen pkg_resources (ab 81 entfernt).
if [[ ! -f "$CONSTR" ]]; then
  echo ">> torch-constraints.txt anlegen"
  cat > "$CONSTR" <<'EOF'
# ROCm-PyTorch-Pins (repo.radeon.com .lw-Wheels, linken System-ROCm 7.2).
torch==2.10.0+rocm7.2.3.lw.git1a270074
torchvision==0.25.0+rocm7.2.3.git82df5f59
torchaudio==2.10.0+rocm7.2.3.git5047768f
triton==3.6.0+rocm7.2.3.git4ed88892
numpy<2.5
setuptools<81
EOF
fi

# --- 1) venv --------------------------------------------------------------------
if [[ ! -x "${VENV}/bin/python" ]]; then
  echo ">> venv anlegen mit ${PY}"
  "$PY" -m venv "$VENV"
fi
PIP="${VENV}/bin/pip"
"$PIP" install --disable-pip-version-check --upgrade pip wheel "setuptools<81"

pipc() { "$PIP" install --disable-pip-version-check -c "$CONSTR" "$@"; }

# --- 2) torch-ROCm (exakte .lw-Wheels) ------------------------------------------
echo ">> torch/torchaudio/torchvision (ROCm ${ROCM_IDX##*/})"
pipc "$TORCH_WHL" "$TAUDIO_WHL" "$TVISION_WHL"

# --- 3) App-Layer: Whisper/diffusers/pyannote/Piper -----------------------------
echo ">> App-Layer"
pipc diffusers transformers accelerate safetensors huggingface_hub \
     librosa soundfile 'mcp[cli]' uvicorn pyannote.audio piper-tts

# --- 4) Sprecher-Enrollment (speechbrain) — --no-deps schützt torch -------------
echo ">> speechbrain"
pipc --no-deps speechbrain
pipc hyperpyyaml

# --- 5) Chatterbox (GPU-Voice-Clone) --------------------------------------------
# --no-deps ist PFLICHT: chatterbox-tts pinnt torch==2.6.0/transformers==5.2.0 usw.
# und würde sonst den ganzen ROCm-Stack zerschießen. Nur die echten Rest-Deps
# nachinstallieren (constrained). perth braucht pkg_resources -> setuptools<81 (o.).
echo ">> chatterbox-tts (--no-deps + echte Deps)"
pipc --no-deps chatterbox-tts
pipc resemble-perth conformer==0.3.2 omegaconf==2.3.1 s3tokenizer spacy-pkuseg pykakasi==2.3.0

# --- 6) torchcodec MUSS RAUS ----------------------------------------------------
# CUDA-Build (von pyannote/chatterbox mitgezogen) beschädigt die GPU-Op-Registry
# -> Whisper+pyannote SIGABRT. Audio laden wir via soundfile, Chatterbox speichert
# via soundfile statt torchaudio.save -> torchcodec unnötig.
"$PIP" uninstall -y torchcodec >/dev/null 2>&1 && echo ">> torchcodec entfernt" || true

# --- 7) Sanity: torch muss ROCm bleiben -----------------------------------------
v="$("${VENV}/bin/python" -c 'import torch;print(torch.__version__, torch.cuda.is_available())' 2>/dev/null)"
if [[ "$v" == *rocm*True ]]; then echo ">> torch OK: $v"; else echo "!! WARNUNG torch: '$v'"; fi

# --- 8) Piper-Stimmen (DE/EN) ---------------------------------------------------
echo ">> Piper-Stimmen"
mkdir -p "${AI}/piper-voices"
for V in de_DE-thorsten-high en_US-lessac-medium; do
  "${VENV}/bin/python" -m piper.download_voices "$V" --data-dir "${AI}/piper-voices" 2>&1 | tail -1
done

# --- 9) Ollama-Modelle (Gehirn + Vision + Zusammenfassung) ----------------------
if command -v ollama >/dev/null; then
  echo ">> Ollama-Modelle (Download gross)"
  for M in qwen3:30b qwen2.5vl:7b gemma4:26b; do ollama pull "$M" || true; done
fi

# --- 9b) ComfyUI custom_nodes (VideoHelperSuite -> mp4-Output fuer generate_video) ---
if [[ -d "${AI}/ComfyUI/custom_nodes" ]]; then
  VHS="${AI}/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite"
  [[ -d "${VHS}/.git" ]] || git clone --depth 1 https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git "$VHS"
  [[ -f "${VHS}/requirements.txt" ]] && pipc -r "${VHS}/requirements.txt"
fi
# ComfyUI-Modelle (SDXL/FLUX/Qwen-Image/LTX-Video/ACE-Step, ~90GB) separat:
echo ">> ComfyUI-Modelle: bash ${AI}/comfyui-models.sh   (SDXL/FLUX/Qwen/LTX/ACE)"

# --- 10) Verzeichnisse + Hinweise -----------------------------------------------
mkdir -p "${AI}/incoming" "${AI}/media-mcp/outputs" "${AI}/media-mcp/tmp" \
         "${AI}/speaker-profiles" "${AI}/.miopen"
cat <<'NOTE'

setup-ai.sh fertig. NOCH MANUELL (bewusst nicht hier, weil gross/geheim):
  - ComfyUI + Modelle: ~/ai/comfyui.sh bzw. ComfyUI/models/ (SDXL/FLUX/Qwen-Image, ~zig GB)
  - Secrets (nicht in git):  ~/ai/.hf_token   (HF, pyannote-gated)
                             ~/ai/.searxng_auth  (Basic-Auth fuers vServer-SearXNG; neu generierbar
                                 -> dann Hash in vServer ~/Docker/Traefik/dynamic/searxng-api.yaml aktualisieren)
  - media-mcp OpenRC-Service: /etc/init.d/media-mcp (+ run.sh via chezmoi)
  - Exakter Referenz-Stand aller Pakete: ~/ai/pip-freeze-snapshot.txt (chezmoi)
NOTE
