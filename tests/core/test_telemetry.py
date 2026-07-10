"""Unit tests for telemetry helpers and schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from automatiq.core.telemetry import (
    AgentErrorProps,
    AgentHeartbeatProps,
    AgentSessionEndedProps,
    AgentStartedProps,
    FinalScriptSubmittedProps,
    ModeSwitchedProps,
    RecordingEndedProps,
    RecordingStartedProps,
    SystemCrashProps,
    _extract_error_location,
    _sanitize_message,
    make_error_from_exc,
    make_error_props,
)

# ── _sanitize_message ───────────────────────────────────────────────────────


class TestSanitizeMessage:
    def test_strips_windows_paths(self):
        msg = _sanitize_message("Error reading C:\\Users\\kanish\\file.txt")
        assert "C:\\Users" not in msg
        assert "<redacted>" in msg

    def test_strips_unix_paths(self):
        msg = _sanitize_message("Failed to load /home/user/project/data.json")
        assert "/home/user" not in msg
        assert "<redacted>" in msg

    def test_strips_urls(self):
        msg = _sanitize_message("Request to https://api.example.com/v1/data failed")
        assert "https://api.example.com" not in msg
        assert "<redacted>" in msg

    def test_strips_api_keys(self):
        msg = _sanitize_message("Auth failed with key sk-abc123def456ghi789jkl012mno345")
        assert "sk-abc123" not in msg
        assert "<redacted>" in msg

    def test_strips_bearer_tokens(self):
        msg = _sanitize_message("Header: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature")
        assert "Bearer eyJ" not in msg
        assert "<redacted>" in msg

    def test_strips_token_query_params(self):
        msg = _sanitize_message("GET /api?token=secret123abc&user=admin")
        assert "token=secret123" not in msg
        assert "<redacted>" in msg

    def test_truncates_long_messages(self):
        long_msg = "A" * 300
        result = _sanitize_message(long_msg, max_len=200)
        assert len(result) == 203  # 200 chars + "..."
        assert result.endswith("...")

    def test_preserves_short_clean_messages(self):
        msg = _sanitize_message("Rate limit exceeded for model gpt-4o")
        assert msg == "Rate limit exceeded for model gpt-4o"

    def test_multiple_patterns_in_one_message(self):
        msg = _sanitize_message("Failed at C:\\Users\\me\\code.py calling https://api.openai.com with sk-secret123abc")
        assert "C:\\Users" not in msg
        assert "https://api.openai.com" not in msg
        assert "sk-secret" not in msg

    def test_empty_string(self):
        assert _sanitize_message("") == ""


# ── _extract_error_location ─────────────────────────────────────────────────


class TestExtractErrorLocation:
    def test_finds_automatiq_frame(self):
        def _raise_inner():
            raise ValueError("inner error")

        def _raise_outer():
            _raise_inner()

        try:
            _raise_outer()
        except ValueError as exc:
            line, file = _extract_error_location(exc)

        assert isinstance(line, int)
        assert line > 0
        assert file == "test_telemetry.py"

    def test_falls_back_to_last_frame_when_no_automatiq(self):
        # Create an exception with a synthetic traceback that has no automatiq frame
        try:
            # This code runs in the test file itself, so it WILL have an automatiq
            # frame if the test file is under automatiq/. But test files are under
            # tests/, so we need to simulate. Instead, test the fallback with
            # a real exception that has frames only outside automatiq.
            raise RuntimeError("test error")
        except RuntimeError as exc:
            line, file = _extract_error_location(exc)

        # This test file is under tests/ not automatiq/, so the frame
        # won't match /automatiq/ — but it still returns the last frame
        assert isinstance(line, int)
        assert isinstance(file, str)

    def test_no_traceback_returns_unknown(self):
        exc = ValueError("no traceback")
        line, file = _extract_error_location(exc)
        assert line == 0
        assert file == "unknown"


# ── make_error_props ────────────────────────────────────────────────────────


class TestMakeErrorProps:
    def test_builds_correct_props(self):
        props = make_error_props(
            exception_class="RateLimitError",
            message="Rate limit exceeded",
            line=487,
            file="main.py",
            phase="llm_call",
            step=5,
            cell=2,
        )
        assert props.exception_class == "RateLimitError"
        assert props.message == "Rate limit exceeded"
        assert props.line == 487
        assert props.file == "main.py"
        assert props.phase == "llm_call"
        assert props.step == 5
        assert props.cell == 2

    def test_sanitizes_message(self):
        props = make_error_props(
            exception_class="FileNotFoundError",
            message="File C:\\Users\\secret\\data.json not found",
            line=100,
            file="main.py",
            phase="sandbox_execution",
            step=1,
            cell=1,
        )
        assert "C:\\Users" not in props.message
        assert "<redacted>" in props.message


# ── make_error_from_exc ─────────────────────────────────────────────────────


class TestMakeErrorFromExc:
    def test_extracts_exception_class_and_message(self):
        exc = KeyError("result_queue")
        props = make_error_from_exc(exc, phase="sandbox_execution", step=15, cell=8)
        assert props.exception_class == "KeyError"
        assert "result_queue" in props.message
        assert props.phase == "sandbox_execution"
        assert props.step == 15
        assert props.cell == 8

    def test_extracts_line_and_file(self):
        try:
            raise ValueError("test")
        except ValueError as exc:
            props = make_error_from_exc(exc, phase="llm_call", step=3, cell=1)

        assert props.line > 0
        assert isinstance(props.file, str)


# ── Schema validation ───────────────────────────────────────────────────────


class TestSchemaValidation:
    def test_agent_error_props(self):
        props = AgentErrorProps(
            exception_class="RateLimitError",
            message="Rate limit exceeded",
            line=487,
            file="main.py",
            phase="llm_call",
            step=3,
            cell=1,
        )
        assert props.exception_class == "RateLimitError"

    def test_agent_started_props_fresh(self):
        props = AgentStartedProps(
            model="gpt-4o",
            session_type="fresh",
            proxy_enabled=False,
            max_steps=100,
        )
        assert props.session_type == "fresh"

    def test_agent_started_props_resume(self):
        props = AgentStartedProps(
            model="gpt-4o",
            session_type="resume",
            proxy_enabled=True,
            max_steps=50,
        )
        assert props.session_type == "resume"

    def test_agent_started_props_invalid_session_type(self):
        with pytest.raises(ValidationError):
            AgentStartedProps(
                model="gpt-4o",
                session_type="invalid",
                proxy_enabled=False,
                max_steps=100,
            )

    def test_recording_started_props(self):
        props = RecordingStartedProps(
            browser="brave",
            proxy_enabled=True,
            blocklist_enabled=True,
        )
        assert props.browser == "brave"

    def test_agent_heartbeat_props(self):
        props = AgentHeartbeatProps(
            step=15,
            cell=8,
            duration_seconds=300.0,
            current_mode="building",
            current_phase="llm_call",
            final_scripts_submitted=1,
            guardrails={"duplicate_thought": 2, "repeated_execution": 1},
            errors=[],
        )
        assert props.step == 15
        assert props.current_mode == "building"
        assert props.final_scripts_submitted == 1

    def test_agent_heartbeat_props_with_errors(self):
        error = AgentErrorProps(
            exception_class="RateLimitError",
            message="Rate limit",
            line=487,
            file="main.py",
            phase="llm_call",
            step=3,
            cell=1,
        )
        props = AgentHeartbeatProps(
            step=15,
            cell=8,
            duration_seconds=300.0,
            current_mode="reading",
            current_phase="llm_call",
            final_scripts_submitted=0,
            guardrails={},
            errors=[error],
        )
        assert len(props.errors) == 1
        assert props.errors[0].exception_class == "RateLimitError"

    def test_mode_switched_props(self):
        props = ModeSwitchedProps(from_mode="reading", to_mode="testing", step=5, cell=3)
        assert props.from_mode == "reading"
        assert props.to_mode == "testing"

    def test_final_script_submitted_props(self):
        props = FinalScriptSubmittedProps(step=28, cell=14, duration_seconds=720.0, total_tokens=5000)
        assert props.step == 28
        assert props.total_tokens == 5000

    def test_agent_session_ended_props_success(self):
        props = AgentSessionEndedProps(
            outcome="success",
            model="gpt-4o",
            total_tokens=5000,
            prompt_tokens=3000,
            completion_tokens=2000,
            steps_taken=28,
            cells_executed=14,
            duration_seconds=720.0,
            proxy_enabled=False,
        )
        assert props.outcome == "success"
        assert props.errors == []
        assert props.crash_step is None
        assert props.crash_cell is None
        assert props.crash_phase is None

    def test_agent_session_ended_props_crash_with_context(self):
        error = AgentErrorProps(
            exception_class="KeyError",
            message="'result_queue'",
            line=199,
            file="sandbox.py",
            phase="sandbox_execution",
            step=15,
            cell=8,
        )
        props = AgentSessionEndedProps(
            outcome="crash",
            model="gpt-4o",
            total_tokens=5000,
            prompt_tokens=3000,
            completion_tokens=2000,
            steps_taken=15,
            cells_executed=8,
            duration_seconds=360.0,
            proxy_enabled=False,
            guardrails={"duplicate_thought": 2, "step_limit": 1},
            errors=[error],
            crash_step=15,
            crash_cell=8,
            crash_phase="sandbox_execution",
        )
        assert props.outcome == "crash"
        assert len(props.errors) == 1
        assert props.crash_step == 15
        assert props.crash_phase == "sandbox_execution"

    def test_agent_session_ended_props_step_limit_reached(self):
        props = AgentSessionEndedProps(
            outcome="step_limit_reached",
            model="gpt-4o",
            total_tokens=10000,
            prompt_tokens=6000,
            completion_tokens=4000,
            steps_taken=100,
            cells_executed=20,
            duration_seconds=1800.0,
            proxy_enabled=False,
            guardrails={"step_limit": 1},
        )
        assert props.outcome == "step_limit_reached"
        assert props.guardrails["step_limit"] == 1

    def test_agent_session_ended_props_default_guardrails(self):
        props = AgentSessionEndedProps(
            outcome="abandoned_by_user",
            model="gpt-4o",
            total_tokens=0,
            prompt_tokens=0,
            completion_tokens=0,
            steps_taken=0,
            cells_executed=0,
            duration_seconds=0.0,
            proxy_enabled=False,
        )
        assert props.guardrails["duplicate_thought"] == 0
        assert props.guardrails["repeated_execution"] == 0
        assert props.guardrails["final_script_bounce"] == 0
        assert props.guardrails["step_limit"] == 0
        assert props.guardrails["validation_bailout"] == 0

    def test_recording_ended_props_with_crash_reason(self):
        props = RecordingEndedProps(
            duration_seconds=120.0,
            total_http_requests=50,
            total_ws_connections=3,
            total_ws_frames=100,
            browser_used="brave",
            proxy_enabled=False,
            has_ai_analysis=True,
            crash_reason="browser_crash",
        )
        assert props.crash_reason == "browser_crash"

    def test_recording_ended_props_no_crash(self):
        props = RecordingEndedProps(
            duration_seconds=120.0,
            total_http_requests=50,
            total_ws_connections=3,
            total_ws_frames=100,
            browser_used="brave",
            proxy_enabled=False,
            has_ai_analysis=True,
            crash_reason=None,
        )
        assert props.crash_reason is None

    def test_system_crash_props_unhandled_exception(self):
        props = SystemCrashProps(
            crash_type="unhandled_exception",
            exception_class="KeyError",
            message="'result_queue'",
            line=199,
            file="sandbox.py",
            module="automatiq.core.ipython_sandbox.sandbox",
            active_command="run",
            step=15,
            cell=8,
            phase="sandbox_execution",
        )
        assert props.crash_type == "unhandled_exception"
        assert props.exception_class == "KeyError"
        assert props.line == 199

    def test_system_crash_props_force_quit(self):
        props = SystemCrashProps(
            crash_type="force_quit",
            active_command="record",
        )
        assert props.crash_type == "force_quit"
        assert props.exception_class is None
        assert props.step is None

    def test_system_crash_props_invalid_type(self):
        with pytest.raises(ValidationError):
            SystemCrashProps(
                crash_type="invalid",
                active_command="run",
            )
