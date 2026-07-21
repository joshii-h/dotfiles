#!/usr/bin/env python3
"""searxng_engines_patch.py

Idempotent, standalone patcher for Odysseus's bundled SearXNG settings
template: config/searxng/settings.yml.

The stock Odysseus template ships NO `engines:` list, so the searxng container
falls back to SearXNG's default engine set. For general web/news search that
returns a lot of SEO recap fluff. This script injects a curated engine list
(ported from a well-tuned self-hosted instance, then adapted for a
residential/LTE IP where DuckDuckGo, Qwant and Reddit are NOT datacenter-IP
blocked) plus a few search-quality knobs (safe_search off, google autocomplete,
auto language). News engines (google_news weight 1.2, bing_news, yahoo_news)
are kept prominent — news quality is the point.

It preserves everything Odysseus needs:
  - the `__SEARXNG_SECRET__` placeholder (the compose entrypoint sed-substitutes
    it on first boot / regen),
  - `formats: [html, json]` (Odysseus queries the JSON API),
  - `use_default_settings: true` (curated engines override defaults by name).

The template lives inside the Odysseus git clone, so `git pull` in ai-update.sh
reverts it. This script re-applies the injection after every pull. It is:

  - idempotent: no-ops if the marker `# ODYX-PATCH:searxng-engines` is already
    present.
  - fail-loud: if the anchor `search:` block is missing (an upstream refactor
    changed the template), it prints a clear error to stderr and exits non-zero
    WITHOUT writing — the file is never left half-patched or corrupted.
  - all-or-nothing: it only writes when the anchor matched exactly once.

Usage:
    python3 searxng_engines_patch.py /path/to/odysseus/config/searxng/settings.yml
"""

import sys

MARKER = "# ODYX-PATCH:searxng-engines"

# The stock template's `search:` block. Anchored verbatim; must appear EXACTLY
# ONCE. We replace it with (a) the same block plus search-quality tuning and the
# idempotency marker, followed by (b) the curated `engines:` list. `engines:` is
# a top-level YAML key, so appending it right after the search block is valid.
ANCHOR = (
    "search:\n"
    "  formats:\n"
    "    - html\n"
    "    - json\n"
)

REPLACEMENT = (
    MARKER + "  (curated engines + news + search-quality tuning)\n"
    "# Ported from the self-hosted vServer instance, adapted for this\n"
    "# residential/LTE box: duckduckgo weight back to 1.0, and qwant + reddit\n"
    "# re-enabled (the VPS disabled/down-weighted them only for datacenter-IP\n"
    "# CAPTCHA/blocks, which do not apply here). Re-injected by\n"
    "# odysseus-patches/searxng_engines_patch.py after each Odysseus git pull.\n"
    "#\n"
    "# odysseus-local-searxng-json-2026-05-30\n"
    "# ^ regen sentinel: the compose entrypoint regenerates /etc/searxng/\n"
    "# settings.yml from this template whenever the GENERATED file still contains\n"
    "# this string (it survives the secret sed-substitution because it is a\n"
    "# comment). Keeping it here makes template edits take effect on every\n"
    "# searxng restart instead of freezing after first boot.\n"
    "search:\n"
    "  safe_search: 0\n"
    '  autocomplete: "google"\n'
    "  autocomplete_min: 2\n"
    '  default_lang: "auto"\n'
    "  formats:\n"
    "    - html\n"
    "    - json\n"
    "\n"
    "engines:\n"
    "  # --- General (weighted) ---\n"
    "  # Google: zuverlaessigster, hoch gewichtet\n"
    "  - name: google\n"
    "    engine: google\n"
    "    shortcut: g\n"
    "    use_mobile_ui: true\n"
    "    disabled: false\n"
    "    weight: 1.0\n"
    "\n"
    "  # Startpage: Google-Proxy, hoch gewichtet\n"
    "  - name: startpage\n"
    "    engine: startpage\n"
    "    shortcut: sp\n"
    "    disabled: false\n"
    "    weight: 2.0\n"
    "\n"
    "  # Brave: eigener Index, wertvoll\n"
    "  - name: brave\n"
    "    engine: brave\n"
    "    shortcut: br\n"
    "    disabled: false\n"
    "    weight: 1.0\n"
    "\n"
    "  # DuckDuckGo: hier NICHT VPS-CAPTCHA-geblockt (residential IP) -> volle Gewichtung\n"
    "  - name: duckduckgo\n"
    "    engine: duckduckgo\n"
    "    shortcut: ddg\n"
    "    disabled: false\n"
    "    weight: 1.0\n"
    "\n"
    "  # Bing: oft irrelevant (#4964)\n"
    "  - name: bing\n"
    "    engine: bing\n"
    "    shortcut: bi\n"
    "    disabled: false\n"
    "    weight: 0.5\n"
    "\n"
    "  # Qwant: auf VPS blockiert (#3929), hier (residential IP) reaktiviert\n"
    "  - name: qwant\n"
    "    engine: qwant\n"
    "    shortcut: qw\n"
    "    disabled: false\n"
    "\n"
    "  - name: mojeek\n"
    "    engine: mojeek\n"
    "    shortcut: mj\n"
    "    disabled: true\n"
    "\n"
    "  - name: yahoo\n"
    "    engine: yahoo\n"
    "    shortcut: yh\n"
    "    disabled: true\n"
    "\n"
    "  # --- News ---\n"
    "  - name: google news\n"
    "    engine: google_news\n"
    "    shortcut: gn\n"
    "    disabled: false\n"
    "    weight: 1.2\n"
    "\n"
    "  - name: bing news\n"
    "    engine: bing_news\n"
    "    shortcut: bin\n"
    "    disabled: false\n"
    "\n"
    "  - name: yahoo news\n"
    "    engine: yahoo_news\n"
    "    shortcut: yhn\n"
    "    disabled: false\n"
    "\n"
    "  # --- Social ---\n"
    "  # Reddit: auf VPS blockiert (#3444), hier (residential IP) reaktiviert\n"
    "  - name: reddit\n"
    "    engine: reddit\n"
    "    shortcut: re\n"
    "    disabled: false\n"
    "\n"
    "  - name: hackernews\n"
    "    engine: hackernews\n"
    "    shortcut: hn\n"
    "    disabled: false\n"
    "\n"
    "  # --- IT / Dev ---\n"
    "  - name: github\n"
    "    engine: github\n"
    "    shortcut: gh\n"
    "    disabled: false\n"
    "\n"
    "  - name: gitlab\n"
    "    engine: gitlab\n"
    "    shortcut: gl\n"
    "    disabled: false\n"
    "\n"
    "  - name: stackoverflow\n"
    "    engine: stackexchange\n"
    "    shortcut: so\n"
    '    api_site_url: "https://api.stackexchange.com"\n'
    '    site: "stackoverflow.com"\n'
    "    disabled: false\n"
    "    weight: 1.1\n"
    "\n"
    "  - name: superuser\n"
    "    engine: stackexchange\n"
    "    shortcut: su\n"
    '    api_site_url: "https://api.stackexchange.com"\n'
    '    site: "superuser.com"\n'
    "    disabled: false\n"
    "\n"
    "  - name: arch wiki\n"
    "    engine: archlinux\n"
    "    shortcut: aw\n"
    "    disabled: false\n"
    "\n"
    "  - name: docker hub\n"
    "    engine: docker_hub\n"
    "    shortcut: dh\n"
    "    disabled: false\n"
    "\n"
    "  - name: pkg.go.dev\n"
    "    engine: pkg_go_dev\n"
    "    shortcut: go\n"
    "    disabled: false\n"
    "\n"
    "  # --- Wiki / Knowledge ---\n"
    "  - name: wikipedia\n"
    "    engine: wikipedia\n"
    "    shortcut: wp\n"
    "    disabled: false\n"
    "    weight: 1.2\n"
    "\n"
    "  - name: wikidata\n"
    "    engine: wikidata\n"
    "    shortcut: wd\n"
    "    disabled: false\n"
    "\n"
    "  - name: currency\n"
    "    engine: currency_convert\n"
    "    shortcut: cc\n"
    "    disabled: false\n"
    "\n"
    "  # --- Packages ---\n"
    "  - name: pypi\n"
    "    engine: pypi\n"
    "    shortcut: pypi\n"
    "    disabled: false\n"
    "\n"
    "  - name: crates.io\n"
    "    engine: crates\n"
    "    shortcut: crt\n"
    "    disabled: true\n"
    "\n"
    "  # --- Maps ---\n"
    "  - name: openstreetmap\n"
    "    engine: openstreetmap\n"
    "    shortcut: osm\n"
    "    disabled: false\n"
    "\n"
    "  # --- Wissenschaft ---\n"
    "  - name: arxiv\n"
    "    engine: arxiv\n"
    "    shortcut: ax\n"
    "    disabled: true\n"
    "\n"
    "  - name: google scholar\n"
    "    engine: google_scholar\n"
    "    shortcut: gs\n"
    "    disabled: true\n"
    "\n"
    "  - name: semantic scholar\n"
    "    engine: semantic_scholar\n"
    "    shortcut: ss\n"
    "    disabled: true\n"
    "\n"
    "  # --- Bilder ---\n"
    "  - name: google images\n"
    "    engine: google_images\n"
    "    shortcut: gi\n"
    "    disabled: false\n"
    "\n"
    "  - name: bing images\n"
    "    engine: bing_images\n"
    "    shortcut: bii\n"
    "    disabled: false\n"
    "\n"
    "  - name: unsplash\n"
    "    engine: unsplash\n"
    "    shortcut: us\n"
    "    disabled: false\n"
    "\n"
    "  # --- Videos ---\n"
    "  - name: google videos\n"
    "    engine: google_videos\n"
    "    shortcut: gv\n"
    "    disabled: false\n"
    "\n"
    "  - name: youtube\n"
    "    engine: youtube_noapi\n"
    "    shortcut: yt\n"
    "    disabled: false\n"
    "\n"
    "  - name: dailymotion\n"
    "    engine: dailymotion\n"
    "    shortcut: dm\n"
    "    disabled: false\n"
    "\n"
    "  # --- Musik ---\n"
    "  - name: genius\n"
    "    engine: genius\n"
    "    shortcut: gns\n"
    "    disabled: false\n"
)


def main(argv):
    if len(argv) != 2:
        print("usage: searxng_engines_patch.py <path/to/config/searxng/settings.yml>",
              file=sys.stderr)
        return 2

    target = argv[1]
    try:
        with open(target, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read {target}: {exc}", file=sys.stderr)
        return 2

    if MARKER in src:
        print(f"searxng-engines patch already present in {target}; nothing to do.")
        return 0

    count = src.count(ANCHOR)
    if count == 0:
        print(
            f"ERROR: `search:` anchor block not found in {target}. Upstream likely "
            "changed the SearXNG settings template; the searxng-engines patch was "
            "NOT applied and the file was left unchanged.",
            file=sys.stderr,
        )
        return 1
    if count > 1:
        print(
            f"ERROR: `search:` anchor block matched {count} times in {target} "
            "(expected exactly once); refusing to patch ambiguously. The file was "
            "left unchanged.",
            file=sys.stderr,
        )
        return 1

    patched = src.replace(ANCHOR, REPLACEMENT, 1)

    if MARKER not in patched or "__SEARXNG_SECRET__" not in patched:
        print("ERROR: internal error — marker or __SEARXNG_SECRET__ placeholder "
              "missing after patch; file left unchanged.", file=sys.stderr)
        return 1

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(patched)
    except OSError as exc:
        print(f"ERROR: cannot write {target}: {exc}", file=sys.stderr)
        return 2

    print(f"searxng-engines patch applied to {target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
