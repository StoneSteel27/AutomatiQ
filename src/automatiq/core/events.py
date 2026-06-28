from blinker import Namespace

agent_signals = Namespace()

# Lifecycle Events
agent_start = agent_signals.signal("agent_start")
agent_done = agent_signals.signal("agent_done")
preload_start = agent_signals.signal("preload_start")
preload_end = agent_signals.signal("preload_end")

# User Interaction Events
prompt_request_start = agent_signals.signal("prompt_request_start")
prompt_request_end = agent_signals.signal("prompt_request_end")

# LLM Network Events
llm_request_start = agent_signals.signal("llm_request_start")
llm_request_end = agent_signals.signal("llm_request_end")

# Tool Execution Events
code_exec_start = agent_signals.signal("code_exec_start")
code_exec_output = agent_signals.signal("code_exec_output")
code_exec_end = agent_signals.signal("code_exec_end")

# Thought & Observation Events
agent_thought = agent_signals.signal("agent_thought")
agent_text = agent_signals.signal("agent_text")
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
log_warn = agent_signals.signal("log_warn")
log_error = agent_signals.signal("log_error")
log_traceback = agent_signals.signal("log_traceback")
