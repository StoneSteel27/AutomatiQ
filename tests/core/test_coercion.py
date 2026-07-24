"""Tests for silent coercion pipeline."""

from automatiq.core.coercion import (
    coerce_tool_args,
    normalize_enum_case,
    strip_markdown_fences,
    trim_whitespace,
    unwrap_double_json,
)


class TestStripMarkdownFences:
    def test_python_fence(self):
        code = "```python\nimport os\nprint('hello')\n```"
        result, changed = strip_markdown_fences(code)
        assert changed is True
        assert "import os" in result
        assert "print('hello')" in result
        assert "```" not in result

    def test_bash_fence(self):
        code = "```bash\nls -la\n```"
        result, changed = strip_markdown_fences(code)
        assert changed is True
        assert "ls -la" in result
        assert "```" not in result

    def test_no_fence(self):
        code = "import os\nprint('hello')"
        result, changed = strip_markdown_fences(code)
        assert changed is False
        assert result == code

    def test_non_string(self):
        result, changed = strip_markdown_fences(123)
        assert changed is False
        assert result == 123


class TestNormalizeEnumCase:
    def test_exact_match(self):
        result, changed = normalize_enum_case("reading", {"reading", "testing", "building"})
        assert changed is False
        assert result == "reading"

    def test_case_insensitive(self):
        result, changed = normalize_enum_case("Reading", {"reading", "testing", "building"})
        assert changed is True
        assert result == "reading"

    def test_uppercase(self):
        result, changed = normalize_enum_case("TESTING", {"reading", "testing", "building"})
        assert changed is True
        assert result == "testing"

    def test_with_whitespace(self):
        result, changed = normalize_enum_case("  building  ", {"reading", "testing", "building"})
        assert changed is True
        assert result == "building"

    def test_invalid_value(self):
        result, changed = normalize_enum_case("invalid", {"reading", "testing", "building"})
        assert changed is False
        assert result == "invalid"

    def test_non_string(self):
        result, changed = normalize_enum_case(123, {"reading", "testing", "building"})
        assert changed is False
        assert result == 123


class TestTrimWhitespace:
    def test_leading_trailing(self):
        result, changed = trim_whitespace("  hello  ")
        assert changed is True
        assert result == "hello"

    def test_no_whitespace(self):
        result, changed = trim_whitespace("hello")
        assert changed is False
        assert result == "hello"

    def test_empty_string(self):
        result, changed = trim_whitespace("")
        assert changed is False
        assert result == ""

    def test_non_string(self):
        result, changed = trim_whitespace(123)
        assert changed is False
        assert result == 123


class TestUnwrapDoubleJson:
    def test_single_encoded(self):
        value = '{"key": "value"}'
        result, changed = unwrap_double_json(value)
        assert changed is True
        assert result == {"key": "value"}

    def test_double_encoded(self):
        value = '"{\\"key\\": \\"value\\"}"'
        result, changed = unwrap_double_json(value)
        assert changed is True
        assert result == {"key": "value"}

    def test_array(self):
        value = "[1, 2, 3]"
        result, changed = unwrap_double_json(value)
        assert changed is True
        assert result == [1, 2, 3]

    def test_not_json(self):
        value = "hello world"
        result, changed = unwrap_double_json(value)
        assert changed is False
        assert result == "hello world"

    def test_non_string(self):
        result, changed = unwrap_double_json(123)
        assert changed is False
        assert result == 123


class TestCoerceToolArgs:
    def test_execute_ipython_fence_stripping(self):
        args = {
            "ipython_script": "```python\nimport os\n```",
            "description": "Test",
        }
        result, fixes = coerce_tool_args("execute_ipython", args)
        assert len(fixes) > 0
        assert "stripped markdown fence from ipython_script" in fixes
        assert "```" not in result["ipython_script"]
        assert "import os" in result["ipython_script"]

    def test_switch_mode_enum_normalization(self):
        args = {
            "target_mode": "Reading",
            "context": "Test context",
        }
        result, fixes = coerce_tool_args("switch_mode", args)
        assert len(fixes) > 0
        assert any("normalized target_mode case" in fix for fix in fixes)
        assert result["target_mode"] == "reading"

    def test_final_submit_fence_stripping(self):
        args = {
            "final_python_script": "```python\nprint('hello')\n```",
        }
        result, fixes = coerce_tool_args("final_submit", args)
        assert len(fixes) > 0
        assert "stripped markdown fence from final_python_script" in fixes
        assert "```" not in result["final_python_script"]

    def test_identifier_whitespace_trimming(self):
        args = {
            "path": "  /some/path  ",
            "ipython_script": "print('hello')",
            "description": "Test",
        }
        result, fixes = coerce_tool_args("execute_ipython", args)
        # path is in IDENTIFIER_FIELDS, so it gets trimmed
        assert len(fixes) == 1
        assert "trimmed whitespace from path" in fixes
        assert result["path"] == "/some/path"

    def test_no_fixes_needed(self):
        args = {
            "ipython_script": "import os",
            "description": "Test",
        }
        result, fixes = coerce_tool_args("execute_ipython", args)
        assert len(fixes) == 0
        assert result == args

    def test_non_dict_input(self):
        result, fixes = coerce_tool_args("execute_ipython", "not a dict")
        assert len(fixes) == 0
        assert result == "not a dict"
