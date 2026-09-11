#!/usr/bin/env python3
"""Homelab-MCP — Hermes bekommt Zugriff auf die vServer-Dienste.

Bewusst LEAN gehalten (qwen3 ertrinkt ab ~90 Tools): nur wenige, hochwertige
Tools. Aktuell Immich (Fotosuche) + AdGuard Home (DNS-Filter). Weitere Dienste
(Uptime Kuma, Forgejo, Matrix) kommen inkrementell dazu.

Alle Dienste werden per API-Key/Basic-Auth angesprochen (kommt an Authentik-SSO
vorbei). Secrets liegen in ~/ai/.* (chmod 600), NICHT in git/config:
  ~/ai/.immich_api_key   -> Immich-API-Key (Immich: Konto -> API-Schlüssel)
  ~/ai/.adguard_auth     -> "benutzer:passwort" des AdGuard-Admins

Start: ~/ai/homelab-mcp/run.sh  (oder OpenRC-Service analog media-mcp)
Transport: SSE auf 0.0.0.0:8766
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
PORT = int(os.environ.get("HOMELAB_MCP_PORT", "8766"))
IMMICH_URL = os.environ.get("IMMICH_URL", "https://photos.joshuahirsig.xyz").rstrip("/")
ADGUARD_URL = os.environ.get("ADGUARD_URL", "https://dns.joshuahirsig.xyz").rstrip("/")
IMMICH_WEB = os.environ.get("IMMICH_WEB", IMMICH_URL).rstrip("/")

AI = os.path.expanduser("~/ai")
IMMICH_KEY_FILE = os.path.join(AI, ".immich_api_key")
ADGUARD_AUTH_FILE = os.path.join(AI, ".adguard_auth")

HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------- #
# Secrets + HTTP-Helfer (stdlib, keine schweren Deps)
# --------------------------------------------------------------------------- #
def _read_secret(path: str, hint: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
        if not val:
            raise ValueError("leer")
        return val
    except Exception:
        raise RuntimeError(
            f"Secret fehlt/leer: {path}. {hint}"
        )


def _http(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """Führt einen JSON-HTTP-Request aus und gibt das geparste JSON zurück."""
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code} bei {url}: {detail}")
    except Exception as exc:
        raise RuntimeError(f"Verbindungsfehler bei {url}: {exc}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:500]}


def _immich_headers() -> dict:
    return {"x-api-key": _read_secret(
        IMMICH_KEY_FILE,
        "In Immich: Konto-Einstellungen -> API-Schlüssel -> neuen Key erzeugen, "
        "dann `echo -n '<KEY>' > ~/ai/.immich_api_key && chmod 600 ~/ai/.immich_api_key`.",
    )}


def _adguard_headers() -> dict:
    cred = _read_secret(
        ADGUARD_AUTH_FILE,
        "`echo -n 'ADMIN:PASSWORT' > ~/ai/.adguard_auth && chmod 600 ~/ai/.adguard_auth` "
        "(AdGuard-Admin-Login).",
    )
    token = base64.b64encode(cred.encode()).decode()
    return {"Authorization": "Basic " + token}


# --------------------------------------------------------------------------- #
mcp = FastMCP("battlestation-homelab", host="0.0.0.0", port=PORT)


# --------------------------------------------------------------------------- #
# Immich — Fotosuche (die KI/CLIP-Suche macht Immich selbst)
# --------------------------------------------------------------------------- #
@mcp.tool()
def immich_search_photos(query: str, limit: int = 20) -> str:
    """Durchsucht die private Immich-Fotobibliothek per KI-Bildsuche (CLIP) nach
    einer natürlichsprachlichen Beschreibung. Nutze dies für „finde Fotos von …",
    z.B. „Skiurlaub", „Sonnenuntergang am Meer", „Geburtstagstorte". Liefert
    Aufnahmedatum, Dateiname und einen Web-Link je Treffer.
    """
    limit = max(1, min(int(limit), 100))
    res = _http("POST", f"{IMMICH_URL}/api/search/smart", _immich_headers(),
                {"query": query})
    assets = (((res.get("assets") or {}).get("items")) or [])[:limit]
    if not assets:
        return f"Keine Fotos gefunden für: {query!r}"
    lines = [f"{len(assets)} Treffer für {query!r}:"]
    for a in assets:
        aid = a.get("id", "")
        date = (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10]
        name = a.get("originalFileName", "")
        lines.append(f"- {date}  {name}  {IMMICH_WEB}/photos/{aid}")
    return "\n".join(lines)


@mcp.tool()
def immich_search_by_person(name: str, limit: int = 20) -> str:
    """Findet Fotos einer bestimmten Person in Immich anhand ihres Namens
    (Gesichtserkennung). Die Person muss in Immich benannt sein. Nutze dies für
    „Fotos von <Name>". Liefert Aufnahmedatum, Dateiname und Web-Link je Treffer.
    """
    limit = max(1, min(int(limit), 100))
    people = _http("GET", f"{IMMICH_URL}/api/people?withHidden=false&size=1000",
                   _immich_headers())
    plist = people.get("people") if isinstance(people, dict) else people
    plist = plist or []
    q = name.strip().lower()
    match = next((p for p in plist if q == (p.get("name", "").lower())), None) \
        or next((p for p in plist if q in (p.get("name", "").lower())), None)
    if not match:
        named = [p.get("name") for p in plist if p.get("name")]
        return (f"Keine benannte Person passend zu {name!r} gefunden. "
                f"Bekannte Namen: {', '.join(named[:30]) or '(keine)'}")
    pid = match.get("id")
    res = _http("POST", f"{IMMICH_URL}/api/search/metadata", _immich_headers(),
                {"personIds": [pid]})
    assets = (((res.get("assets") or {}).get("items")) or [])[:limit]
    if not assets:
        return f"Keine Fotos von {match.get('name')!r} gefunden."
    lines = [f"{len(assets)} Fotos von {match.get('name')!r}:"]
    for a in assets:
        aid = a.get("id", "")
        date = (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10]
        name_ = a.get("originalFileName", "")
        lines.append(f"- {date}  {name_}  {IMMICH_WEB}/photos/{aid}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# AdGuard Home — DNS-Filter
# --------------------------------------------------------------------------- #
@mcp.tool()
def adguard_top_blocked(limit: int = 10) -> str:
    """Zeigt die am häufigsten von AdGuard Home geblockten Domains sowie
    Gesamt-Statistik (Anfragen, geblockt, Blockrate). Nutze dies für „was wird
    am meisten geblockt?" oder „wie viel blockt AdGuard?".
    """
    limit = max(1, min(int(limit), 50))
    st = _http("GET", f"{ADGUARD_URL}/control/stats", _adguard_headers())
    total = st.get("num_dns_queries", 0)
    blocked = st.get("num_blocked_filtering", 0)
    rate = (blocked / total * 100) if total else 0.0
    top = st.get("top_blocked_domains") or []
    lines = [f"AdGuard: {total} Anfragen, {blocked} geblockt ({rate:.1f}%).",
             f"Top {min(limit, len(top))} geblockte Domains:"]
    for entry in top[:limit]:
        if isinstance(entry, dict):
            for dom, cnt in entry.items():
                lines.append(f"- {dom}: {cnt}")
    return "\n".join(lines)


@mcp.tool()
def adguard_block_domain(domain: str) -> str:
    """Sperrt eine Domain dauerhaft in AdGuard Home, indem eine
    Custom-Filterregel `||domain^` ergänzt wird (bestehende Regeln bleiben
    erhalten). Nutze dies für „sperre <domain>" / „blockiere <domain>".
    """
    domain = domain.strip().lower().lstrip("*.")
    if not domain or "/" in domain or " " in domain:
        return f"Ungültige Domain: {domain!r}"
    rule = f"||{domain}^"
    hdr = _adguard_headers()
    status = _http("GET", f"{ADGUARD_URL}/control/filtering/status", hdr)
    rules = list(status.get("user_rules") or [])
    if any(domain in r for r in rules):
        return f"{domain} ist bereits in den Filterregeln enthalten."
    rules.append(rule)
    _http("POST", f"{ADGUARD_URL}/control/filtering/set_rules", hdr, {"rules": rules})
    return f"Gesperrt: {domain} (Regel `{rule}` ergänzt, {len(rules)} Regeln gesamt)."


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    mcp.run(transport="sse")
