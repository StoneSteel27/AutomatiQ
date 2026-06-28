import unittest.mock

from automatiq.core import events


def test_event_definitions():
    """Verify that expected Blinker signals exist and are properly instantiated."""
    expected_signals = [
        "agent_start",
        "agent_thought_chunk",
        "agent_text_chunk",
        "agent_stream_end",
        "log_info",
        "log_error",
        "code_exec_start",
        "operation_cancelled",
        "mode_switch",
    ]

    for sig_name in expected_signals:
        signal = getattr(events, sig_name)
        assert signal is not None, f"Expected signal '{sig_name}' not found."
        assert signal.name == sig_name, f"Signal name mismatch: {signal.name} != {sig_name}"
        # blinker.Signal does not have a strict type check against the base class easily accessible
        # without importing internals, so we verify it acts like a signal.
        assert hasattr(signal, "connect"), f"Signal '{sig_name}' missing .connect()"
        assert hasattr(signal, "send"), f"Signal '{sig_name}' missing .send()"


def test_signal_publish_subscribe():
    """Verify that a handler can subscribe to a signal and capture emitted payloads."""
    mock_handler = unittest.mock.MagicMock()

    # Connect the mock to a specific event
    events.log_info.connect(mock_handler)

    try:
        # Fire the event with a complex payload from the core
        sender = "core"
        payload = {"text": "Integration test payload", "level": "INFO", "code": 200}
        events.log_info.send(sender, **payload)

        # Assert the mock received the event exactly once
        mock_handler.assert_called_once()

        # Extract arguments passed to the mock
        args, kwargs = mock_handler.call_args

        # Assert the sender is correct (blinker passes sender as the first positional argument)
        assert args[0] == sender

        # Assert the payload perfectly matches
        assert kwargs == payload
    finally:
        # Cleanup so we don't leak handlers into other tests
        events.log_info.disconnect(mock_handler)


def test_signal_disconnect():
    """Verify that disconnecting a handler prevents it from receiving further signals."""
    mock_handler = unittest.mock.MagicMock()

    # Connect and then immediately disconnect
    events.mode_switch.connect(mock_handler)
    events.mode_switch.disconnect(mock_handler)

    # Fire the event
    events.mode_switch.send("core", mode="BUILDING")

    # The handler should not have been called
    mock_handler.assert_not_called()
