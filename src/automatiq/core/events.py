from blinker import Namespace

agent_signals = Namespace()

# Lifecycle Events
agent_done = agent_signals.signal("agent_done")
preload_start = agent_signals.signal("preload_start")

# User Interaction Events
prompt_request_start = agent_signals.signal("prompt_request_start")

# LLM Network Events
llm_request_start = agent_signals.signal("llm_request_start")
llm_request_end = agent_signals.signal("llm_request_end")

# Tool Execution Events
code_exec_start = agent_signals.signal("code_exec_start")
code_exec_output = agent_signals.signal("code_exec_output")
code_exec_end = agent_signals.signal("code_exec_end")
restore_progress = agent_signals.signal("restore_progress")

# Thought & Observation Events
agent_thought_chunk = agent_signals.signal("agent_thought_chunk")
agent_text_chunk = agent_signals.signal("agent_text_chunk")
agent_stream_end = agent_signals.signal("agent_stream_end")
tool_message = agent_signals.signal("tool_message")
mode_switch = agent_signals.signal("mode_switch")

# Wait / Retry Events
wait_start = agent_signals.signal("wait_start")
operation_cancelled = agent_signals.signal("operation_cancelled")

# Logging Events
log_info = agent_signals.signal("log_info")
log_debug = agent_signals.signal("log_debug")
log_warn = agent_signals.signal("log_warn")
log_error = agent_signals.signal("log_error")
log_traceback = agent_signals.signal("log_traceback")
