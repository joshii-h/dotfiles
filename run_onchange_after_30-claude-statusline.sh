#!/bin/sh
# Claude Code: Statuszeile (~/.claude/statusline-command.sh) in settings.json
# eintragen. Nur dieser eine Schlüssel – der Rest der Datei (Freigaben usw.)
# ist je Rechner verschieden und bleibt unangetastet.
settings="$HOME/.claude/settings.json"
command -v jq >/dev/null 2>&1 || { echo "claude-statusline: jq fehlt – statusLine nicht eingetragen" >&2; exit 0; }
mkdir -p "$HOME/.claude"
[ -s "$settings" ] || echo '{}' > "$settings"
want='{"type":"command","command":"bash $HOME/.claude/statusline-command.sh"}'
jq -e --argjson w "$want" '.statusLine == $w' "$settings" >/dev/null 2>&1 && exit 0
tmp="$settings.tmp.$$"
jq --argjson w "$want" '.statusLine = $w' "$settings" > "$tmp" && mv "$tmp" "$settings" || { rm -f "$tmp"; exit 1; }
