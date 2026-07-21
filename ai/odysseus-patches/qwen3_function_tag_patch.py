#!/usr/bin/env python3
"""qwen3_function_tag_patch.py

Idempotent, standalone patcher for Odysseus's src/tool_parsing.py.

qwen3-coder emits tool calls as text like:

    <function=web_search>
    <parameter=query>
    some query
    </parameter>
    </function>

Odysseus's parse_tool_blocks did not recognize that
<function=NAME>...<parameter=KEY>VALUE</parameter>...</function> shape, so on
local Ollama (where Odysseus omits the `tools` field for built-in tools, and
Ollama therefore never converts the text form to structured tool_calls) those
calls landed verbatim in content and were silently dropped — web_search etc.
never ran.

This script re-applies the parser patch after any Odysseus `git pull` /
rebuild. It is:

  - idempotent: no-ops if the marker `# ODYX-PATCH:qwen3-function-tag` is
    already present.
  - fail-loud: if any of the 4 verbatim anchor lines is missing (an upstream
    refactor moved them), it prints a clear error to stderr and exits non-zero
    WITHOUT writing — the file is never left half-patched or corrupted.
  - all-or-nothing: it only writes when all 4 anchors matched.

Usage:
    python3 qwen3_function_tag_patch.py /path/to/odysseus/src/tool_parsing.py
"""

import sys

MARKER = "# ODYX-PATCH:qwen3-function-tag"

# Each entry: (human name, verbatim anchor found in the file, replacement text
# that contains the anchor plus the inserted patch piece). The anchor must
# appear EXACTLY ONCE in the file.
PATCHES = [
    (
        "1a: regex constants",
        '_FUNCTION_MODEL_PARAMS_CLOSE_RE = re.compile(r"</parameters>", re.IGNORECASE)\n'
        '_QWEN_ROLE_MARKER_RE = re.compile(r"</?\\|(?:assistant|assistan|user|system|tool)\\|>?|</\\|end\\|>?", re.IGNORECASE)',
        '_FUNCTION_MODEL_PARAMS_CLOSE_RE = re.compile(r"</parameters>", re.IGNORECASE)\n'
        "# ODYX-PATCH:qwen3-function-tag\n"
        "# Pattern 3c: Llama-3.1 / Qwen3-Coder text-format function calls.\n"
        "# <function=NAME><parameter=KEY>VALUE</parameter>...</function> — qwen3-coder's\n"
        "# own text tool-call convention. Ollama's parser only converts it when the\n"
        "# request's `tools` field is populated; Odysseus omits `tools` for built-in\n"
        "# tools on local Ollama, so it lands verbatim in content and was dropped.\n"
        '_FUNCTION_TAG_OPEN_RE = re.compile(r"<function=([A-Za-z_][\\w-]*)>\\s*", re.IGNORECASE)\n'
        '_FUNCTION_TAG_CLOSE_RE = re.compile(r"</function>", re.IGNORECASE)\n'
        '_FUNCTION_TAG_PARAM_OPEN_RE = re.compile(r"<parameter=([A-Za-z_][\\w-]*)>", re.IGNORECASE)\n'
        '_QWEN_ROLE_MARKER_RE = re.compile(r"</?\\|(?:assistant|assistan|user|system|tool)\\|>?|</\\|end\\|>?", re.IGNORECASE)',
    ),
    (
        "1b: parse function",
        "    from src.tool_schemas import function_call_to_tool_block\n"
        "    return function_call_to_tool_block(tool_name, params)\n"
        "\n"
        "\n"
        "def _iter_delimited(text, open_re, close_re):",
        "    from src.tool_schemas import function_call_to_tool_block\n"
        "    return function_call_to_tool_block(tool_name, params)\n"
        "\n"
        "\n"
        "def _parse_function_tag_call(name: str, body: str) -> Optional[ToolBlock]:\n"
        '    """Parse <function=NAME><parameter=KEY>VALUE</parameter>...</function>\n'
        "    (qwen3-coder / Llama-3.1 text tool-calling). Delegates to\n"
        '    function_call_to_tool_block like every other text-format handler."""\n'
        "    tool_name = name.lower()\n"
        "    params = {}\n"
        "    for pname, pval in _iter_named_blocks(body, _FUNCTION_TAG_PARAM_OPEN_RE, _XML_PARAM_CLOSE_RE):\n"
        "        params[pname] = pval.strip()\n"
        "    from src.tool_schemas import function_call_to_tool_block\n"
        "    return function_call_to_tool_block(tool_name, json.dumps(params))\n"
        "\n"
        "\n"
        "def _iter_delimited(text, open_re, close_re):",
    ),
    (
        "1c: wire into parse_tool_blocks",
        "                block = _parse_xml_invoke(inv_name, inv_body)\n"
        "                if block:\n"
        "                    blocks.append(block)\n"
        "\n"
        "    # Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)",
        "                block = _parse_xml_invoke(inv_name, inv_body)\n"
        "                if block:\n"
        "                    blocks.append(block)\n"
        "\n"
        "    # Pattern 3c: <function=NAME><parameter=KEY>...</parameter></function> (qwen3-coder)\n"
        "    if not blocks:\n"
        "        for fn_name, fn_body in _iter_named_blocks(text, _FUNCTION_TAG_OPEN_RE, _FUNCTION_TAG_CLOSE_RE):\n"
        "            block = _parse_function_tag_call(fn_name, fn_body)\n"
        "            if block:\n"
        "                blocks.append(block)\n"
        "\n"
        "    # Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)",
    ),
    (
        "1d: mirror in strip_tool_blocks",
        "    cleaned = _strip_delimited(cleaned, _FUNCTION_MODEL_OPEN_RE, _FUNCTION_MODEL_CLOSE_RE)\n"
        "    cleaned = _strip_raw_openai_tool_call_json(cleaned)",
        "    cleaned = _strip_delimited(cleaned, _FUNCTION_MODEL_OPEN_RE, _FUNCTION_MODEL_CLOSE_RE)\n"
        "    cleaned = _strip_delimited(cleaned, _FUNCTION_TAG_OPEN_RE, _FUNCTION_TAG_CLOSE_RE)\n"
        "    cleaned = _strip_raw_openai_tool_call_json(cleaned)",
    ),
]


def main(argv):
    if len(argv) != 2:
        print("usage: qwen3_function_tag_patch.py <path/to/tool_parsing.py>",
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
        print(f"qwen3-function-tag patch already present in {target}; nothing to do.")
        return 0

    # Validate ALL anchors first — never write a partially patched file.
    patched = src
    for name, anchor, replacement in PATCHES:
        count = patched.count(anchor)
        if count == 0:
            print(
                f"ERROR: anchor for [{name}] not found in {target}. "
                "Upstream likely refactored tool_parsing.py; the qwen3-function-tag "
                "patch was NOT applied and the file was left unchanged.",
                file=sys.stderr,
            )
            return 1
        if count > 1:
            print(
                f"ERROR: anchor for [{name}] matched {count} times in {target} "
                "(expected exactly once); refusing to patch ambiguously. "
                "The file was left unchanged.",
                file=sys.stderr,
            )
            return 1
        patched = patched.replace(anchor, replacement, 1)

    if MARKER not in patched:
        print("ERROR: internal error — marker missing after applying patches; "
              "file left unchanged.", file=sys.stderr)
        return 1

    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(patched)
    except OSError as exc:
        print(f"ERROR: cannot write {target}: {exc}", file=sys.stderr)
        return 2

    print(f"qwen3-function-tag patch applied to {target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
