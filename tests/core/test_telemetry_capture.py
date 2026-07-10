"""Integration tests for telemetry event capture during agent loop execution.

These tests run the real ``run_agent`` loop with mocked LLM streams and sandbox,
intercepting the telemetry client's ``track`` method to verify that each event
is fired with the correct properties — phases, guardrails, errors, outcomes,
heartbeats, and usage milestones.
"""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automatiq.core import events
from automatiq.core.cancel_standard import CancelToken
from automatiq.core.main import run_agent

# ── Shared fixtures (mirror test_main_agent.py patterns) ────────────────────


@pytest.fixture
def mock_config_workspace(tmp_path, mocker):
    mocker.patch("automatiq.core.main.config.WORKSPACE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def session_dump_dir(mock_config_workspace, mocker):
    root_dir = mock_config_workspace / "mock_session"
    root_dir.mkdir()
    (root_dir / "session_metadata.json").write_text(json.dumps({"status": "completed"}))
    workspace_dir = root_dir / "workspace"
    workspace_dir.mkdir()
    mocker.patch("automatiq.core.main.find_latest_session_dir", return_value=root_dir)
    return root_dir


@pytest.fixture
def mock_sandbox(mocker):
    sandbox_cls = mocker.patch("automatiq.core.main.AgentSandbox")
    instance = sandbox_cls.return_value
    instance.execute.return_value = "Mocked execution output"
    instance.cancel_result = None
    instance._cancel_result = None
    instance.output_cache = {}
    instance.history = []
    instance.cell_counter = 0
    instance.last_error_info = None
    return instance


@pytest.fixture
def mock_llm_stream(mocker):
    return mocker.patch("automatiq.core.main.call_llm_streaming")


@pytest.fixture
def telemetry_spy(mocker):
    """Patch the telemetry client's ``track`` method to capture all events.

    Returns a list of ``(event_name, properties_dict)`` tuples.
    """
    captured: list[tuple[str, dict]] = []

    def _track(event, properties):
        captured.append((event, properties))

    mocker.patch("automatiq.core.telemetry.client._enabled", True)
    mocker.patch("automatiq.core.telemetry.client.track", side_effect=_track)
    return captured


def _usage(prompt=100, completion=50):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)


def _make_stream(chunks):
    """Convert a list of chunk tuples into an iterator for mock_llm_stream."""
    return iter(chunks)


# ── agent_started ───────────────────────────────────────────────────────────


class TestAgentStarted:
    def test_fires_agent_started_fresh(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy):
        mock_llm_stream.return_value = _make_stream([(None, "Hi.", None, _usage())])
        input_q = queue.Queue()
        input_q.put("q")
        run_agent(input_queue=input_q, cancel_token=CancelToken())

        started_events = [(e, p) for e, p in telemetry_spy if e == "agent_started"]
        assert len(started_events) == 1
        props = started_events[0][1]
        assert props["session_type"] == "fresh"
        assert "model" in props
        assert "max_steps" in props
        assert "proxy_enabled" in props

    def test_fires_agent_started_resume(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, tmp_path, mocker
    ):
        import yaml

        from automatiq.core.history import _SessionDumper

        history_dir = tmp_path / "test-session_20260630_120000"
        history_dir.mkdir()
        mocker.patch("automatiq.core.history.config.HISTORY_DIR", tmp_path)
        payload = {"metadata": {"current_mode": "reading", "cell_counter": 0}, "messages": []}
        with open(history_dir / "messages_full.yaml", "w", encoding="utf-8") as f:
            yaml.dump(payload, f, Dumper=_SessionDumper, sort_keys=False)

        mock_llm_stream.return_value = _make_stream([(None, "Resuming.", None, _usage())])
        input_q = queue.Queue()
        input_q.put("q")
        run_agent(input_queue=input_q, cancel_token=CancelToken(), resume_from=str(history_dir))

        started_events = [(e, p) for e, p in telemetry_spy if e == "agent_started"]
        assert len(started_events) == 1
        assert started_events[0][1]["session_type"] == "resume"


# ── agent_session_ended ─────────────────────────────────────────────────────


class TestAgentSessionEnded:
    def test_fires_on_user_exit(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy):
        input_q = queue.Queue()
        input_q.put("q")
        run_agent(input_queue=input_q)

        ended_events = [(e, p) for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended_events) == 1
        props = ended_events[0][1]
        assert props["outcome"] == "abandoned_by_user"
        assert "model" in props
        assert "total_tokens" in props
        assert "steps_taken" in props
        assert "cells_executed" in props
        assert "duration_seconds" in props
        assert "proxy_enabled" in props
        assert "guardrails" in props
        assert "errors" in props
        assert props["guardrails"]["duplicate_thought"] == 0
        assert props["errors"] == []

    def test_outcome_success_on_final_submit(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # Stream 1: switch to building mode first (final_submit bounces otherwise)
        mock_chunks_1 = [
            (None, "Switching to building.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "switch_mode", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"target_mode": "building", "context": "Ready to submit"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        # Stream 2: final_submit (now in building mode, bounces once then accepted on resubmit)
        # Actually: final_script_bounces starts at 0, incremented to 1 before check.
        # In building mode: 1 < 1 is False → not bounced → accepted.
        mock_chunks_2 = [
            (None, "Submitting.", None, None),
            (None, None, [{"index": 0, "id": "c2", "name": "final_submit", "arguments": ""}], None),
            (
                None,
                None,
                [{"index": 0, "id": None, "name": None, "arguments": json.dumps({"final_python_script": "print(1)"})}],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_3 = [(None, "Bye.", None, _usage())]
        mock_llm_stream.side_effect = [
            _make_stream(mock_chunks_1),
            _make_stream(mock_chunks_2),
            _make_stream(mock_chunks_3),
        ]

        run_agent(input_queue=input_q)

        ended_events = [(e, p) for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended_events) == 1
        assert ended_events[0][1]["outcome"] == "success"

    def test_outcome_crash_on_unhandled_exception(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")

        # TypeError is not caught by the LLM retry loop → hits except Exception
        mock_llm_stream.side_effect = TypeError("unexpected crash")

        run_agent(input_queue=input_q)

        ended_events = [(e, p) for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended_events) == 1
        assert ended_events[0][1]["outcome"] == "crash"
        assert ended_events[0][1]["crash_step"] is not None
        assert ended_events[0][1]["crash_phase"] is not None


# ── Guardrail tracking ──────────────────────────────────────────────────────


class TestGuardrailTracking:
    def test_duplicate_thought_guardrail_counted(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")

        # Turn 1: execute_ipython with description "Test the script"
        # Turn 2: execute_ipython with SAME description "Test the script" → duplicate
        # Turn 3: quit
        mock_chunks_1 = [
            (None, "Running code.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"ipython_script": "print(1)", "description": "Test the script"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [
            (None, "Running again.", None, None),
            (None, None, [{"index": 0, "id": "c2", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"ipython_script": "print(2)", "description": "Test the script"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_3 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [
            _make_stream(mock_chunks_1),
            _make_stream(mock_chunks_2),
            _make_stream(mock_chunks_3),
        ]

        # Need "q" to exit after the duplicate
        input_q.put("q")

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        assert ended[0]["guardrails"]["duplicate_thought"] == 1

    def test_final_script_bounce_guardrail_counted(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # final_submit while in reading mode (not building) → bounce
        mock_chunks = [
            (None, "Submitting.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "final_submit", "arguments": ""}], None),
            (
                None,
                None,
                [{"index": 0, "id": None, "name": None, "arguments": json.dumps({"final_python_script": "print(1)"})}],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Ok.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        assert ended[0]["guardrails"]["final_script_bounce"] == 1

    def test_validation_bailout_guardrail_counted(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # 3 consecutive validation failures (empty ipython_script) → bailout
        bad_chunks = [
            (None, "Trying.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"description": "test", "ipython_script": ""}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_llm_stream.side_effect = [
            _make_stream(bad_chunks),
            _make_stream(bad_chunks),
            _make_stream(bad_chunks),
            _make_stream([(None, "Done.", None, _usage())]),
        ]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        assert ended[0]["guardrails"]["validation_bailout"] == 1

    def test_repeated_execution_guardrail_counted(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")

        # Run the same script 3 times with empty descriptions to avoid
        # duplicate_thought guardrail (which fires on same non-empty desc).
        # check_repeated_execution fires when same display_script ran >= 2 times.
        script = json.dumps({"ipython_script": "print('same')", "description": ""})
        exec_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (None, None, [{"index": 0, "id": None, "name": None, "arguments": script}], None),
            (None, None, None, _usage()),
        ]
        mock_llm_stream.side_effect = [
            _make_stream(exec_chunks),
            _make_stream(exec_chunks),
            _make_stream(exec_chunks),
            _make_stream([(None, "Done.", None, _usage())]),
        ]

        input_q.put("q")
        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        assert ended[0]["guardrails"]["repeated_execution"] == 1


# ── Mode switched ───────────────────────────────────────────────────────────


class TestModeSwitched:
    def test_mode_switched_event_fires(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_chunks = [
            (None, "Switching.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "switch_mode", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"target_mode": "testing", "context": "Investigating"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        switched = [(e, p) for e, p in telemetry_spy if e == "mode_switched"]
        assert len(switched) == 1
        props = switched[0][1]
        assert props["from_mode"] == "reading"
        assert props["to_mode"] == "testing"
        assert "step" in props
        assert "cell" in props


# ── Final script submitted ──────────────────────────────────────────────────


class TestFinalScriptSubmitted:
    def test_fires_on_final_submit(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # Stream 1: switch to building mode (final_submit bounces in reading mode)
        mock_chunks_1 = [
            (None, "Switching to building.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "switch_mode", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"target_mode": "building", "context": "Ready"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        # Stream 2: final_submit in building mode
        mock_chunks_2 = [
            (None, "Submitting.", None, None),
            (None, None, [{"index": 0, "id": "c2", "name": "final_submit", "arguments": ""}], None),
            (
                None,
                None,
                [{"index": 0, "id": None, "name": None, "arguments": json.dumps({"final_python_script": "print(1)"})}],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_3 = [(None, "Bye.", None, _usage())]
        mock_llm_stream.side_effect = [
            _make_stream(mock_chunks_1),
            _make_stream(mock_chunks_2),
            _make_stream(mock_chunks_3),
        ]

        run_agent(input_queue=input_q)

        submitted = [(e, p) for e, p in telemetry_spy if e == "final_script_submitted"]
        assert len(submitted) == 1
        props = submitted[0][1]
        assert "step" in props
        assert "cell" in props
        assert "duration_seconds" in props
        assert "total_tokens" in props


# ── Error capture ───────────────────────────────────────────────────────────


class TestErrorCapture:
    def test_llm_error_captured(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        from litellm.exceptions import RateLimitError

        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # First call raises RateLimitError, second succeeds
        mock_llm_stream.side_effect = [
            RateLimitError("Rate limit exceeded", model="gpt-4o", llm_provider="openai"),
            _make_stream([(None, "Ok.", None, _usage())]),
        ]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        errors = ended[0]["errors"]
        assert len(errors) == 1
        assert errors[0]["exception_class"] == "RateLimitError"
        assert errors[0]["phase"] == "llm_call"
        assert "step" in errors[0]
        assert "cell" in errors[0]
        assert "line" in errors[0]
        assert "file" in errors[0]

    def test_json_parse_error_captured(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [{"index": 0, "id": None, "name": None, "arguments": "NOT VALID JSON{]["}],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        errors = ended[0]["errors"]
        json_errors = [e for e in errors if e["exception_class"] == "JSONDecodeError"]
        assert len(json_errors) == 1
        assert json_errors[0]["phase"] == "tool_execution"

    def test_tool_validation_error_captured(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # Empty ipython_script → validation error
        mock_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"description": "test", "ipython_script": ""}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        errors = ended[0]["errors"]
        validation_errors = [e for e in errors if e["exception_class"] == "ToolValidationError"]
        assert len(validation_errors) == 1
        assert validation_errors[0]["phase"] == "tool_execution"

    def test_sandbox_error_captured(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        mocker.patch.object(events.tool_message, "send")
        mock_sandbox.last_error_info = {
            "exception_class": "SandboxHardTimeout",
            "message": "Cell unresponsive to interrupt after 60s.",
            "line": 199,
            "file": "sandbox.py",
        }
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps(
                            {"ipython_script": "import time; time.sleep(999)", "description": "Long sleep"}
                        ),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        errors = ended[0]["errors"]
        sandbox_errors = [e for e in errors if e["exception_class"] == "SandboxHardTimeout"]
        assert len(sandbox_errors) == 1
        assert sandbox_errors[0]["phase"] == "sandbox_execution"
        assert sandbox_errors[0]["file"] == "sandbox.py"
        assert sandbox_errors[0]["line"] == 199

    def test_system_crash_fires_on_unhandled_exception(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")

        # Make the LLM stream raise an unexpected error (not caught by retry loop)
        mock_llm_stream.side_effect = TypeError("unexpected type error")

        run_agent(input_queue=input_q)

        crash_events = [(e, p) for e, p in telemetry_spy if e == "system_crash"]
        assert len(crash_events) == 1
        props = crash_events[0][1]
        assert props["crash_type"] == "unhandled_exception"
        assert props["exception_class"] == "TypeError"
        assert "message" in props
        assert props["message"] is not None
        assert "line" in props
        assert props["line"] is not None
        assert "file" in props
        assert props["file"] is not None
        assert "step" in props
        assert "cell" in props
        assert "phase" in props

    def test_multiple_errors_accumulate(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        from litellm.exceptions import APIConnectionError

        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        # First call: APIConnectionError
        # Second call: validation error (empty script)
        # Third call: success
        mock_llm_stream.side_effect = [
            APIConnectionError("Connection refused", model="gpt-4o", llm_provider="openai"),
            _make_stream(
                [
                    (None, "Running.", None, None),
                    (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
                    (
                        None,
                        None,
                        [
                            {
                                "index": 0,
                                "id": None,
                                "name": None,
                                "arguments": json.dumps({"description": "t", "ipython_script": ""}),
                            }
                        ],
                        None,
                    ),
                    (None, None, None, _usage()),
                ]
            ),
            _make_stream([(None, "Done.", None, _usage())]),
        ]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        errors = ended[0]["errors"]
        assert len(errors) >= 2
        classes = [e["exception_class"] for e in errors]
        assert "APIConnectionError" in classes
        assert "ToolValidationError" in classes


# ── Heartbeat ───────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_does_not_fire_immediately(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy):
        mock_llm_stream.return_value = _make_stream([(None, "Hi.", None, _usage())])
        input_q = queue.Queue()
        input_q.put("q")
        run_agent(input_queue=input_q, cancel_token=CancelToken())

        heartbeats = [e for e, p in telemetry_spy if e == "agent_heartbeat"]
        assert len(heartbeats) == 0

    def test_heartbeat_fires_after_interval(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        # Patch HEARTBEAT_INTERVAL to 0 so it fires immediately on the first loop iteration
        mocker.patch("automatiq.core.telemetry.HEARTBEAT_INTERVAL", 0)
        # Also need to patch the import in main.py since it reads at function entry
        mocker.patch("automatiq.core.main.HEARTBEAT_INTERVAL", 0, create=True)

        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_llm_stream.return_value = _make_stream([(None, "Hi.", None, _usage())])

        # We need to make the _hb_interval variable in run_agent be 0.
        # Since it's read via `from .telemetry import HEARTBEAT_INTERVAL as _hb_interval`
        # at function entry, we need to patch it before the function runs.
        with patch("automatiq.core.telemetry.HEARTBEAT_INTERVAL", 0):
            run_agent(input_queue=input_q)

        heartbeats = [(e, p) for e, p in telemetry_spy if e == "agent_heartbeat"]
        assert len(heartbeats) >= 1
        props = heartbeats[0][1]
        assert "step" in props
        assert "cell" in props
        assert "duration_seconds" in props
        assert "current_mode" in props
        assert "current_phase" in props
        assert "final_scripts_submitted" in props
        assert "guardrails" in props
        assert "errors" in props


# ── Phase tracking in errors ────────────────────────────────────────────────


class TestPhaseTracking:
    def test_llm_error_has_llm_call_phase(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        from litellm.exceptions import Timeout

        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_llm_stream.side_effect = [
            Timeout("Request timed out", model="gpt-4o", llm_provider="openai"),
            _make_stream([(None, "Ok.", None, _usage())]),
        ]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        errors = ended[0]["errors"]
        assert errors[0]["phase"] == "llm_call"

    def test_validation_error_has_tool_execution_phase(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"description": "t", "ipython_script": ""}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        errors = ended[0]["errors"]
        validation_errors = [e for e in errors if e["exception_class"] == "ToolValidationError"]
        assert validation_errors[0]["phase"] == "tool_execution"

    def test_sandbox_error_has_sandbox_execution_phase(
        self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker
    ):
        mocker.patch.object(events.tool_message, "send")
        mock_sandbox.last_error_info = {
            "exception_class": "SandboxSoftTimeout",
            "message": "Cell exceeded timeout.",
            "line": 189,
            "file": "sandbox.py",
        }
        input_q = queue.Queue()
        input_q.put("")
        input_q.put("q")

        mock_chunks = [
            (None, "Running.", None, None),
            (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
            (
                None,
                None,
                [
                    {
                        "index": 0,
                        "id": None,
                        "name": None,
                        "arguments": json.dumps({"ipython_script": "x=1", "description": "test"}),
                    }
                ],
                None,
            ),
            (None, None, None, _usage()),
        ]
        mock_chunks_2 = [(None, "Done.", None, _usage())]
        mock_llm_stream.side_effect = [_make_stream(mock_chunks), _make_stream(mock_chunks_2)]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        errors = ended[0]["errors"]
        sandbox_errors = [e for e in errors if e["exception_class"] == "SandboxSoftTimeout"]
        assert sandbox_errors[0]["phase"] == "sandbox_execution"


# ── Step limit outcome ──────────────────────────────────────────────────────


class TestStepLimitOutcome:
    def test_step_limit_reached_outcome(self, session_dump_dir, mock_sandbox, mock_llm_stream, telemetry_spy, mocker):
        mocker.patch.object(events.tool_message, "send")
        mocker.patch("automatiq.core.main.config.MAX_AGENT_STEPS", 2)
        input_q = queue.Queue()
        input_q.put("")  # trigger first autonomous turn
        input_q.put("q")  # exit after guardrail fires

        # Each stream returns an execute_ipython tool call with a DIFFERENT
        # description to avoid duplicate_thought guardrail firing first.
        def _exec_stream(desc: str):
            return _make_stream(
                [
                    (None, "Running code.", None, None),
                    (None, None, [{"index": 0, "id": "c1", "name": "execute_ipython", "arguments": ""}], None),
                    (
                        None,
                        None,
                        [
                            {
                                "index": 0,
                                "id": None,
                                "name": None,
                                "arguments": json.dumps({"ipython_script": "x = 1", "description": desc}),
                            }
                        ],
                        None,
                    ),
                    (None, None, None, _usage()),
                ]
            )

        # Need 3 execute_ipython streams: step counter goes 0→1→2, then
        # on the 3rd step check 2>=2 fires the guardrail.
        mock_llm_stream.side_effect = [
            _exec_stream("first step"),
            _exec_stream("second step"),
            _exec_stream("third step"),
            _make_stream([(None, "Done.", None, _usage())]),
        ]

        run_agent(input_queue=input_q)

        ended = [p for e, p in telemetry_spy if e == "agent_session_ended"]
        assert len(ended) == 1
        assert ended[0]["outcome"] == "step_limit_reached"
        assert ended[0]["guardrails"]["step_limit"] >= 1
