"""Regression tests for session naming, collision handling, and stale-output reset.

Pins the recorder-path bugs from the audited six:
- A reused session name must not inherit a previous failed session's
  artifacts (stale OUTPUT_DIR shadowing).
- The AI session-name blacklist must actually be enforced on the model's
  answer, not just requested in the prompt.
- The collision suffix loop must increment past existing ``_01`` folders
  (the refactor branch's decomposition regressed this to a literal ``_01``;
  this test guards the correct main behavior against that merge).
"""

import json
import os
from types import SimpleNamespace

from automatiq.core import config
from automatiq.core.recorder.ai_analyzer import VideoActionAnalyzer
from automatiq.core.recorder.compile.workspace import compile_workspace

from .conftest import make_action_payload


class TestSessionNameCollision:
    def test_collision_suffix_increments_past_existing(self, agent, tmp_path, monkeypatch):
        """Unnamed session colliding with <name> and <name>_01 lands at <name>_02."""

        monkeypatch.chdir(tmp_path)
        # Pre-existing recordings from earlier sessions
        for existing in ("book-tickets", "book-tickets_01"):
            os.makedirs(tmp_path / existing, exist_ok=True)
            (tmp_path / existing / "marker.txt").write_text("do not touch", encoding="utf-8")

        # The recording pipeline's OUTPUT_DIR (pre-rename), as the record flow sets it
        monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "automatiq_recording_xyz")
        monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "automatiq_recording_xyz" / "workspace")

        # AI naming returns a name that already has two collisions
        monkeypatch.setattr(VideoActionAnalyzer, "generate_session_name", lambda self, flow, fb: "book-tickets")

        agent._process_action(make_action_payload("click", text="Submit"))
        import asyncio

        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        _, success = compile_workspace(
            session_name=None,
            temp_data_dir=temp_data_dir,
            full_video_path="nonexistent.mp4",
            video_start_unix=0,
        )

        assert success is True

        # New recording lands at _02, past both existing folders
        final_dir = tmp_path / "book-tickets_02"
        assert os.path.isdir(final_dir)
        assert os.path.isdir(os.path.join(final_dir, "workspace", "session_dump"))

        # Pre-existing recordings are untouched
        assert (tmp_path / "book-tickets" / "marker.txt").read_text(encoding="utf-8") == "do not touch"
        assert (tmp_path / "book-tickets_01" / "marker.txt").read_text(encoding="utf-8") == "do not touch"


class TestStaleOutputReset:
    def test_reset_output_dirs_wipes_stale_session_artifacts(self, tmp_path, monkeypatch):
        """reset_output_dirs removes a previous failed session's leftovers from OUTPUT_DIR."""

        stale_output = tmp_path / "mysession"
        stale_dump = stale_output / "workspace" / "session_dump"
        os.makedirs(stale_dump / "requests" / "999_GET_stale.example.com", exist_ok=True)
        open(os.path.join(stale_dump, "requests", "999_GET_stale.example.com", "transaction.json"), "w").close()
        open(os.path.join(stale_dump, "full_record.mp4"), "w").close()
        with open(stale_output / "session_metadata.json", "w") as f:
            json.dump({"status": "in_progress"}, f)

        monkeypatch.setattr(config, "OUTPUT_DIR", stale_output)
        monkeypatch.setattr(config, "WORKSPACE_DIR", stale_output / "workspace")

        config.reset_output_dirs()

        assert not os.path.exists(stale_dump)
        assert not os.path.exists(stale_output / "session_metadata.json")
        assert os.path.isdir(stale_output)  # recreated empty, ready for the new recording

    def test_reused_name_compiles_into_clean_workspace(self, agent, tmp_path, monkeypatch):
        """End to end: a named session reusing a dirty folder compiles without stale files."""

        monkeypatch.chdir(tmp_path)
        output_dir = tmp_path / "mysession"
        stale_requests = output_dir / "workspace" / "session_dump" / "requests"
        os.makedirs(stale_requests / "999_GET_stale.example.com", exist_ok=True)
        open(os.path.join(stale_requests, "999_GET_stale.example.com", "transaction.json"), "w").close()

        monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
        monkeypatch.setattr(config, "WORKSPACE_DIR", output_dir / "workspace")

        # The record flow starts each recording from a clean slate
        config.reset_output_dirs()

        import asyncio

        from .conftest import (
            make_data_received_event,
            make_loading_finished_event,
            make_request_event,
            make_response_event,
        )

        async def feed():
            await agent.request_handler_for_tab(make_request_event(), "test_session")
            await agent.data_received_handler_for_tab(make_data_received_event(), "test_session")
            await agent.response_handler_for_tab(make_response_event(), "test_session")
            await agent.loading_finished_handler_for_tab(make_loading_finished_event(), "test_session")

        asyncio.run(feed())
        temp_data_dir = asyncio.run(agent._cleanup_and_build_report())

        _, success = compile_workspace(
            session_name="mysession",
            temp_data_dir=temp_data_dir,
            full_video_path="nonexistent.mp4",
            video_start_unix=0,
        )

        assert success is True

        requests_dir = output_dir / "workspace" / "session_dump" / "requests"
        request_folders = os.listdir(requests_dir)
        assert len(request_folders) == 1
        assert "999_GET_stale.example.com" not in request_folders


class TestGenerateSessionNameBlacklist:
    def test_blacklisted_word_falls_back(self, monkeypatch):
        """An AI answer containing a forbidden word (word-boundary match) is rejected."""
        import automatiq.core.recorder.ai_analyzer as ai_analyzer_module

        analyzer = VideoActionAnalyzer()

        def fake_completion(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="recording-of-login"))])

        monkeypatch.setattr(ai_analyzer_module.litellm, "completion", fake_completion)

        result = analyzer.generate_session_name([{"summary": "User logged in"}], fallback_name="fallback-name")
        assert result == "fallback-name"

    def test_clean_name_passes_through(self, monkeypatch):
        import automatiq.core.recorder.ai_analyzer as ai_analyzer_module

        analyzer = VideoActionAnalyzer()

        def fake_completion(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Login-to-GitHub"))])

        monkeypatch.setattr(ai_analyzer_module.litellm, "completion", fake_completion)

        result = analyzer.generate_session_name([{"summary": "User logged in"}], fallback_name="fallback-name")
        assert result == "login-to-github"

    def test_substring_match_not_rejected(self, monkeypatch):
        """'contest' contains 'test' as a substring but not as a word — allowed."""
        import automatiq.core.recorder.ai_analyzer as ai_analyzer_module

        analyzer = VideoActionAnalyzer()

        def fake_completion(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="contest-prize-check"))])

        monkeypatch.setattr(ai_analyzer_module.litellm, "completion", fake_completion)

        result = analyzer.generate_session_name([{"summary": "User entered a contest"}], fallback_name="fallback-name")
        assert result == "contest-prize-check"

    def test_each_blacklisted_word_rejected(self, monkeypatch):
        import automatiq.core.recorder.ai_analyzer as ai_analyzer_module

        analyzer = VideoActionAnalyzer()

        for bad in ("my-session", "the-video-tour", "clip-trimmer", "test-run"):

            def make_completion(content):
                def fake_completion(**kwargs):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

                return fake_completion

            monkeypatch.setattr(ai_analyzer_module.litellm, "completion", make_completion(bad))

            result = analyzer.generate_session_name([{"summary": "Something"}], fallback_name="fallback-name")
            assert result == "fallback-name", f"{bad} should have been rejected"
