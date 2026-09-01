"""CancelToken standard — thread-safe cancellation flag for long-running tasks.

This module provides a single, unified cancellation primitive used by
workspace compilation. Cancellation is driven by the stop_recording MCP tool
(via stop tokens) and by on_skip_requested/cancel-token flows - never by
keyboard input.
"""

import threading


class StopToken:
    """A thread-safe stop flag passed to completely abort operations (e.g. via Ctrl+C)."""

    def __init__(self):
        self._event = threading.Event()

    def stop(self):
        """Set the stop flag."""
        self._event.set()

    def reset(self):
        """Clear the stop flag."""
        self._event.clear()

    def is_stopped(self) -> bool:
        """Check if stop was requested."""
        return self._event.is_set()


class StopRequestedException(Exception):
    """Raised when an operation is completely aborted via StopToken."""

    pass


class CancelToken:
    """A thread-safe cancellation flag passed to long-running tasks."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        """Set the cancel flag."""
        self._event.set()

    def reset(self):
        """Clear the cancel flag."""
        self._event.clear()

    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._event.is_set()


class CancelRequestedException(Exception):
    """Raised when an operation is aborted via CancelToken."""

    pass


def run_cancellable(token: CancelToken, func, *args, stop_token: StopToken = None, **kwargs):
    """Run a function in a background thread, raising if cancelled or stopped.

    Raises :class:`CancelRequestedException` if *token* is cancelled (and resets it
    before raising), or :class:`StopRequestedException` if *stop_token* is stopped.
    """
    if token and token.is_cancelled():
        token.reset()
        raise CancelRequestedException()

    result = []
    error = []
    done = threading.Event()

    def _worker():
        try:
            result.append(func(*args, **kwargs))
        except Exception as e:
            error.append(e)
        finally:
            done.set()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while not done.wait(timeout=0.15):
        if token and token.is_cancelled():
            token.reset()
            raise CancelRequestedException("Cancelled via token")
        if stop_token and stop_token.is_stopped():
            raise StopRequestedException("Aborted via stop token")

    if error:
        raise error[0]
    return result[0]
