import unittest.mock

from automatiq.core import events

# Exhaustive list of every signal that should exist in events.py after the
# streaming refactor.  When signals are added or removed, update this list.
EXPECTED_SIGNALS = [
    # Lifecycle
    "agent_done",
    "preload_start",
    # User Interaction
    "prompt_request_start",
    # LLM Network
    "llm_request_start",
    "llm_request_end",
    # Tool Execution
    "code_exec_start",
    "code_exec_output",
    "code_exec_end",
    "restore_progress",
    # Thought & Observation
    "agent_thought_chunk",
    "agent_text_chunk",
    "agent_stream_end",
    "tool_message",
    "mode_switch",
    # Wait / Retry
    "wait_start",
    "operation_cancelled",
    # Logging
    "log_info",
    "log_debug",
    "log_warn",
    "log_error",
    "log_traceback",
]

# Signals that were removed during the streaming refactor.  Asserting their
# absence prevents accidental re-introduction.
REMOVED_SIGNALS = [
    "agent_start",
    "preload_end",
    "prompt_request_end",
    "agent_thought",
    "agent_text",
]


def test_event_definitions():
    """Verify that every expected Blinker signal exists and is properly instantiated."""
    for sig_name in EXPECTED_SIGNALS:
        signal = getattr(events, sig_name, None)
        assert signal is not None, f"Expected signal '{sig_name}' not found."
        assert signal.name == sig_name, f"Signal name mismatch: {signal.name} != {sig_name}"
        assert hasattr(signal, "connect"), f"Signal '{sig_name}' missing .connect()"
        assert hasattr(signal, "send"), f"Signal '{sig_name}' missing .send()"


def test_removed_signals_are_gone():
    """Verify that dead signals removed during the streaming refactor are absent."""
    for sig_name in REMOVED_SIGNALS:
        assert not hasattr(events, sig_name), f"Signal '{sig_name}' was removed but is still defined in events.py."


def test_no_extra_signals():
    """Verify no unexpected signals are defined in the agent_signals namespace."""
    # Collect all signal names from the namespace via the signals attribute
    actual = {
        name
        for name, sig in vars(events).items()
        if hasattr(sig, "connect") and hasattr(sig, "send") and hasattr(sig, "name")
    }
    expected = set(EXPECTED_SIGNALS)
    extras = actual - expected
    assert not extras, f"Unexpected signals found in events.py: {extras}"


def test_signal_publish_subscribe():
    """Verify that a handler can subscribe to a signal and capture emitted payloads."""
    mock_handler = unittest.mock.MagicMock()

    events.log_info.connect(mock_handler)

    try:
        sender = "core"
        payload = {"text": "Integration test payload", "level": "INFO", "code": 200}
        events.log_info.send(sender, **payload)

        mock_handler.assert_called_once()

        args, kwargs = mock_handler.call_args
        assert args[0] == sender
        assert kwargs == payload
    finally:
        events.log_info.disconnect(mock_handler)


def test_signal_disconnect():
    """Verify that disconnecting a handler prevents it from receiving further signals."""
    mock_handler = unittest.mock.MagicMock()

    events.mode_switch.connect(mock_handler)
    events.mode_switch.disconnect(mock_handler)

    events.mode_switch.send("core", mode="BUILDING")

    mock_handler.assert_not_called()
