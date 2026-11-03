#!/usr/bin/env bash
# Claude Code Statuszeile im Stil des starship-Prompts.
#
# Liest das statusLine-JSON von stdin: Modell, Kontext, Kosten, geänderte
# Zeilen, Dauer und die Limits des Abos. Fehlende Felder fallen einfach weg.
#
# Farben: ~/.cache/matugen/claude-statusline.sh (von DMS/matugen aus dem
# Hintergrundbild erzeugt). Ohne die Datei gilt die Palette unten, das Skript
# läuft also auch ohne DMS, auf jedem System mit bash und jq.
#
# CLAUDE_STATUSLINE_PLAIN=1: ohne Nerd-Font-Symbole (schlichte Trenner).
# Symbole als UTF-8-Bytes (\xNN): bash 3.2 (macOS) kennt kein \u in $'…'.
#
# chezmoi-managed: ~/Projects/dotfiles/dot_claude/statusline-command.sh

input=$(cat)
# Zahlen aus dem JSON haben einen Punkt; mit de_*-Locale würde printf sonst scheitern.
export LC_NUMERIC=C
command -v jq >/dev/null 2>&1 || { printf 'Claude Code'; exit 0; }

CS_PRIMARY_BG="#3a4a6b" CS_PRIMARY_FG="#d8e2ff"
CS_SECONDARY_BG="#3e4759" CS_SECONDARY_FG="#dae2f9"
CS_TERTIARY_BG="#563e5c" CS_TERTIARY_FG="#fbd7fc"
CS_SURFACE_BG="#292a2f" CS_SURFACE_FG="#e3e2e9"
CS_ERROR_BG="#93000a" CS_ERROR_FG="#ffdad6"
colors="${XDG_CACHE_HOME:-$HOME/.cache}/matugen/claude-statusline.sh"
# shellcheck source=/dev/null
[ -r "$colors" ] && . "$colors"
CS_WARN_FG="#f0c674"

fg() { local h=${1#\#}; printf '\033[38;2;%d;%d;%dm' "0x${h:0:2}" "0x${h:2:2}" "0x${h:4:2}"; }
bg() { local h=${1#\#}; printf '\033[48;2;%d;%d;%dm' "0x${h:0:2}" "0x${h:2:2}" "0x${h:4:2}"; }
RESET=$'\033[0m'

if [ "${CLAUDE_STATUSLINE_PLAIN:-0}" = 1 ]; then
  CAP_L="" CAP_R="" ARROW="" I_MODEL="" I_CTX="ctx " I_COST="\$" I_DUR="" I_LIMIT=""
else
  CAP_L=$'\xee\x82\xb6' CAP_R=$'\xee\x82\xb4' ARROW=$'\xee\x82\xb0'
  I_MODEL=$'\xf3\xb0\x9a\xa9 ' I_CTX=$'\xef\x92\x9b ' I_COST=$'\xef\x85\x95 ' I_DUR=$'\xef\x80\x97 ' I_LIMIT=$'\xef\x83\xa4 '
fi

# Eine jq-Abfrage für alle Felder, getrennt durch Tabs.
IFS=$'\t' read -r model used size tokens cost added removed dur five week < <(
  printf '%s' "$input" | jq -r '[
    (.model.display_name // .model.id // "Claude"),
    (.context_window.used_percentage // ""),
    (.context_window.context_window_size // ""),
    (.context_window.total_input_tokens // ""),
    (.cost.total_cost_usd // ""),
    (.cost.total_lines_added // ""),
    (.cost.total_lines_removed // ""),
    (.cost.total_duration_ms // ""),
    (.rate_limits.five_hour.used_percentage // ""),
    (.rate_limits.seven_day.used_percentage // "")
  ] | map(tostring) | join("\t")' 2>/dev/null)

model=${model:-Claude}

human() {
  awk -v n="$1" 'BEGIN {
    if (n >= 1000000) { printf "%.3gM", n / 1000000; exit }
    if (n >= 1000)    { printf "%.0fk", n / 1000; exit }
    printf "%d", n }'
}

# Abschnitte: Hintergrund, Vordergrund, Text. Zwischen zwei Abschnitten steht
# ein Pfeil in der Farbe des linken auf dem Hintergrund des rechten.
segs=()
add() { segs+=("$1|$2|$3"); }

add "$CS_PRIMARY_BG" "$CS_PRIMARY_FG" "${I_MODEL}${model}"

if [ -n "$used" ]; then
  used_int=$(printf '%.0f' "$used")
  ctx="${I_CTX}${used_int}%"
  [ -n "$tokens" ] && [ -n "$size" ] && ctx+=" · $(human "$tokens")/$(human "$size")"
  if [ "$used_int" -ge 80 ]; then
    add "$CS_ERROR_BG" "$CS_ERROR_FG" "$ctx"
  elif [ "$used_int" -ge 50 ]; then
    add "$CS_SECONDARY_BG" "$CS_WARN_FG" "$ctx"
  else
    add "$CS_SECONDARY_BG" "$CS_SECONDARY_FG" "$ctx"
  fi
fi

work=""
[ -n "$cost" ] && work="${I_COST}$(printf '%.2f' "$cost")"
if [ -n "$added" ] || [ -n "$removed" ]; then
  work+="${work:+  }+${added:-0} −${removed:-0}"
fi
if [ -n "$dur" ]; then
  min=$(( ${dur%.*} / 60000 ))
  if [ "$min" -ge 60 ]; then d="$((min / 60))h$(printf '%02d' $((min % 60)))m"; else d="${min}m"; fi
  work+="${work:+  }${I_DUR}${d}"
fi
[ -n "$work" ] && add "$CS_TERTIARY_BG" "$CS_TERTIARY_FG" "$work"

limits=""
[ -n "$five" ] && limits="5h $(printf '%.0f' "$five")%"
[ -n "$week" ] && limits+="${limits:+ · }7d $(printf '%.0f' "$week")%"
[ -n "$limits" ] && add "$CS_SURFACE_BG" "$CS_SURFACE_FG" "${I_LIMIT}${limits}"

out="" prev=""
for s in "${segs[@]}"; do
  IFS='|' read -r b f text <<<"$s"
  if [ -z "$prev" ]; then
    out+="$(fg "$b")${CAP_L}"
  elif [ -n "$ARROW" ]; then
    out+="$(fg "$prev")$(bg "$b")${ARROW}"
  else
    out+="${RESET} "
  fi
  out+="$(bg "$b")$(fg "$f") ${text} "
  prev=$b
done
[ -n "$prev" ] && out+="${RESET}$(fg "$prev")${CAP_R}${RESET}"
printf '%s' "$out"
