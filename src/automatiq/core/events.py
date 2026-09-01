from blinker import Namespace

recorder_signals = Namespace()

# Logging Events (the recorder's session-bus: file tier + status ring feed
# off these; see mcp/server.py::_wire_event_logging and
# mcp/status_log.py::connect_status_log)
log_info = recorder_signals.signal("log_info")
log_debug = recorder_signals.signal("log_debug")
log_warn = recorder_signals.signal("log_warn")
log_error = recorder_signals.signal("log_error")
log_traceback = recorder_signals.signal("log_traceback")
