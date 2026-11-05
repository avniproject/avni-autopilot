"""Mark rule code in a chat reply that no rule tool returned as suggested.

Rule code reaches the user safely only through a `suggest_*` tool, whose
result passed the validator. Code the model writes or edits itself skipped
that check, so every such block in a reply gets a visible note under it.

Fenced blocks are found the way a CommonMark renderer (the chat panel uses
react-markdown) finds them: backtick or tilde fences, an optional space
before the language tag, fences inside a `>` quote, and a fence left open
until the end of the reply. A block counts as rule code when its language is
a JavaScript dialect; when it is untagged, plain text or markdown and its
code mentions Avni rule names; or, in any other language (diff, json, an
unknown tag), when it reads like a full rule. A JSON or Python block that only
mentions a rule name is left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNCHECKED_RULE_CODE_NOTE = (
    "> ⚠️ **NOT CHECKED — do not paste this code into a form.** "
    "It did not come from the rule tool, so nothing checked it against the "
    "form's fields."
)

_QUOTE_PREFIX_RE = re.compile(r"^(?:[ \t]*>[ \t]?)*")
_OPENING_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$")
_LANGUAGE_RE = re.compile(r"[A-Za-z0-9_+-]*")
_RULE_LANGUAGES: frozenset[str] = frozenset({
    "js", "javascript", "jsx", "mjs", "cjs", "ts", "typescript", "tsx",
})
_PLAIN_LANGUAGES: frozenset[str] = frozenset({
    "", "text", "txt", "plain", "plaintext", "none", "md", "markdown",
})
_FULL_RULE_RE = re.compile(
    r"\(\s*\{\s*(?:params\s*,\s*imports|imports\s*,\s*params)\s*\}\s*\)\s*=>"
    r"|\.build\(\s*\)"
)
_RULE_MARKERS: tuple[str, ...] = (
    "imports.rulesConfig", "params.entity", "params.formElement",
    "RuleCondition", "FormElementStatus",
)


@dataclass(frozen=True)
class _FencedBlock:
    start_line: int
    end_line: int  # index of the closing fence line, or len(lines) when unclosed
    language: str
    code: str
    closed: bool
    fence: str
    indent: str
    quote_prefix: str


def normalize_rule_code(js: str) -> str:
    """Whitespace-insensitive form used to compare a reply's code with tool output."""
    return "".join(js.split())


def mark_unchecked_rule_code(text: str, checked_rule_code: set[str]) -> str:
    """Append the not-checked note after each rule code block not in `checked_rule_code`.

    `checked_rule_code` holds `normalize_rule_code` forms of every `js` a rule
    tool returned with status "suggested" in this session. An unclosed block
    is closed first so the note renders outside the code.
    """
    lines = text.split("\n")
    output: list[str] = []
    cursor = 0
    for block in _iter_fenced_blocks(lines):
        block_end = block.end_line + 1 if block.closed else len(lines)
        output.extend(lines[cursor:block_end])
        cursor = block_end
        if not _is_unchecked_rule_code(block, checked_rule_code):
            continue
        if not block.closed:
            output.append(f"{block.quote_prefix}{block.indent}{block.fence}")
        output.extend(["", UNCHECKED_RULE_CODE_NOTE])
        if cursor < len(lines) and lines[cursor].strip():
            output.append("")
    output.extend(lines[cursor:])
    return "\n".join(output)


def _is_unchecked_rule_code(block: _FencedBlock, checked_rule_code: set[str]) -> bool:
    if not _is_rule_code(block.language, block.code):
        return False
    candidates = {block.code, _without_fence_lines(block.code)}
    return not any(normalize_rule_code(code) in checked_rule_code for code in candidates)


def _without_fence_lines(code: str) -> str:
    """Code with fence lines removed, for a fence wrapped around another (```md around ```js)."""
    return "\n".join(
        line for line in code.split("\n")
        if not _OPENING_FENCE_RE.match(_strip_quote_prefix(line)[1])
    )


def _strip_quote_prefix(line: str) -> tuple[str, str]:
    prefix = _QUOTE_PREFIX_RE.match(line).group(0)
    return prefix, line[len(prefix):]


def _iter_fenced_blocks(lines: list[str]):
    index = 0
    while index < len(lines):
        quote_prefix, content = _strip_quote_prefix(lines[index])
        opening = _OPENING_FENCE_RE.match(content)
        if opening is None or not _is_valid_info(opening.group("fence"), opening.group("info")):
            index += 1
            continue
        fence = opening.group("fence")
        closing_re = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        end = index + 1
        while end < len(lines) and not closing_re.match(_strip_quote_prefix(lines[end])[1]):
            end += 1
        closed = end < len(lines)
        info = opening.group("info").strip()
        yield _FencedBlock(
            start_line=index,
            end_line=end if closed else len(lines),
            language=_LANGUAGE_RE.match(info).group(0).lower(),
            code="\n".join(_strip_quote_prefix(line)[1] for line in lines[index + 1:end]),
            closed=closed,
            fence=fence,
            indent=opening.group("indent"),
            quote_prefix=quote_prefix,
        )
        index = end + 1


def _is_valid_info(fence: str, info: str) -> bool:
    """A backtick fence's info string may not contain a backtick (CommonMark)."""
    return not (fence.startswith("`") and "`" in info)


def _is_rule_code(language: str, code: str) -> bool:
    if language in _RULE_LANGUAGES:
        return True
    if language in _PLAIN_LANGUAGES:
        return any(marker in code for marker in _RULE_MARKERS)
    return _FULL_RULE_RE.search(code) is not None
