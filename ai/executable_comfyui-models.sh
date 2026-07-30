#!/usr/bin/env bash
# chezmoi-managed: ai/executable_comfyui-models.sh
#
# Reproducible re-download script for ComfyUI models on Battlestation.
# The model files themselves (~70 GB, currently-installed set) are NOT stored
# in git/chezmoi -- only this script is. Run it after a fresh ComfyUI checkout
# (e.g. after an SSD failure) to re-fetch everything into
# ~/ai/ComfyUI/models/<subdir>/.
#
# Idempotent: each download is skipped if the target file already exists, so
# this script is safe to re-run (e.g. to top up a partial restore).
#
# Usage:
#   bash ~/ai/comfyui-models.sh              # fetch everything
#   bash ~/ai/comfyui-models.sh qwen flux     # fetch only these groups (see
#                                             # group names below); omit args
#                                             # to fetch all groups.
#
# Requires: ~/ai/venv (huggingface_hub installed there; verified: 1.23.0).
# Optional: ~/ai/.hf_token (plain-text HF token) for gated repos. None of the
# models below are currently gated, but the token is wired up in case that
# changes (e.g. black-forest-labs/FLUX.1-dev upstream is gated -- we use the
# non-gated Comfy-Org repack instead, see FLUX section).
#
# Source verification method: every HF file below was checked with
# `HfApi().get_paths_info()` and its byte size compared against the actual
# on-disk file (where present) -- exact match confirms the correct upstream
# file, not just a plausible-looking repo name.

set -euo pipefail

AI_DIR="$HOME/ai"
MODELS="$AI_DIR/ComfyUI/models"
PYTHON="$AI_DIR/venv/bin/python"
HF_TOKEN_FILE="$AI_DIR/.hf_token"

if [[ ! -x "$PYTHON" ]]; then
    echo "WARNING: $PYTHON not found, falling back to system python3" >&2
    PYTHON="$(command -v python3)"
fi
if ! "$PYTHON" -c "import huggingface_hub" 2>/dev/null; then
    echo "ERROR: huggingface_hub not importable via $PYTHON" >&2
    echo "  Install it in the venv: $PYTHON -m pip install -U huggingface_hub" >&2
    exit 1
fi

if [[ -z "${HF_TOKEN:-}" && -f "$HF_TOKEN_FILE" ]]; then
    export HF_TOKEN
    HF_TOKEN="$(cat "$HF_TOKEN_FILE")"
fi

# Which groups to run: all of them, or only the ones named on argv.
GROUPS=("$@")
want() {
    [[ ${#GROUPS[@]} -eq 0 ]] && return 0
    local g
    for g in "${GROUPS[@]}"; do [[ "$g" == "$1" ]] && return 0; done
    return 1
}

# ---------------------------------------------------------------------------
# dl_hf REPO_ID REPO_PATH TARGET_DIR TARGET_NAME [REPO_TYPE]
#
# Downloads REPO_PATH from the HF repo REPO_ID into TARGET_DIR, then -- if the
# file lives under a subfolder in the repo (e.g. ACE-Step's split_files/...
# layout) -- moves it up to TARGET_DIR/TARGET_NAME as a flat file, and prunes
# the now-empty subfolders hf_hub_download created. Skips entirely if
# TARGET_DIR/TARGET_NAME already exists (idempotent).
# ---------------------------------------------------------------------------
dl_hf() {
    local repo_id="$1" repo_path="$2" target_dir="$3" target_name="$4" repo_type="${5:-model}"
    local target="$target_dir/$target_name"
    if [[ -f "$target" ]]; then
        echo "  [skip] $target_name (already present)"
        return 0
    fi
    echo "  [get]  $repo_id :: $repo_path"
    echo "         -> $target"
    mkdir -p "$target_dir"
    "$PYTHON" - "$repo_id" "$repo_path" "$target_dir" "$target_name" "$repo_type" <<'PYEOF'
import sys, shutil, pathlib
from huggingface_hub import hf_hub_download

repo_id, repo_path, target_dir, target_name, repo_type = sys.argv[1:6]
target_dir = pathlib.Path(target_dir)
target = target_dir / target_name

downloaded = pathlib.Path(hf_hub_download(
    repo_id=repo_id,
    filename=repo_path,
    repo_type=repo_type,
    local_dir=str(target_dir),
))

if downloaded != target:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(downloaded), str(target))
    # Clean up now-empty subfolders (e.g. split_files/diffusion_models/) that
    # hf_hub_download created to mirror the repo's directory layout.
    parent = downloaded.parent
    while parent != target_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent

print(f"    done: {target} ({target.stat().st_size / 1e9:.2f} GB)")
PYEOF
}

# civitai_manual TARGET_PATH URL NOTE
# For anything without a verified byte-identical HF mirror. Not used by any
# model currently on this box (see report), kept here as the pattern to
# extend the script with if a future model turns out Civitai-only.
civitai_manual() {
    local target="$1" url="$2" note="$3"
    if [[ -f "$target" ]]; then
        echo "  [skip] $(basename "$target") (already present)"
        return 0
    fi
    echo "  [MANUAL] $(basename "$target") is Civitai-only -- no scripted download."
    echo "           Target: $target"
    echo "           URL:    $url"
    [[ -n "$note" ]] && echo "           Note:   $note"
}

# ===========================================================================
# SDXL image checkpoints  (checkpoints/)
# All sources below were confirmed by exact byte-size match against the
# files actually installed on this box.
# ===========================================================================
if want sdxl; then
    echo "== SDXL checkpoints =="
    # Stability AI base model (official repo).
    dl_hf stabilityai/stable-diffusion-xl-base-1.0 \
        sd_xl_base_1.0.safetensors \
        "$MODELS/checkpoints" sd_xl_base_1.0.safetensors

    # RunDiffusion's official Juggernaut XL v9 upload; the file inside the repo
    # is named Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors (same model,
    # longer HF filename) -- byte-identical to the locally installed
    # Juggernaut-XL_v9.safetensors (7105348188 bytes), so we rename on fetch.
    dl_hf RunDiffusion/Juggernaut-XL-v9 \
        Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors \
        "$MODELS/checkpoints" Juggernaut-XL_v9.safetensors

    # cagliostrolab's official animagine-xl-4.0 repo; filename matches exactly.
    dl_hf cagliostrolab/animagine-xl-4.0 \
        animagine-xl-4.0.safetensors \
        "$MODELS/checkpoints" animagine-xl-4.0.safetensors

    # Playground v2.5: the installed file is the fp16 single-file checkpoint
    # (byte-identical), NOT the fp32 one (which is exactly 2x the size).
    dl_hf playgroundai/playground-v2.5-1024px-aesthetic \
        playground-v2.5-1024px-aesthetic.fp16.safetensors \
        "$MODELS/checkpoints" playground-v2.5.safetensors
fi

# ===========================================================================
# FLUX.1-dev  (checkpoints/)
# black-forest-labs/FLUX.1-dev is gated; Comfy-Org/flux1-dev is an UNGATED
# repack of the same weights (verified: gated=False) and its
# flux1-dev-fp8.safetensors is byte-identical to the local file
# (17246524772 bytes). License: FLUX.1-dev non-commercial (private use only).
# ===========================================================================
if want flux; then
    echo "== FLUX.1-dev =="
    dl_hf Comfy-Org/flux1-dev \
        flux1-dev-fp8.safetensors \
        "$MODELS/checkpoints" flux1-dev-fp8.safetensors
fi

# ===========================================================================
# Qwen-Image  (diffusion_models/, text_encoders/, vae/)
# All three files confirmed byte-identical against Comfy-Org/Qwen-Image_ComfyUI.
# Files live under split_files/<kind>/ in the repo; dl_hf flattens them.
# ===========================================================================
if want qwen; then
    echo "== Qwen-Image =="
    dl_hf Comfy-Org/Qwen-Image_ComfyUI \
        split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors \
        "$MODELS/diffusion_models" qwen_image_fp8_e4m3fn.safetensors

    dl_hf Comfy-Org/Qwen-Image_ComfyUI \
        split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \
        "$MODELS/text_encoders" qwen_2.5_vl_7b_fp8_scaled.safetensors

    dl_hf Comfy-Org/Qwen-Image_ComfyUI \
        split_files/vae/qwen_image_vae.safetensors \
        "$MODELS/vae" qwen_image_vae.safetensors
fi

# ===========================================================================
# LTX-Video  (checkpoints/, text_encoders/)
# NOTE: as of writing this was only PARTWAY downloaded on this box (a lock +
# .incomplete file was found under
# ~/ai/ComfyUI/models/checkpoints/.cache/huggingface/download/ for exactly
# this filename) -- included here so a from-scratch restore gets the full,
# intended model set rather than reproducing the partial state.
# ltxv-13b-0.9.8-distilled-fp8.safetensors confirmed present at the repo root
# of Lightricks/LTX-Video (matches the .incomplete filename exactly).
# t5xxl_fp8_e4m3fn_scaled.safetensors is the standard text encoder ComfyUI's
# native LTX-Video workflows pair with this checkpoint (CLIPLoader type
# "ltxv"/"t5"); NOT independently size-verified against a local file since
# none exists yet -- if your actual LTXV workflow uses a different T5
# variant, swap the filename below.
# ===========================================================================
if want ltx; then
    echo "== LTX-Video =="
    dl_hf Lightricks/LTX-Video \
        ltxv-13b-0.9.8-distilled-fp8.safetensors \
        "$MODELS/checkpoints" ltxv-13b-0.9.8-distilled-fp8.safetensors

    dl_hf comfyanonymous/flux_text_encoders \
        t5xxl_fp8_e4m3fn_scaled.safetensors \
        "$MODELS/text_encoders" t5xxl_fp8_e4m3fn_scaled.safetensors
fi

# ===========================================================================
# ACE-Step 1.5 (music generation)  (diffusion_models/, text_encoders/, vae/)
# NOTE: also only partially downloaded on this box (lock + .incomplete found
# under diffusion_models/.cache/huggingface/download/split_files/
# diffusion_models/ for acestep_v1.5_turbo.safetensors) -- included for the
# same from-scratch-restore reason as LTX-Video above. All filenames
# confirmed to exist in Comfy-Org/ace_step_1.5_ComfyUI_files (split_files/
# layout, flattened by dl_hf); sizes were NOT locally verifiable (no complete
# local file to compare against).
#   - qwen_0.6b_ace15.safetensors: smaller/faster text encoder (default).
#   - qwen_4b_ace15.safetensors:   larger/higher-quality alternative
#     (~8.4 GB); fetched too since the task context named both -- comment out
#     if you only ever use the 0.6b one.
# ===========================================================================
if want ace; then
    echo "== ACE-Step 1.5 (music) =="
    dl_hf Comfy-Org/ace_step_1.5_ComfyUI_files \
        split_files/diffusion_models/acestep_v1.5_turbo.safetensors \
        "$MODELS/diffusion_models" acestep_v1.5_turbo.safetensors

    dl_hf Comfy-Org/ace_step_1.5_ComfyUI_files \
        split_files/text_encoders/qwen_0.6b_ace15.safetensors \
        "$MODELS/text_encoders" qwen_0.6b_ace15.safetensors

    dl_hf Comfy-Org/ace_step_1.5_ComfyUI_files \
        split_files/text_encoders/qwen_4b_ace15.safetensors \
        "$MODELS/text_encoders" qwen_4b_ace15.safetensors

    dl_hf Comfy-Org/ace_step_1.5_ComfyUI_files \
        split_files/vae/ace_1.5_vae.safetensors \
        "$MODELS/vae" ace_1.5_vae.safetensors
fi

echo "Done."
