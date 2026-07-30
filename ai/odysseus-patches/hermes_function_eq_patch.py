#!/usr/bin/env python3
"""hermes_function_eq_patch.py

Idempotent, standalone patcher for Hermes Agent's
agent/transports/chat_completions.py.

Some local models — notably qwen3-coder via Ollama — sometimes emit tool calls
as literal *text* in the Qwen grammar:

    <function=web_search><parameter=query>zurich news</parameter></function>

instead of native OpenAI `tool_calls`.  Ollama only converts that text form into
structured `delta.tool_calls` when its template decides to; qwen3-coder is
inconsistent and periodically streams the block as plain `delta.content`.  In
that case Hermes' ChatCompletionsTransport.normalize_response() sees empty
`tool_calls`, and conversation_loop.py treats the raw `<function=...>` text as
the FINAL answer (observed: `hermes -z` printing a bare
`<function=terminal><parameter=command>curl ...` block).

This patch adds a fallback in the single normalization chokepoint shared by the
streamed and non-streamed Ollama / OpenAI-compatible paths: when there are no
native tool_calls but the content carries `<function=NAME>` blocks, reparse them
into ToolCall objects and strip them from the content.

The patch re-applies after any Hermes `git pull`.  It is:

  - idempotent: no-ops if the marker `HERMES-PATCH:qwen-function-eq` is present.
  - fail-loud: if any verbatim anchor is missing (upstream refactor moved it) it
    prints a clear error to stderr and exits non-zero WITHOUT writing — the file
    is never left half-patched.
  - all-or-nothing: it only writes when every anchor matched exactly once.

Usage:
    python3 hermes_function_eq_patch.py /path/to/agent/transports/chat_completions.py
"""

import sys

MARKER = "HERMES-PATCH:qwen-function-eq"

# The module-level helper + regexes, inserted just before the first top-level
# function.  Includes a mid-module `import re` (legal; the transport module does
# not otherwise import re) so the patch is fully self-contained.
_HELPER_BLOCK = '''import re  # noqa: E402  HERMES-PATCH:qwen-function-eq (self-contained fallback parser)

# HERMES-PATCH:qwen-function-eq
# Some local models (notably qwen3-coder via Ollama) sometimes emit tool calls
# as literal text in the Qwen grammar
#     <function=NAME><parameter=KEY>VALUE</parameter>...</function>
# instead of native `tool_calls`.  When Ollama does not convert that text form
# into structured tool_calls, it arrives as the assistant's `content` with an
# empty `tool_calls`, and the agent loop treats the raw block as the final
# answer.  _salvage_equals_function_calls() reparses those blocks so the loop
# executes them like any native tool call.
_QWEN_FUNC_EQ_RE = re.compile(
    r"<function=([A-Za-z_][\\w.\\-]*)\\s*>(.*?)</function\\s*>", re.DOTALL | re.IGNORECASE
)
_QWEN_PARAM_EQ_RE = re.compile(
    r"<parameter=([A-Za-z_][\\w.\\-]*)\\s*>(.*?)</parameter\\s*>", re.DOTALL | re.IGNORECASE
)


def _salvage_equals_function_calls(content):
    """Parse leaked ``<function=NAME>...<parameter=K>V</parameter>...</function>``
    text tool-calls into ToolCall objects.

    Returns ``(tool_calls | None, cleaned_content)``.  ``tool_calls`` is ``None``
    when no well-formed block is found (or the content fails the safety guards
    below), leaving the caller's content untouched.
    """
    if not isinstance(content, str) or "<function=" not in content:
        return None, content
    calls = []
    spans = []
    for m in _QWEN_FUNC_EQ_RE.finditer(content):
        name = (m.group(1) or "").strip()
        if not name:
            continue
        body = m.group(2) or ""
        args = {}
        pspans = []
        for pm in _QWEN_PARAM_EQ_RE.finditer(body):
            args[pm.group(1)] = (pm.group(2) or "").strip("\\n\\r")
            pspans.append((pm.start(), pm.end()))
        # Reject a block whose body carries non-whitespace text OUTSIDE its
        # <parameter=...> spans.  That means the block is malformed, or a VALUE
        # contained a literal </parameter>/</function> that closed it early --
        # executing it would run on silently-truncated arguments.  Bail on the
        # whole content rather than fire a corrupted call.
        residue = body
        for ps, pe in reversed(pspans):
            residue = residue[:ps] + residue[pe:]
        if residue.strip():
            return None, content
        calls.append(build_tool_call("salvage_%d" % len(calls), name, args))
        spans.append((m.start(), m.end()))
    if not calls:
        return None, content
    # Only salvage when the tool block(s) are the TAIL of the message -- nothing
    # but whitespace after the final </function>.  A block buried mid-content
    # with substantial trailing text is almost always the model quoting an
    # example or echoing file/tool output that contains the grammar, not
    # actually calling a tool; executing that is an injection vector.
    if content[spans[-1][1]:].strip():
        return None, content
    cleaned = content
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    return calls, cleaned.strip()


'''

# Each entry: (human name, verbatim anchor, replacement). The anchor must appear
# EXACTLY ONCE in the file.
PATCHES = [
    (
        "1: import build_tool_call",
        "from agent.transports.types import NormalizedResponse, ToolCall, Usage\n",
        "from agent.transports.types import NormalizedResponse, ToolCall, Usage, build_tool_call\n",
    ),
    (
        "2: helper + regexes",
        "def _reasoning_config_for_model(model: str, reasoning_config: dict | None) -> dict | None:\n",
        _HELPER_BLOCK
        + "def _reasoning_config_for_model(model: str, reasoning_config: dict | None) -> dict | None:\n",
    ),
    (
        "3: salvage in normalize_response",
        "        content = msg.content\n"
        '        refusal = getattr(msg, "refusal", None)\n',
        "        content = msg.content\n"
        "        # HERMES-PATCH:qwen-function-eq — salvage text-format tool calls\n"
        "        # (<function=NAME><parameter=K>V</parameter></function>) that some\n"
        "        # local models (qwen3-coder/Ollama) leak into content instead of\n"
        "        # emitting native tool_calls.\n"
        '        if not tool_calls and isinstance(content, str) and "<function=" in content:\n'
        "            _eq_calls, _eq_clean = _salvage_equals_function_calls(content)\n"
        "            if _eq_calls:\n"
        "                tool_calls = _eq_calls\n"
        "                content = _eq_clean or None\n"
        '                if finish_reason in (None, "stop"):\n'
        '                    finish_reason = "tool_calls"\n'
        '        refusal = getattr(msg, "refusal", None)\n',
    ),
]


def main(argv):
    if len(argv) != 2:
        print("usage: hermes_function_eq_patch.py <path/to/chat_completions.py>",
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
        print(f"qwen-function-eq patch already present in {target}; nothing to do.")
        return 0

    # Validate ALL anchors first — never write a partially patched file.
    patched = src
    for name, anchor, replacement in PATCHES:
        count = patched.count(anchor)
        if count == 0:
            print(
                f"ERROR: anchor for [{name}] not found in {target}. "
                "Upstream likely refactored chat_completions.py; the "
                "qwen-function-eq patch was NOT applied and the file was left "
                "unchanged.",
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

    print(f"qwen-function-eq patch applied to {target}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
