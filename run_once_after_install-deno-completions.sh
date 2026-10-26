#!/bin/bash
# Generate deno zsh completions if deno is installed

if ! command -v deno &>/dev/null; then
    echo "deno not found, skipping completions"
    exit 0
fi

mkdir -p "${XDG_DATA_HOME:-$HOME/.local/share}/zsh/completions"
mkdir -p "${XDG_DATA_HOME:-$HOME/.local/share}/oh-my-zsh/custom/plugins/deno"

deno completions zsh > "${XDG_DATA_HOME:-$HOME/.local/share}/zsh/completions/_deno"
cp "${XDG_DATA_HOME:-$HOME/.local/share}/zsh/completions/_deno" "${XDG_DATA_HOME:-$HOME/.local/share}/oh-my-zsh/custom/plugins/deno/_deno"

echo "deno completions installed"
