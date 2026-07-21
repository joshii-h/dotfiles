# msteams-pwa-handler

Öffnet `msteams://` Deeplinks in einer als **PWA** installierten Microsoft-Teams-Instanz
(via [firefoxpwa](https://github.com/filips123/PWAsForFirefox)) statt im nativen Desktop-Client.

Wenn du Teams nur als Progressive Web App nutzt, registriert nichts das `msteams://`-Schema
— Links aus Outlook, Kalender-Einladungen, anderen Apps laufen dann ins Leere. Dieses kleine
Werkzeug schließt die Lücke: Es schreibt `msteams:/l/…` nach `https://teams.microsoft.com/l/…`
um und öffnet das im PWA-Fenster.

## Voraussetzungen

- Linux mit einer freedesktop-Umgebung (`xdg-mime`, `xdg-utils`)
- [`firefoxpwa`](https://github.com/filips123/PWAsForFirefox)
- Microsoft Teams als firefoxpwa-PWA installiert (`https://teams.microsoft.com`)

## Installation

```bash
./install.sh
```

Standardmäßig nach `~/.local/bin` (Wrapper) und `~/.local/share/applications` (Handler).
Überschreibbar per `BIN_DIR=` / `APP_DIR=`.

Danach testen:

```bash
xdg-open "msteams:/l/chat/0/0?users=jemand@example.com"
```

## Deinstallation

```bash
./uninstall.sh
```

## Wie es funktioniert

Teams kennt zwei Link-Welten:

| Format | Wer registriert es |
|--------|--------------------|
| `msteams://` / `msteams:/l/…` | nativer Desktop-Client |
| `https://teams.microsoft.com/l/…` | Web / PWA |

Der Wrapper nimmt eine `msteams:`-URL entgegen, entfernt Schema und optionalen Host,
prependet `https://teams.microsoft.com` und startet die PWA damit
(`firefoxpwa site launch <id> --url <url>`). Die firefoxpwa-Site-ID wird zur Laufzeit
automatisch ermittelt (Match über die `teams.microsoft.com`-URL), lässt sich aber per
`MSTEAMS_PWA_ID=` erzwingen.

## Grenzen

- Nur die offiziellen **`/l/…`-Deeplinks** funktionieren zuverlässig — das ist das Format,
  das die Web-/PWA-Oberfläche versteht. Native-only `msteams://`-Aktionen haben kein
  Web-Äquivalent.
- Nur getestet mit firefoxpwa. Für Chrome/Edge-PWAs wäre ein anderer Launch-Aufruf nötig.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
