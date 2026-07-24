"""Tests for middle-out truncation."""

from automatiq.core.truncation import truncate_middle


class TestTruncateMiddle:
    def test_short_text_no_truncation(self):
        text = "hello world"
        result = truncate_middle(text, max_bytes=100)
        assert result == text

    def test_long_text_truncated(self):
        text = "a" * 1000
        result = truncate_middle(text, max_bytes=100)
        assert len(result.encode("utf-8")) <= 100
        assert "truncated" in result
        assert result.startswith("a")
        assert result.endswith("a")

    def test_utf8_boundaries(self):
        # Emoji is 4 bytes each
        text = "😀" * 100  # 400 bytes
        result = truncate_middle(text, max_bytes=100)
        # Should not split emoji
        assert "😀" in result
        assert "truncated" in result

    def test_empty_string(self):
        result = truncate_middle("", max_bytes=100)
        assert result == ""

    def test_exact_limit(self):
        text = "a" * 100
        result = truncate_middle(text, max_bytes=100)
        assert result == text

    def test_just_over_limit(self):
        text = "a" * 101
        result = truncate_middle(text, max_bytes=100)
        assert len(result.encode("utf-8")) <= 100
        assert "truncated" in result

    def test_preserves_start_and_end(self):
        text = "START" + "x" * 1000 + "END"
        result = truncate_middle(text, max_bytes=100)
        assert result.startswith("START")
        assert result.endswith("END")
        assert "truncated" in result

    def test_marker_format(self):
        text = "a" * 1000
        result = truncate_middle(text, max_bytes=100)
        assert "chars truncated" in result
