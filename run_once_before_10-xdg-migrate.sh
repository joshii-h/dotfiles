#!/bin/bash
# Einmalige Umzüge nach XDG (2026-10-26): Punkt-Ordner aus dem Home nach
# ~/.config, ~/.local/share, ~/.local/state bzw. ~/.cache. Die passenden
# Variablen setzt ~/.zshenv. Läuft vor dem Anwenden der Dateien, damit
# ~/.config/zsh/.zshrc gleich die verschobenen Pfade vorfindet.
# Idempotent: verschoben wird nur, was alt noch existiert und neu fehlt.
set -u
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}"

move() {  # move <alt> <neu>
  local src=$1 dst=$2
  [ -e "$src" ] || [ -L "$src" ] || return 0
  if [ -e "$dst" ]; then
    echo "xdg-migrate: $dst existiert schon – $src bleibt liegen" >&2
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  mv "$src" "$dst" && echo "xdg-migrate: $src → $dst"
}

move "$HOME/.oh-my-zsh"   "$DATA/oh-my-zsh"
move "$HOME/.zsh_history" "$STATE/zsh/history"
move "$HOME/completions"  "$DATA/zsh/completions"
move "$HOME/go"           "$DATA/go"
move "$HOME/.npm"         "$CACHE/npm"
move "$HOME/.npmrc"       "$CONF/npm/npmrc"
move "$HOME/.docker"      "$CONF/docker"
move "$HOME/.wget-hsts"   "$STATE/wget-hsts"
move "$HOME/.gtkrc-2.0"   "$CONF/gtk-2.0/gtkrc"
# rustup-Installationen haben ~/.cargo/env mit festen Pfaden – die nicht
# anfassen, nur reine Caches (Portage-/Distro-Rust) verschieben.
[ -e "$HOME/.cargo/env" ] || move "$HOME/.cargo" "$DATA/cargo"

rm -f "$HOME"/.zcompdump*
mkdir -p "$STATE/zsh" "$CACHE/zsh"
exit 0
