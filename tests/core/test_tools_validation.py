"""Tests for improved tool validation error messages."""

from automatiq.core.tools import validate_tool_args


class TestValidateToolArgs:
    def test_execute_ipython_syntax_error_with_line_number(self):
        args = {
            "ipython_script": "import os\nprint('hello'}\n",  # Wrong closing bracket
            "description": "Test",
        }
        error = validate_tool_args("execute_ipython", args)
        assert error is not None
        assert "ipython_script:" in error
        # Should be a syntax error (invalid), not incomplete
        assert "Syntax error" in error or "Incomplete" in error

    def test_execute_ipython_syntax_error_with_preview(self):
        args = {
            "ipython_script": "import os\nfor i in range(10):\n    print(i}\n",  # Wrong bracket
            "description": "Test",
        }
        error = validate_tool_args("execute_ipython", args)
        assert error is not None
        assert "Script preview:" in error
        assert ">>>" in error  # Error line marker

    def test_execute_ipython_missing_script(self):
        args = {"description": "Test"}
        error = validate_tool_args("execute_ipython", args)
        assert error is not None
        assert "ipython_script" in error

    def test_execute_ipython_empty_script(self):
        args = {
            "ipython_script": "",
            "description": "Test",
        }
        error = validate_tool_args("execute_ipython", args)
        assert error is not None
        assert "ipython_script:" in error
        assert "empty" in error.lower()

    def test_switch_mode_invalid_enum(self):
        args = {
            "target_mode": "invalid_mode",
            "context": "Test",
        }
        error = validate_tool_args("switch_mode", args)
        assert error is not None
        assert "target_mode:" in error
        assert "invalid_mode" in error
        assert "reading" in error
        assert "testing" in error
        assert "building" in error

    def test_switch_mode_missing_context(self):
        args = {"target_mode": "reading"}
        error = validate_tool_args("switch_mode", args)
        assert error is not None
        assert "context" in error

    def test_final_submit_missing_script(self):
        args = {}
        error = validate_tool_args("final_submit", args)
        assert error is not None
        assert "final_python_script" in error

    def test_unknown_tool(self):
        args = {}
        error = validate_tool_args("unknown_tool", args)
        assert error is not None
        assert "Unknown tool name" in error

    def test_valid_execute_ipython(self):
        args = {
            "ipython_script": "import os\nprint('hello')",
            "description": "Test",
        }
        error = validate_tool_args("execute_ipython", args)
        assert error is None

    def test_valid_switch_mode(self):
        args = {
            "target_mode": "reading",
            "context": "Test context",
        }
        error = validate_tool_args("switch_mode", args)
        assert error is None

    def test_valid_final_submit(self):
        args = {"final_python_script": "print('hello')"}
        error = validate_tool_args("final_submit", args)
        assert error is None
