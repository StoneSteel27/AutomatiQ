"""Unit tests for call_llm_streaming — chunk parsing, tool-call delta
accumulation, usage extraction, and error handling."""

from types import SimpleNamespace

import pytest

from automatiq.core.llm import call_llm_streaming


def _make_delta(content=None, reasoning=None, tool_calls=None):
    """Build a mock Delta-like object.

    Mimics litellm's Delta which *deletes* optional attributes when None,
    so we only set attributes that have values — getattr(..., None) handles the rest.
    """
    delta = SimpleNamespace()
    if content is not None:
        delta.content = content
    if reasoning is not None:
        delta.reasoning_content = reasoning
    if tool_calls is not None:
        delta.tool_calls = tool_calls
    return delta


def _make_tool_call_delta(index, id_=None, name=None, arguments=""):
    """Build a mock ChatCompletionDeltaToolCall-like object."""
    tc = SimpleNamespace(index=index)
    if id_ is not None:
        tc.id = id_
    # function is always present on real tool-call deltas
    tc.function = SimpleNamespace()
    if name is not None:
        tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _make_chunk(delta, usage=None, finish_reason=None):
    """Build a mock ModelResponseStream-like chunk."""
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    chunk = SimpleNamespace(choices=[choice])
    if usage is not None:
        chunk.usage = usage
    return chunk


def _make_usage_chunk(usage):
    """Build a usage-only chunk (empty choices)."""
    return SimpleNamespace(choices=[], usage=usage)


def _usage(prompt=100, completion=50):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)


@pytest.fixture
def mock_litellm(mocker):
    """Patch litellm.completion and supports_reasoning for streaming tests."""
    mocker.patch("automatiq.core.llm.litellm.supports_reasoning", return_value=False)
    mocker.patch("automatiq.core.llm.config.API_BASE", "")
    return mocker.patch("automatiq.core.llm.litellm.completion")


def collect(stream):
    """Collect all tuples from the streaming generator into a list."""
    return list(stream)


# ── Content + Reasoning deltas ──────────────────────────────────────────────


def test_content_deltas(mock_litellm):
    """Content fragments are yielded in order."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(content="Hello")),
            _make_chunk(_make_delta(content=" world")),
            _make_chunk(_make_delta(content="!")),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    content_parts = [c[1] for c in chunks if c[1] is not None]
    assert content_parts == ["Hello", " world", "!"]


def test_reasoning_deltas(mock_litellm):
    """Reasoning fragments are yielded in order."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(reasoning="Thinking")),
            _make_chunk(_make_delta(reasoning=" more")),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    reasoning_parts = [c[0] for c in chunks if c[0] is not None]
    assert reasoning_parts == ["Thinking", " more"]


def test_mixed_content_and_reasoning(mock_litellm):
    """Both content and reasoning can appear in the same chunk."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(reasoning="Let me think", content="I say")),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    # First chunk has both
    assert chunks[0][0] == "Let me think"
    assert chunks[0][1] == "I say"


def test_empty_delta(mock_litellm):
    """A chunk with an empty delta (no content/reasoning/tools) yields None for all."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta()),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    # The empty-delta chunk should have None for reasoning, content, tool_calls
    # (usage may or may not be present on it)
    empty_chunk = chunks[0]
    assert empty_chunk[0] is None  # reasoning
    assert empty_chunk[1] is None  # content
    assert empty_chunk[2] is None  # tool_calls


# ── Tool call deltas ────────────────────────────────────────────────────────


def test_tool_call_first_chunk_has_id_and_name(mock_litellm):
    """First tool-call chunk carries index, id, and name."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(0, id_="call_1", name="execute_ipython")])),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    tc_chunks = [c for c in chunks if c[2] is not None]
    assert len(tc_chunks) == 1
    tc = tc_chunks[0][2][0]
    assert tc["index"] == 0
    assert tc["id"] == "call_1"
    assert tc["name"] == "execute_ipython"
    assert tc["arguments"] == ""


def test_tool_call_argument_fragments(mock_litellm):
    """Subsequent chunks carry only argument fragments."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(0, id_="call_1", name="execute_ipython")])),
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(0, arguments='{"ipython')])),
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(0, arguments='_script": "print(1)"}')])),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    tc_chunks = [c for c in chunks if c[2] is not None]
    assert len(tc_chunks) == 3
    # First chunk has id + name
    assert tc_chunks[0][2][0]["id"] == "call_1"
    assert tc_chunks[0][2][0]["name"] == "execute_ipython"
    assert tc_chunks[0][2][0]["arguments"] == ""
    # Subsequent chunks have only arguments
    assert tc_chunks[1][2][0]["id"] is None
    assert tc_chunks[1][2][0]["name"] is None
    assert tc_chunks[1][2][0]["arguments"] == '{"ipython'
    assert tc_chunks[2][2][0]["arguments"] == '_script": "print(1)"}'


def test_multiple_tool_calls(mock_litellm):
    """Multiple tool calls can be interleaved via index."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(0, id_="call_a", name="tool_a")])),
            _make_chunk(_make_delta(tool_calls=[_make_tool_call_delta(1, id_="call_b", name="tool_b")])),
            _make_usage_chunk(_usage()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    tc_chunks = [c for c in chunks if c[2] is not None]
    assert len(tc_chunks) == 2
    assert tc_chunks[0][2][0]["index"] == 0
    assert tc_chunks[0][2][0]["name"] == "tool_a"
    assert tc_chunks[1][2][0]["index"] == 1
    assert tc_chunks[1][2][0]["name"] == "tool_b"


# ── Usage ───────────────────────────────────────────────────────────────────


def test_usage_on_empty_choices_chunk(mock_litellm):
    """Usage is yielded from the trailing chunk with empty choices."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(content="Hello")),
            _make_usage_chunk(_usage(prompt=200, completion=100)),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    usage_chunk = chunks[-1]
    assert usage_chunk[0] is None  # no reasoning
    assert usage_chunk[1] is None  # no content
    assert usage_chunk[2] is None  # no tool_calls
    assert usage_chunk[3] is not None
    assert usage_chunk[3].prompt_tokens == 200
    assert usage_chunk[3].completion_tokens == 100
    assert usage_chunk[3].total_tokens == 300


def test_no_usage_without_include_usage(mock_litellm):
    """If no usage chunk is sent, all usage values are None."""
    mock_litellm.return_value = iter(
        [
            _make_chunk(_make_delta(content="Hello")),
            _make_chunk(_make_delta()),
        ]
    )

    chunks = collect(call_llm_streaming([], []))

    for c in chunks:
        assert c[3] is None


# ── Error handling ──────────────────────────────────────────────────────────


def test_model_error_raises_value_error(mock_litellm):
    """A model-not-found error is converted to ValueError with helpful message."""
    mock_litellm.side_effect = Exception("model not found: bad/model-name")

    with pytest.raises(ValueError, match="Invalid model string|does not exist|not supported"):
        list(call_llm_streaming([], []))


def test_non_model_error_propagates(mock_litellm):
    """Non-model errors propagate unchanged."""
    from litellm.exceptions import RateLimitError

    mock_litellm.side_effect = RateLimitError(message="Rate limited", model="test/model", llm_provider="test")

    with pytest.raises(RateLimitError):
        list(call_llm_streaming([], []))


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_empty_stream(mock_litellm):
    """An empty stream yields nothing."""
    mock_litellm.return_value = iter([])

    chunks = collect(call_llm_streaming([], []))

    assert chunks == []


def test_stream_options_passed(mock_litellm):
    """stream=True and stream_options are passed to litellm.completion."""
    mock_litellm.return_value = iter([])

    list(call_llm_streaming([], []))

    _, kwargs = mock_litellm.call_args
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
