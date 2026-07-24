"""Tests for JSON repair pipeline."""

from automatiq.core.json_repair import (
    extract_leading_container,
    fix_single_quotes,
    fix_trailing_commas,
    fix_unescaped_newlines,
    repair_json,
    strip_json_fences,
)


class TestStripJsonFences:
    def test_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        result, changed = strip_json_fences(text)
        assert changed is True
        assert '{"key": "value"}' in result
        assert "```" not in result

    def test_no_fence(self):
        text = '{"key": "value"}'
        result, changed = strip_json_fences(text)
        assert changed is False
        assert result == text

    def test_non_string(self):
        result, changed = strip_json_fences(123)
        assert changed is False
        assert result == 123


class TestFixTrailingCommas:
    def test_object_trailing_comma(self):
        text = '{"a": 1,}'
        result, changed = fix_trailing_commas(text)
        assert changed is True
        assert result == '{"a": 1}'

    def test_array_trailing_comma(self):
        text = "[1, 2, 3,]"
        result, changed = fix_trailing_commas(text)
        assert changed is True
        assert result == "[1, 2, 3]"

    def test_no_trailing_comma(self):
        text = '{"a": 1}'
        result, changed = fix_trailing_commas(text)
        assert changed is False
        assert result == text

    def test_non_string(self):
        result, changed = fix_trailing_commas(123)
        assert changed is False
        assert result == 123


class TestFixSingleQuotes:
    def test_single_quoted_keys(self):
        text = "{'key': 'value'}"
        result, changed = fix_single_quotes(text)
        assert changed is True
        assert '"key"' in result
        assert '"value"' in result

    def test_no_single_quotes(self):
        text = '{"key": "value"}'
        result, changed = fix_single_quotes(text)
        assert changed is False
        assert result == text

    def test_non_string(self):
        result, changed = fix_single_quotes(123)
        assert changed is False
        assert result == 123


class TestFixUnescapedNewlines:
    def test_unescaped_newlines(self):
        text = '{"key": "line1\nline2"}'
        result, changed = fix_unescaped_newlines(text)
        # Should attempt to escape newlines
        assert changed is True
        assert "\\n" in result

    def test_already_valid(self):
        text = '{"key": "value"}'
        result, changed = fix_unescaped_newlines(text)
        assert changed is False
        assert result == text

    def test_non_string(self):
        result, changed = fix_unescaped_newlines(123)
        assert changed is False
        assert result == 123


class TestExtractLeadingContainer:
    def test_trailing_braces(self):
        text = '{"a": 1}}}'
        result, changed = extract_leading_container(text)
        assert changed is True
        assert result == '{"a": 1}'

    def test_trailing_brackets(self):
        text = "[1, 2, 3]]]"
        result, changed = extract_leading_container(text)
        assert changed is True
        assert result == "[1, 2, 3]"

    def test_no_trailing(self):
        text = '{"a": 1}'
        result, changed = extract_leading_container(text)
        assert changed is False
        assert result == text

    def test_not_container(self):
        text = "hello"
        result, changed = extract_leading_container(text)
        assert changed is False
        assert result == text

    def test_non_string(self):
        result, changed = extract_leading_container(123)
        assert changed is False
        assert result == 123


class TestRepairJson:
    def test_fence_repair(self):
        text = '```json\n{"key": "value"}\n```'
        result, fixes = repair_json(text)
        assert len(fixes) > 0
        assert "stripped markdown fence" in fixes
        assert '{"key": "value"}' in result

    def test_trailing_comma_repair(self):
        text = '{"a": 1,}'
        result, fixes = repair_json(text)
        assert len(fixes) > 0
        assert "removed trailing commas" in fixes
        assert '{"a": 1}' in result

    def test_single_quote_repair(self):
        text = "{'key': 'value'}"
        result, fixes = repair_json(text)
        assert len(fixes) > 0
        assert "converted single quotes to double quotes" in fixes
        assert '"key"' in result

    def test_multiple_repairs(self):
        text = "```json\n{'key': 'value',}\n```"
        result, fixes = repair_json(text)
        assert len(fixes) >= 2
        assert "stripped markdown fence" in fixes
        assert "removed trailing commas" in fixes

    def test_no_repair_needed(self):
        text = '{"key": "value"}'
        result, fixes = repair_json(text)
        assert len(fixes) == 0
        assert result == text

    def test_repair_fails(self):
        text = "not json at all"
        result, fixes = repair_json(text)
        # Should return original text with empty fixes
        assert result == text
        assert len(fixes) == 0

    def test_non_string(self):
        result, fixes = repair_json(123)
        assert len(fixes) == 0
        assert result == 123
