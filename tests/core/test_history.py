import json

import pytest
import yaml

from automatiq.core.history import (
    _count_cells,
    extract_recording_name,
    find_history_dirs,
    init_history_dir,
    list_resumable_sessions,
    load_session_messages,
    load_session_metadata,
    save_compressed_snapshot,
    save_session_snapshot,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_history_dir(tmp_path, mocker):
    """Patch HISTORY_DIR to a temp directory."""
    mocker.patch("automatiq.core.history.config.HISTORY_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def sample_messages():
    return [
        {"role": "system", "content": "You are AutomatiQ."},
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "I'll run a command.",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "execute_ipython",
                        "arguments": '{"description": "List files", "ipython_script": "!ls"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc",
            "name": "execute_ipython",
            "content": "<terminal_output>\nfile1.txt\nfile2.txt\n</terminal_output>",
        },
        {"role": "assistant", "content": "Done!"},
    ]


@pytest.fixture
def sample_metadata():
    return {
        "model": "gemini/gemini-2.5-flash",
        "llm_calls": 2,
        "cells_executed": 1,
        "prompt_tokens": 500,
        "completion_tokens": 200,
        "total_tokens": 700,
        "session_started": "2026-06-30T14:32:01",
        "current_mode": "reading",
        "cell_counter": 1,
    }


# ── extract_recording_name ──────────────────────────────────────────────────


class TestExtractRecordingName:
    def test_normal_name(self):
        assert extract_recording_name("browse-imdb_20260629_001306") == "browse-imdb"

    def test_name_with_underscores(self):
        assert extract_recording_name("browse_imdb_top_250_20260629_001306") == "browse_imdb_top_250"

    def test_no_timestamp_suffix(self):
        assert extract_recording_name("some-folder") == "some-folder"

    def test_single_digit_month(self):
        assert extract_recording_name("test_20260101_000000") == "test"


# ── init_history_dir ────────────────────────────────────────────────────────


class TestInitHistoryDir:
    def test_creates_folder(self, mock_history_dir):
        result = init_history_dir("test-session")
        assert result.exists()
        assert result.is_dir()
        assert result.parent == mock_history_dir
        assert "test-session_" in result.name

    def test_folder_name_format(self, mock_history_dir):
        result = init_history_dir("mysession")
        # Should be mysession_YYYYMMDD_HHMMSS
        parts = result.name.rsplit("_", 2)
        assert parts[0] == "mysession"
        assert len(parts[1]) == 8  # YYYYMMDD
        assert len(parts[2]) == 6  # HHMMSS


# ── save / load round-trip ──────────────────────────────────────────────────


class TestSaveLoadRoundtrip:
    def test_save_load_new_format(self, mock_history_dir, sample_messages, sample_metadata):
        history_dir = init_history_dir("test-session")
        save_session_snapshot(history_dir, sample_messages, sample_metadata)

        # Verify file exists
        assert (history_dir / "messages_full.yaml").exists()

        # Load messages back
        loaded = load_session_messages(history_dir)
        assert loaded == sample_messages

        # Load metadata back
        loaded_meta = load_session_metadata(history_dir)
        assert loaded_meta == sample_metadata

    def test_save_compressed(self, mock_history_dir, sample_messages):
        history_dir = init_history_dir("test-session")
        save_compressed_snapshot(history_dir, sample_messages)

        # Should be a bare list
        path = history_dir / "messages_compressed.yaml"
        assert path.exists()
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, list)

    def test_load_messages_legacy_list_format(self, mock_history_dir, sample_messages):
        """Legacy sessions store a bare list, not a {metadata, messages} dict."""
        history_dir = init_history_dir("legacy-session")
        path = history_dir / "messages_full.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(sample_messages, f, sort_keys=False, allow_unicode=True)

        loaded = load_session_messages(history_dir)
        assert loaded == sample_messages

    def test_load_metadata_legacy_returns_none(self, mock_history_dir, sample_messages):
        """Legacy sessions have no metadata — should return None."""
        history_dir = init_history_dir("legacy-session")
        path = history_dir / "messages_full.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(sample_messages, f, sort_keys=False, allow_unicode=True)

        assert load_session_metadata(history_dir) is None

    def test_load_metadata_missing_file(self, mock_history_dir):
        """If messages_full.yaml doesn't exist, metadata is None."""
        history_dir = mock_history_dir / "empty-session"
        history_dir.mkdir()
        assert load_session_metadata(history_dir) is None

    def test_load_messages_invalid_format(self, mock_history_dir):
        """Non-list, non-dict YAML should raise ValueError."""
        history_dir = init_history_dir("bad-session")
        path = history_dir / "messages_full.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump("just a string", f)

        with pytest.raises(ValueError, match="Unexpected YAML structure"):
            load_session_messages(history_dir)


# ── _count_cells ────────────────────────────────────────────────────────────


class TestCountCells:
    def test_counts_real_executions(self):
        messages = [
            {"role": "tool", "name": "execute_ipython", "content": "ok"},
            {"role": "tool", "name": "execute_ipython", "content": "ok2"},
        ]
        assert _count_cells(messages) == 2

    def test_skips_validation_errors(self):
        messages = [
            {"role": "tool", "name": "execute_ipython", "content": "SYSTEM: Tool Validation Error"},
            {"role": "tool", "name": "execute_ipython", "content": "ok"},
        ]
        assert _count_cells(messages) == 1

    def test_skips_duplicates(self):
        messages = [
            {
                "role": "tool",
                "name": "execute_ipython",
                "content": "SYSTEM: You have submitted the exact same description",
            },  # noqa: E501
            {"role": "tool", "name": "execute_ipython", "content": "ok"},
        ]
        assert _count_cells(messages) == 1

    def test_ignores_other_tools(self):
        messages = [
            {"role": "tool", "name": "final_submit", "content": "done"},
            {"role": "tool", "name": "execute_ipython", "content": "ok"},
        ]
        assert _count_cells(messages) == 1


# ── list_resumable_sessions ─────────────────────────────────────────────────


class TestListResumableSessions:
    def test_empty_history_dir(self, mock_history_dir):
        assert list_resumable_sessions() == []

    def test_filters_by_cwd_recording(self, mock_history_dir, tmp_path, sample_messages, sample_metadata, mocker):
        # Patch cwd to tmp_path
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        # Create a recording dir in cwd with session_metadata.json
        recording_dir = tmp_path / "test-recording"
        recording_dir.mkdir()
        (recording_dir / "session_metadata.json").write_text(json.dumps({"status": "completed"}))

        # Create a matching history dir
        history_dir = init_history_dir("test-recording")
        save_session_snapshot(history_dir, sample_messages, sample_metadata)

        # Create a history dir WITHOUT a matching recording in cwd
        orphan_dir = init_history_dir("orphan-recording")
        save_session_snapshot(orphan_dir, sample_messages, sample_metadata)

        sessions = list_resumable_sessions()
        assert len(sessions) == 1
        assert sessions[0].recording_name == "test-recording"
        assert sessions[0].history_dir == history_dir

    def test_sorted_newest_first(self, mock_history_dir, tmp_path, sample_messages, mocker):
        import time

        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        # Create two recording dirs
        for name in ["recording-a", "recording-b"]:
            rd = tmp_path / name
            rd.mkdir()
            (rd / "session_metadata.json").write_text(json.dumps({"status": "completed"}))

        # Create history dirs with slight time gap
        hd1 = init_history_dir("recording-a")
        save_session_snapshot(hd1, sample_messages, {})
        time.sleep(1.1)  # ensure different timestamp
        hd2 = init_history_dir("recording-b")
        save_session_snapshot(hd2, sample_messages, {})

        sessions = list_resumable_sessions()
        assert len(sessions) == 2
        # recording-b has a newer timestamp
        assert sessions[0].recording_name == "recording-b"
        assert sessions[1].recording_name == "recording-a"

    def test_counts_are_zero_until_loaded(self, mock_history_dir, tmp_path, sample_messages, sample_metadata, mocker):
        """list_resumable_sessions should NOT load YAML — counts default to 0."""
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        recording_dir = tmp_path / "test-recording"
        recording_dir.mkdir()
        (recording_dir / "session_metadata.json").write_text(json.dumps({"status": "completed"}))

        history_dir = init_history_dir("test-recording")
        save_session_snapshot(history_dir, sample_messages, sample_metadata)

        sessions = list_resumable_sessions()
        assert len(sessions) == 1
        assert sessions[0].messages_count == 0
        assert sessions[0].cell_count == 0

    def test_load_counts(self, mock_history_dir, tmp_path, sample_messages, sample_metadata, mocker):
        """load_counts() should populate messages_count and cell_count from YAML."""
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        recording_dir = tmp_path / "test-recording"
        recording_dir.mkdir()
        (recording_dir / "session_metadata.json").write_text(json.dumps({"status": "completed"}))

        history_dir = init_history_dir("test-recording")
        save_session_snapshot(history_dir, sample_messages, sample_metadata)

        sessions = list_resumable_sessions()
        sessions[0].load_counts()
        # sample_messages has 5 messages and 1 execute_ipython cell
        assert sessions[0].messages_count == 5
        assert sessions[0].cell_count == 1


# ── find_history_dirs ───────────────────────────────────────────────────────


class TestFindHistoryDirs:
    def test_no_name_returns_all(self, mock_history_dir, tmp_path, sample_messages, mocker):
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        for name in ["alpha", "beta"]:
            rd = tmp_path / name
            rd.mkdir()
            (rd / "session_metadata.json").write_text(json.dumps({"status": "completed"}))
            hd = init_history_dir(name)
            save_session_snapshot(hd, sample_messages, {})

        dirs = find_history_dirs(None)
        assert len(dirs) == 2

    def test_name_filter(self, mock_history_dir, tmp_path, sample_messages, mocker):
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        for name in ["alpha-session", "beta-session"]:
            rd = tmp_path / name
            rd.mkdir()
            (rd / "session_metadata.json").write_text(json.dumps({"status": "completed"}))
            hd = init_history_dir(name)
            save_session_snapshot(hd, sample_messages, {})

        dirs = find_history_dirs("alpha")
        assert len(dirs) == 1
        assert "alpha" in dirs[0].name

    def test_no_match(self, mock_history_dir, tmp_path, sample_messages, mocker):
        mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

        rd = tmp_path / "alpha"
        rd.mkdir()
        (rd / "session_metadata.json").write_text(json.dumps({"status": "completed"}))
        hd = init_history_dir("alpha")
        save_session_snapshot(hd, sample_messages, {})

        dirs = find_history_dirs("nonexistent")
        assert dirs == []
