#!/usr/bin/env bash
# Entfernt msteams-pwa-handler wieder.
set -euo pipefail

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
APP_DIR="${APP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/applications}"
WRAPPER_NAME="msteams-pwa-handler"
DESKTOP_NAME="msteams-pwa-handler.desktop"

rm -fv "$BIN_DIR/$WRAPPER_NAME" "$APP_DIR/$DESKTOP_NAME"
command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true

echo
echo "Entfernt. Hinweis: xdg-mime kann eine Default-Zuordnung nicht 'zurücksetzen';"
echo "die Zeile 'x-scheme-handler/msteams=$DESKTOP_NAME' bleibt ggf. in"
echo "  \${XDG_CONFIG_HOME:-~/.config}/mimeapps.list"
echo "stehen, ist aber ohne den .desktop-Eintrag wirkungslos. Bei Bedarf dort löschen."
