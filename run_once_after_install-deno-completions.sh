#!/bin/bash
# Generate deno zsh completions if deno is installed

if ! command -v deno &>/dev/null; then
    echo "deno not found, skipping completions"
    exit 0
fi

mkdir -p "$HOME/completions"
mkdir -p "$HOME/.oh-my-zsh/custom/plugins/deno"

deno completions zsh > "$HOME/completions/_deno"
cp "$HOME/completions/_deno" "$HOME/.oh-my-zsh/custom/plugins/deno/_deno"

echo "deno completions installed"
