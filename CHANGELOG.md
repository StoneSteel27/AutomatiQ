# Changelog

All notable changes to AutomatiQ are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-07-10

### Added
- **Zero-Fingerprint CDP Recorder** — Telemetry and visuals are now injected via a custom Chrome extension, and events are streamed back to a local loopback asyncio HTTP ActionServer (`/act`). This eliminates the detectable `CDP Runtime.addBinding` and `addScriptToEvaluateOnNewDocument` fingerprinting trace used by anti-bot systems.
- **Brave Browser Integration** — Added full, first-class support for Brave as the default recorder browser to leverage its tracker blocking and fingerprinter randomization defenses. Includes a managed portable browser manager to automatically download, checksum-verify, and recursively strip Gatekeeper quarantine attributes on macOS under `~/.automatiq/browsers`.
- **Anonymous Zero-Identity Telemetry** — Tracks high-level aggregate usage volumes (executed steps, token consumption, exception types, and browser choices) to identify and improve failure points without ever logging URLs, credentials, files, prompts, or generated code. Simple opt-out flags (`--no-telemetry` or `enabled = false` in `~/.automatiq/config.toml`) are supported.
- **Interactive Multiline Feedback CLI** — New `automatiq feedback` command without arguments opens a rich, interactive multiline feedback input box powered by `prompt_toolkit` supporting hotkeys (`Enter` for newline, `Alt+Enter` to submit). Falls back automatically to a line-by-line stdin loop if `prompt_toolkit` is not installed.

### Changed
- **Redirect-Hop Tracking** — Upgraded network capture to persist and log all intermediate redirect hops (like `302 Found` login chains) with `redirected: true` and `redirected_to_url` markers. Prevents critical request parameters and post-data bodies from being discarded in complex login flows.
- **Python 3.12+ Required** — Upgraded minimum supported and tested environment requirement to Python 3.12+. Removed support for Python 3.11 from test suites and package metadata.
- **Linter Environment** — Upgraded reference pre-commit Python linter environment target to Python 3.12.
- **Optimized Test Matrix** — Retired Windows runners from GitHub Actions matrix to focus on fast POSIX execution targets (macOS and Linux), saving workflow resources.
- **Lazy-Load Resume Picker** — Refactored resume listing scanning to lazy-load YAML counters on-demand, showing the resumable sessions table instantly.

### Fixed
- **Windows Resource Locking** — Added explicit database and stream closures inside `BrowserAgent.__del__` and the video compilation pipeline, solving Windows-specific `PermissionError` unlinking locks on garbage collection.
- **Isolate Test Directory Resolution** — Sandboxed local browser cache directory resolution inside testing frameworks to fully prevent development host cache pollution.

### Removed
- **Unreliable Domain Blocklists** — Cleared external domain blocklists that were prone to host migration failure and stale lookup latency.

## [0.2.2] — 2026-06-30

### Added
- **Session resume** — new `automatiq resume [name]` command picks up a previous agent session from disk. All messages, cell outputs, mode, and metadata (token counts, llm_calls, cells_executed) are restored. Snapshots are saved incrementally after each tool call, so sessions survive crashes.
- **LLM streaming** — the agent's LLM calls now stream in real-time. Thoughts and text are rendered live via a single persistent Rich `Live` region, replacing the old step-based UI. An elapsed timer and session token counter are displayed inline.
- **Proxy support for the recording browser** — route Chrome through an HTTP or SOCKS proxy via `--proxy URL` / `--no-proxy` CLI flags or the `[recorder_proxy]` config section. Supports dynamic `"module:callable"` providers for rotating proxy services.
- **`debug()` log level** — new `debug()` helper and `log_debug` signal. The file log always captures DEBUG-level output; the terminal shows `[DEBUG]` messages only when `--verbose` is passed. 17 diagnostic log calls across the recorder, sandbox, and bin_manager were downgraded from INFO to DEBUG.
- **Restore progress bar** — `%restore` now displays a Rich progress bar showing how many cells have been re-executed.
- **Banner 256-color fallback** — per-letter 256-color fallback for terminals without truecolor support.
- `--name` flag added to the Rich help OPTIONS table.
- Resume command section in README.

### Changed
- **Resume session picker rewritten** — uses a Rich `Table` showing session name + human-readable 12-hour timestamp. Sessions are preloaded during the banner animation to eliminate post-banner delay.
- **Lazy session scanning** — `list_resumable_sessions()` no longer parses YAML files on scan; `messages_count` and `cell_count` are zero until `load_counts()` is called.
- **Token tracking** — session token counter now includes prompt + completion tokens (was completion-only). Baseline is restored from saved metadata on resume.
- **cell_counter on resume** — uses `max(saved_cell_counter, len(exec_history))` to avoid output cache collisions when the saved counter is stale.
- **History folder rename** — the history directory is renamed to the current timestamp on save, keeping the latest snapshot at a predictable name.
- **Session metadata restore** — `llm_calls`, `cells_executed`, `prompt_tokens`, `completion_tokens`, and `total_tokens` are restored from saved metadata when resuming.
- **Elapsed timer** — now session-wide and tracks generation-only time (excludes tool execution and user input waits).
- **Redundant log lines removed** — "Target URL", "AI Model", "Resuming session from:", and "Using session at:" deleted (duplicated by banner and panels).
- Recorder modularized into `cdp/` (CDP event handlers) and `compile/` (network/WS/action compilers) subpackages.
- Version bumped to `0.2.2`.

### Fixed
- **Tool-call flicker** — eliminated by using a single persistent `Live` region that transitions between streaming and tool-execution phases without re-creating the display.
- **Spinner residue** — spinners now stop cleanly when idle, leaving no leftover characters on the terminal.
- **Token explosion** — the session token counter no longer grows unboundedly across turns.
- **Post-banner delay on resume** — `resume` added to the preload heavy-import block so litellm, IPython, and binaries load during the banner (previously `check_api_keys()` triggered a fresh litellm import after the banner).

## [0.2.1] — 2026-06-24

### Added
- **WebSocket recording** — full capture of WebSocket connections (text + binary frames, control frames) alongside HTTP traffic. Each connection is compiled into its own folder under `session_dump/websockets/` with a `transaction.json` and individual frame files named `{seq}_{direction}_{delta_ms}ms{opcode_suffix}.{ext}`.
- **Disk-streaming recorder** — recording data now streams directly to a temp directory during the session instead of accumulating in memory. Eliminates memory pressure on long recordings.
- **Live recording spinner** — animated spinner shows active recording status in the terminal.
- **Unified crash report system** — if the recorder crashes mid-session, a structured crash report is saved alongside the partial session dump.
- **WebSocket knowledge in agent system prompt** — the internal agent now understands the `websockets/` directory structure, frame file naming convention, timestamp reconstruction, and `websockets` library usage for replay scripts.
- **"Read the JS, not the ciphertext" principle** — agent prompt now instructs tracing JavaScript crypto logic rather than attempting manual decryption of encrypted payloads.
- `websockets` library declared as a runtime dependency so generated WebSocket replay scripts work out of the box.
- NodeMaven sponsor section in README and AGENTS.md with promo codes (`AUTOMATIQ35`, `AUTOMATIQ40`).
- `--target` CLI option documented in README.

### Changed
- Default agent model updated to `gemini/gemini-3.5-flash`.
- `max_steps` default clarified as `100` (was documented as `60` in README).
- Build system requirement bumped to `setuptools>=77` for PEP 639 SPDX license support.
- AGENTS.md trimmed to 59 lines following lean documentation guidelines.
- Recorder docstring import path corrected (`automatiq.core.recorder`, not `automatiq.recorder`).

### Fixed
- Magika content detection now runs before body file copy in `data_compressor.py` — fixes pre-existing `.bin` extension bug on detected files.
- New tabs have a reduced (~10ms) blind window for WebSocket events via a polling loop and reordered `network.enable` command.
- `active_websockets[rid]` is set before file I/O and not popped on `WebSocketClosed` — late-arriving frames still get sequence numbers.

### Removed
- `pydantic-pick` dependency (declared but never imported).
- `requirements.txt` (stale and diverged from `pyproject.toml`; `uv.lock` is the canonical lock file).

## [0.2.0] — 2026-05-28

### Added
- **UI/backend decoupling** — business logic split into `src/automatiq/core/`, presentation into `src/automatiq/cli/`. Communication via [Blinker](https://blinker.readthedocs.io/) pub/sub events.
- **Cross-platform CI** — GitHub Actions workflow runs `pytest` on Ubuntu, macOS, and Windows across Python 3.11, 3.12, and 3.13.
- Comprehensive test suite for the IPython sandbox (execution, cancellation, `rg`/`jq`/`gron` integration).
- Integration tests for the main agent loop, state machine, and Blinker event architecture.
- Pydantic core schema validation tests.
- Background sandbox preloading during the startup banner for faster first-cell execution.
- PyPI downloads badge in README.

### Changed
- Migrated development packages from `optional-dependencies` to `dependency-groups` for seamless `uv sync`.
- Terminal logs routed through a centralized Rich console — timestamps removed from log output.
- Agent output rendering inverted to an Event Router pattern.

### Fixed
- Python 3.12 thread deadlocks in the sandbox causing test hangs.
- Clean thread exit on hard aborts (Ctrl+C).
- CDP network noise silenced on EOF.
- Relative import bug in `cli/callbacks.py`.

## [0.1.3] — 2026-05-08

### Added
- **GitHub Copilot support** — OAuth-based authentication with helpful error messages for unsupported models.
- `prompt_toolkit` migration for multiline input and safe readline handling.
- Agent session history saved in timestamped subdirectories under `~/.automatiq/history/`.
- Background Esc listener migrated to `prompt_toolkit` for cross-platform robustness.
- Log file renamed to match the compiled workspace session name.

### Changed
- Recorder transitioned to the events system with AI-enhanced session naming.
- `gron` prioritized over `jq` for JSON exploration in agent prompts.
- Model names bumped to latest Gemini versions.

### Fixed
- Shadow DOM clicks, cross-origin iframe keystrokes, and missed click capture in the recorder.
- `stop_token` monitoring during agent loop — session history now saves on Ctrl+C (Linux).
- Duplicated provider prefix in model suggestions.
- Log events routing through `console.py` to restore recorder UI.

## [0.1.2] — 2026-05-05

### Added
- Sandbox preloaded in background during startup banner.
- Local models guide (Ollama, LM Studio, vLLM) in README.
- PyPI downloads badge.

### Changed
- README restructured for clarity and formatting.
- Debug statements removed.

## [0.1.1] — 2026-04-28

### Added
- Scoop shim resolver for Windows PATH detection.
- System binary copy fallback in `bin_manager.py`.
- Agent prompt updates.

## [0.1.0] — 2026-04-23

### Added
- Initial alpha release.
- CDP-based browser recorder (HTTP requests, responses, cookies, user interactions).
- Vision LLM analysis of per-action video clips.
- IPython sandboxed agent with `reading` / `testing` / `building` modes.
- Rich terminal UI with animated banner, live spinner, and markdown rendering.
- CLI flags (`--model`, `--recorder-model`, `--base-url`, `--max-steps`, `--sandbox-timeout`, `--output-dir`, `--no-banner`, `--verbose`).
- LiteLLM integration for multi-provider model support.
- `~/.automatiq/config.toml` persistent configuration.
- Blocklist filtering (StevenBlack + AdAway) for recorded network traffic.
