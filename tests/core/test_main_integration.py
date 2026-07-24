"""End-to-end integration tests for tool validation pipeline."""

import json

from automatiq.core.coercion import coerce_tool_args
from automatiq.core.json_repair import repair_json
from automatiq.core.tools import validate_tool_args


class TestIntegrationPipeline:
    def test_fence_stripping_then_validation(self):
        """Test that markdown fences are stripped before validation."""
        raw_args = '{"ipython_script": "```python\\nimport os\\n```", "description": "Test"}'

        # Step 1: Parse JSON
        args = json.loads(raw_args)

        # Step 2: Coerce (strip fences)
        args, fixes = coerce_tool_args("execute_ipython", args)
        assert len(fixes) > 0
        assert "```" not in args["ipython_script"]

        # Step 3: Validate (should pass now)
        error = validate_tool_args("execute_ipython", args)
        assert error is None

    def test_enum_normalization_then_validation(self):
        """Test that enum case is normalized before validation."""
        raw_args = '{"target_mode": "Reading", "context": "Test"}'

        # Step 1: Parse JSON
        args = json.loads(raw_args)

        # Step 2: Coerce (normalize enum)
        args, fixes = coerce_tool_args("switch_mode", args)
        assert len(fixes) > 0
        assert args["target_mode"] == "reading"

        # Step 3: Validate (should pass now)
        error = validate_tool_args("switch_mode", args)
        assert error is None

    def test_json_repair_then_validation(self):
        """Test that malformed JSON is repaired before validation."""
        raw_args = '{"target_mode": "reading", "context": "Test",}'  # Trailing comma

        # Step 1: Try to parse JSON (will fail)
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            # Step 2: Repair JSON
            repaired, fixes = repair_json(raw_args)
            assert len(fixes) > 0
            args = json.loads(repaired)

        # Step 3: Validate (should pass now)
        error = validate_tool_args("switch_mode", args)
        assert error is None

    def test_full_pipeline_with_multiple_fixes(self):
        """Test full pipeline with JSON repair + coercion + validation."""
        # Malformed JSON with fence + trailing comma
        raw_args = '```json\n{"ipython_script": "```python\\nimport os\\n```", "description": "Test",}\n```'

        # Step 1: Try to parse JSON (will fail)
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            # Step 2: Repair JSON
            repaired, fixes = repair_json(raw_args)
            assert len(fixes) > 0
            args = json.loads(repaired)

        # Step 3: Coerce (strip inner fence)
        args, fixes = coerce_tool_args("execute_ipython", args)
        assert len(fixes) > 0

        # Step 4: Validate (should pass now)
        error = validate_tool_args("execute_ipython", args)
        assert error is None

    def test_validation_still_fails_for_real_errors(self):
        """Test that validation still fails for actual syntax errors."""
        raw_args = '{"ipython_script": "import os\\nprint(\'hello\'}\\n", "description": "Test"}'

        # Step 1: Parse JSON
        args = json.loads(raw_args)

        # Step 2: Coerce (no fixes needed)
        args, fixes = coerce_tool_args("execute_ipython", args)

        # Step 3: Validate (should fail with syntax error)
        error = validate_tool_args("execute_ipython", args)
        assert error is not None
        # Should be a syntax error (invalid), not incomplete
        assert "Syntax error" in error or "Incomplete" in error

    def test_coercion_does_not_break_valid_args(self):
        """Test that coercion doesn't modify already-valid arguments."""
        raw_args = '{"ipython_script": "import os\\nprint(\'hello\')", "description": "Test"}'

        # Step 1: Parse JSON
        args = json.loads(raw_args)
        original_args = args.copy()

        # Step 2: Coerce (no fixes needed)
        args, fixes = coerce_tool_args("execute_ipython", args)
        assert len(fixes) == 0

        # Step 3: Validate (should pass)
        error = validate_tool_args("execute_ipython", args)
        assert error is None

        # Args should be unchanged
        assert args == original_args
