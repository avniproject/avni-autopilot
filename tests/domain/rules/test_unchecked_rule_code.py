"""Unit tests for ``domain.rules.unchecked_rule_code``.

Usage:
    PYTHONPATH=src pytest tests/domain/rules/test_unchecked_rule_code.py
"""

from __future__ import annotations

from domain.rules.unchecked_rule_code import (
    UNCHECKED_RULE_CODE_NOTE,
    mark_unchecked_rule_code,
    normalize_rule_code,
)

_TOOL_JS = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  return new imports.rulesConfig.FormElementStatus(params.formElement.uuid, true, null);
};"""


def _reply(code: str, language: str = "javascript") -> str:
    return f"Here is the rule:\n\n```{language}\n{code}\n```\n\nIt shows the field."


def test_code_returned_by_the_tool_is_left_alone() -> None:
    text = _reply(_TOOL_JS)
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text


def test_tool_code_with_different_whitespace_counts_as_returned() -> None:
    text = _reply(_TOOL_JS.replace("\n  ", "\n    "))
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text


def test_code_no_tool_returned_gets_the_note_under_it() -> None:
    edited = _TOOL_JS.replace("true", "false")
    marked = mark_unchecked_rule_code(_reply(edited), {normalize_rule_code(_TOOL_JS)})
    assert marked == _reply(edited).replace(
        "```\n\nIt shows", f"```\n\n{UNCHECKED_RULE_CODE_NOTE}\n\nIt shows",
    )


def test_each_unchecked_block_is_marked() -> None:
    text = _reply("const a = 1;") + "\n\n" + _reply("const b = 2;", "js")
    assert mark_unchecked_rule_code(text, set()).count(UNCHECKED_RULE_CODE_NOTE) == 2


def test_untagged_block_is_marked_only_when_it_looks_like_a_rule() -> None:
    rule = _reply("const encounter = params.entity;", language="")
    tree = _reply("resources/\n  input/", language="")
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(rule, set())
    assert mark_unchecked_rule_code(tree, set()) == tree


def test_non_javascript_blocks_are_left_alone() -> None:
    text = _reply('{"status": "done"}', language="json")
    assert mark_unchecked_rule_code(text, set()) == text


def test_reply_without_code_is_unchanged() -> None:
    text = "The rule was rejected: off-bundle concept names: ['Weight']."
    assert mark_unchecked_rule_code(text, set()) == text


# ── Fence shapes a CommonMark renderer still shows as code ───────────────────


def test_tilde_fence_is_marked() -> None:
    text = "Rule:\n\n~~~javascript\nconst encounter = params.entity;\n~~~\n\nDone."
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set())


def test_space_before_language_tag_is_marked() -> None:
    text = _reply("const a = 1;", language=" javascript")
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set())


def test_unclosed_fence_is_closed_and_marked_after_the_code() -> None:
    text = "Rule:\n\n```js\nconst encounter = params.entity;"
    assert mark_unchecked_rule_code(text, set()) == (
        f"{text}\n```\n\n{UNCHECKED_RULE_CODE_NOTE}"
    )


def test_unclosed_fence_holding_tool_code_is_left_alone() -> None:
    text = f"Rule:\n\n```javascript\n{_TOOL_JS}"
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text


def test_jsx_and_ts_tags_are_marked() -> None:
    for language in ("jsx", "ts", "tsx", "typescript"):
        text = _reply("const a = 1;", language=language)
        assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set()), language


def test_longer_fence_closes_only_on_an_equal_or_longer_fence() -> None:
    text = "````js\nconst a = 1;\n```\nstill code\n````\n\nAfter."
    marked = mark_unchecked_rule_code(text, set())
    assert marked == text.replace("````\n\nAfter.", f"````\n\n{UNCHECKED_RULE_CODE_NOTE}\n\nAfter.")


def test_indented_fence_inside_a_list_is_marked() -> None:
    text = "1. Use this:\n   ```js\n   const a = 1;\n   ```\n2. Save."
    marked = mark_unchecked_rule_code(text, set())
    assert UNCHECKED_RULE_CODE_NOTE in marked
    assert marked.endswith("2. Save.")


def test_note_stands_out_without_quote_styling() -> None:
    assert "⚠️" in UNCHECKED_RULE_CODE_NOTE
    assert "NOT CHECKED" in UNCHECKED_RULE_CODE_NOTE


# ── Rarer shapes ─────────────────────────────────────────────────────────────


def test_fence_inside_a_quote_is_marked() -> None:
    text = "> Try this:\n>\n> ```js\n> const encounter = params.entity;\n> ```\n\nDone."
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set())


def test_quoted_tool_code_is_left_alone() -> None:
    quoted = "\n".join(f"> {line}" for line in _TOOL_JS.split("\n"))
    text = f"> ```javascript\n{quoted}\n> ```"
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text


def test_unclosed_fence_inside_a_quote_is_closed_inside_the_quote() -> None:
    text = "> ```js\n> const encounter = params.entity;"
    assert mark_unchecked_rule_code(text, set()) == f"{text}\n> ```\n\n{UNCHECKED_RULE_CODE_NOTE}"


def test_language_tag_with_attributes_is_marked() -> None:
    text = _reply("const a = 1;", language="javascript{1,3}")
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set())


def test_rule_code_fenced_as_text_is_marked() -> None:
    for language in ("text", "txt", "plaintext"):
        text = _reply("const encounter = params.entity;", language=language)
        assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set()), language


def test_plain_text_block_without_rule_code_is_left_alone() -> None:
    text = _reply("Open App Designer > Forms > Visit", language="text")
    assert mark_unchecked_rule_code(text, set()) == text


def test_md_fence_around_a_js_fence_is_marked() -> None:
    text = "````md\n```js\nconst encounter = params.entity;\n```\n````\n\nDone."
    assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set())


def test_md_fence_around_tool_code_is_left_alone() -> None:
    text = f"````md\n```javascript\n{_TOOL_JS}\n```\n````"
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text


def test_other_languages_mentioning_rule_names_are_left_alone() -> None:
    for language, code in (
        ("json", '{"rule": "new imports.rulesConfig.RuleCondition({encounter})"}'),
        ("python", 'entity = "params.entity"'),
        ("yaml", "rule: FormElementStatus"),
    ):
        text = _reply(code, language=language)
        assert mark_unchecked_rule_code(text, set()) == text, language


def test_full_rule_in_any_language_is_marked() -> None:
    full_rule = _TOOL_JS.replace("true", "false")
    for language in ("diff", "java", "json", "html", "rule"):
        text = _reply(full_rule, language=language)
        assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set()), language


def test_rule_fragment_in_another_language_is_marked() -> None:
    for code in (
        "({ imports, params }) => {}",
        "statusBuilder.build()",
    ):
        text = _reply(code, language="diff")
        assert UNCHECKED_RULE_CODE_NOTE in mark_unchecked_rule_code(text, set()), code


def test_full_tool_rule_in_another_language_is_left_alone() -> None:
    text = _reply(_TOOL_JS, language="rule")
    assert mark_unchecked_rule_code(text, {normalize_rule_code(_TOOL_JS)}) == text
