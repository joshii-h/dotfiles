#!/usr/bin/env bash
# Installiert msteams-pwa-handler als Handler für msteams:// Links.
#
#   ./install.sh            # nach ~/.local/bin bzw. ~/.local/share/applications
#   BIN_DIR=~/bin ./install.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
APP_DIR="${APP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/applications}"
WRAPPER_NAME="msteams-pwa-handler"
DESKTOP_NAME="msteams-pwa-handler.desktop"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# --- Voraussetzungen ------------------------------------------------------
command -v firefoxpwa >/dev/null 2>&1 \
    || die "firefoxpwa nicht gefunden. Bitte zuerst firefoxpwa installieren und Teams als PWA einrichten."

command -v xdg-mime >/dev/null 2>&1 \
    || die "xdg-mime nicht gefunden (Paket xdg-utils)."

# Teams-PWA-ID ermitteln (nur für das Icon; der Wrapper detektiert selbst zur Laufzeit).
PWA_ID="$(firefoxpwa profile list 2>/dev/null \
    | grep -iE 'teams\.microsoft\.com' \
    | grep -oE '\([0-9A-Z]{26}\)' | tr -d '()' | head -n1 || true)"

if [[ -n "$PWA_ID" ]]; then
    ICON="FFPWA-${PWA_ID}"
    info "Teams-PWA gefunden (ID: ${PWA_ID})."
else
    ICON="applications-internet"
    warn "Keine Teams-PWA gefunden. Installation läuft weiter; der Handler funktioniert,"
    warn "sobald Teams als firefoxpwa-PWA installiert ist."
fi

# --- Wrapper platzieren ---------------------------------------------------
mkdir -p "$BIN_DIR" "$APP_DIR"
install -m 0755 "$SRC_DIR/$WRAPPER_NAME" "$BIN_DIR/$WRAPPER_NAME"
info "Wrapper installiert: $BIN_DIR/$WRAPPER_NAME"

# --- .desktop erzeugen ----------------------------------------------------
sed -e "s|@@EXEC@@|$BIN_DIR/$WRAPPER_NAME|g" \
    -e "s|@@ICON@@|$ICON|g" \
    "$SRC_DIR/$DESKTOP_NAME.in" > "$APP_DIR/$DESKTOP_NAME"
info "Desktop-Eintrag installiert: $APP_DIR/$DESKTOP_NAME"

# --- Als Default-Handler registrieren ------------------------------------
xdg-mime default "$DESKTOP_NAME" x-scheme-handler/msteams
command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true

REGISTERED="$(xdg-mime query default x-scheme-handler/msteams || true)"
if [[ "$REGISTERED" == "$DESKTOP_NAME" ]]; then
    info "Registriert als Standard-Handler für msteams://"
else
    warn "Handler-Registrierung nicht bestätigt (query lieferte: '${REGISTERED:-leer}')."
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR liegt nicht im PATH — der Handler funktioniert trotzdem (absoluter Pfad im .desktop),"
    warn "aber für den direkten Aufruf '$WRAPPER_NAME' solltest du $BIN_DIR ins PATH aufnehmen."
fi

cat <<EOF

Fertig. Test:
  xdg-open "msteams:/l/chat/0/0?users=jemand@example.com"
EOF
