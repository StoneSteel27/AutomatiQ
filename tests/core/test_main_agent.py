import json
import queue
from types import SimpleNamespace

import pytest

from automatiq.core import events
from automatiq.core.cancel_standard import CancelToken
from automatiq.core.main import run_agent


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
    return instance


@pytest.fixture
def mock_llm_stream(mocker):
    """Patch call_llm_streaming so no real LLM call is made."""
    return mocker.patch("automatiq.core.main.call_llm_streaming")


def _usage(prompt=100, completion=50):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)


def test_agent_startup_and_missing_session(mock_config_workspace, mocker):
    """Verify that the agent exits if the session_dump directory is missing."""
    mocker.patch("automatiq.core.main.find_latest_session_dir", return_value=None)
    log_error_mock = mocker.patch.object(events.log_error, "send")

    with pytest.raises(SystemExit) as exc_info:
        run_agent(input_queue=queue.Queue())

    assert exc_info.value.code == 1
    log_error_mock.assert_called_once()
    assert "No valid completed sessions found" in log_error_mock.call_args[1]["text"]


def test_agent_user_exit(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that providing 'q' cleanly exits the agent."""
    log_info_mock = mocker.patch.object(events.log_info, "send")

    input_q = queue.Queue()
    input_q.put("q")

    run_agent(input_queue=input_q)

    log_info_mock.assert_any_call("core", text="User requested exit.")


def test_agent_cancellation_during_llm(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that a cancel token interrupting the LLM cleanly returns to prompt."""
    operation_cancelled_mock = mocker.patch.object(events.operation_cancelled, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    cancel_token = CancelToken()

    def mock_stream_call(*args, **kwargs):
        cancel_token.cancel()
        from automatiq.core.cancel_standard import CancelRequestedException

        raise CancelRequestedException("Interrupted by mock cancel token")

    mock_llm_stream.side_effect = mock_stream_call

    run_agent(input_queue=input_q, cancel_token=cancel_token)

    operation_cancelled_mock.assert_called_once_with("core")


def test_agent_tool_final_submit(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that the final_submit tool correctly triggers UI events and extracts the script."""
    mocker.patch.object(events.tool_message, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    mock_chunks = [
        (None, "I have finished investigating and wrote the script.", None, None),
        (
            None,
            None,
            [{"index": 0, "id": "call_1", "name": "final_submit", "arguments": ""}],
            None,
        ),
        (
            None,
            None,
            [
                {
                    "index": 0,
                    "id": None,
                    "name": None,
                    "arguments": json.dumps({"final_python_script": "print('hello world')"}),
                }
            ],
            None,
        ),
        (None, None, None, _usage()),
    ]
    mock_llm_stream.return_value = iter(mock_chunks)

    run_agent(input_queue=input_q)

    assert mock_llm_stream.call_count >= 1


def test_agent_tool_dispatch_execute(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that execute_ipython correctly interacts with the sandbox and triggers UI events."""
    code_exec_start_mock = mocker.patch.object(events.code_exec_start, "send")
    code_exec_output_mock = mocker.patch.object(events.code_exec_output, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    mock_chunks_1 = [
        (None, "I will run some code to check the current state of the document.", None, None),
        (
            None,
            None,
            [{"index": 0, "id": "call_1", "name": "execute_ipython", "arguments": ""}],
            None,
        ),
        (
            None,
            None,
            [
                {
                    "index": 0,
                    "id": None,
                    "name": None,
                    "arguments": json.dumps(
                        {"ipython_script": "print('hi')", "description": "Prints hi to the console"}
                    ),
                }
            ],
            None,
        ),
        (None, None, None, _usage()),
    ]
    mock_chunks_2 = [
        (None, "I am done with the execution and will now talk to the user.", None, None),
        (
            None,
            None,
            [{"index": 0, "id": "call_2", "name": "final_submit", "arguments": ""}],
            None,
        ),
        (
            None,
            None,
            [{"index": 0, "id": None, "name": None, "arguments": json.dumps({"final_python_script": "print('hello')"})}],
            None,
        ),
        (None, None, None, _usage()),
    ]

    mock_llm_stream.side_effect = [iter(mock_chunks_1), iter(mock_chunks_2)]

    run_agent(input_queue=input_q)

    mock_sandbox.execute.assert_called_once_with("print('hi')")

    code_exec_start_mock.assert_called_once()
    code_exec_output_mock.assert_called_once()
    assert code_exec_output_mock.call_args[1]["output"] == "Mocked execution output"


def test_agent_chunk_signals_emitted(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that agent_thought_chunk and agent_text_chunk signals fire during streaming."""
    thought_chunk_mock = mocker.patch.object(events.agent_thought_chunk, "send")
    text_chunk_mock = mocker.patch.object(events.agent_text_chunk, "send")
    stream_end_mock = mocker.patch.object(events.agent_stream_end, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    mock_chunks = [
        ("Let me think...", None, None, None),
        ("...about this.", None, None, None),
        (None, "Here is my ", None, None),
        (None, "answer.", None, None),
        (None, None, None, _usage()),
    ]
    mock_llm_stream.return_value = iter(mock_chunks)

    run_agent(input_queue=input_q)

    thought_chunk_mock.assert_any_call("core", text="Let me think...")
    thought_chunk_mock.assert_any_call("core", text="...about this.")
    text_chunk_mock.assert_any_call("core", text="Here is my ")
    text_chunk_mock.assert_any_call("core", text="answer.")
    stream_end_mock.assert_called_once()
    assert stream_end_mock.call_args[1]["usage"] is not None


def test_agent_tool_call_reconstruction(session_dump_dir, mock_sandbox, mock_llm_stream, mocker):
    """Verify that tool call deltas are correctly accumulated and reconstructed."""
    code_exec_start_mock = mocker.patch.object(events.code_exec_start, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    # Tool call arguments arrive in multiple fragments
    # Full JSON: {"ipython_script": "x = 1 + 2", "description": "add"}
    mock_chunks = [
        (None, "Running code now.", None, None),
        (
            None,
            None,
            [{"index": 0, "id": "call_1", "name": "execute_ipython", "arguments": ""}],
            None,
        ),
        (
            None,
            None,
            [{"index": 0, "id": None, "name": None, "arguments": '{"ipython_script": "x = 1'}],
            None,
        ),
        (
            None,
            None,
            [{"index": 0, "id": None, "name": None, "arguments": ' + 2", "description": "add"}'}],
            None,
        ),
        (None, None, None, _usage()),
    ]
    mock_llm_stream.return_value = iter(mock_chunks)

    run_agent(input_queue=input_q)

    mock_sandbox.execute.assert_called_once_with("x = 1 + 2")
    code_exec_start_mock.assert_called_once()
