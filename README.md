<p align="center">
  <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/automatiq_banner.svg" alt="AutomatiQ" width="600">
</p>

<p align="center">
  <em>Your <span style="color:#00FFC8;font-weight:bold">activity</span>, into <span style="color:#FF009E;font-weight:bold">automation</span>.</em>
</p>

<p align="center">
  <a href="https://discord.gg/8j7dFWMMDA"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-violet?style=flat-square" alt="License">
</p>
<p align="center">
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/test.yaml"><img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/test.yaml?branch=main&label=Tests&style=flat-square&logo=github" alt="Test Status"></a>
  <a href="https://github.com/StoneSteel27/AutomatiQ/actions/workflows/lint.yaml"><img src="https://img.shields.io/github/actions/workflow/status/StoneSteel27/AutomatiQ/lint.yaml?branch=main&label=Lint&style=flat-square&logo=python&logoColor=white" alt="Lint Status"></a>
  <img src="https://img.shields.io/pypi/v/automatiq?style=flat-square&color=blue&label=PyPI" alt="PyPI Version">
</p>

# AutomatiQ

> [!Note]
> **Alpha** ⟶ Things will break and change. Read [VISION.md](https://github.com/StoneSteel27/AutomatiQ/blob/main/VISION.md) to understand what AutomatiQ is trying to achieve and where it's headed.

AutomatiQ records HTTP requests, Websocket frames, and your interactions for reverse-engineering your **goal/intent** into a standalone Python automation/extraction script without needing any manual inspection and unnecessary paid dependencies, or heavy dependencies like a browser during runtime.

## How it works

<p align="center">
  <img src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/process.svg" alt="AutomatiQ" width="800">
</p>

1. **Record (Browser Capture)** ⟶ Chrome is launched with CDP instrumentation. Every network request, response body, cookie, WebSocket frame, and user interaction (clicks, typing, navigation) is recorded with timestamps. Press `Ctrl+C` when you're done.
2. **Compile (Vision Analysis)** ⟶ The recording is split into per-action video clips. A vision LLM watches each clip and produces structured annotations (what was clicked, what changed, whether the action succeeded). Network requests are decoded, deduplicated, and structured into a workspace dump.
3. **Agent (Sandbox Execution)** ⟶ An LLM investigator reads the workspace dump, experiments in an isolated Python/IPython environment, and iteratively produces a working script. It can test hypotheses against the live site with guardrails against loops and repetition.

## Sponsor

<a href="https://go.nodemaven.com/automatiq">
  <img align="right" src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/nodemaven_banner.png" alt="NodeMaven - High Quality Proxies" width="400">
</a>

Running web automation and scraping scripts reliably requires high-quality proxies to avoid rate limits, IP bans, and CAPTCHA blocks. [NodeMaven](https://go.nodemaven.com/automatiq) is our recommended provider.

**Why NodeMaven?**
- You get **99.9% uptime** with sticky sessions lasting up to 7 days.
- All proxies have a **fraud score under 97%** while requiring **No KYC** for registration.
- You can earn up to **10% cashback** on the data you use.

🎁 **Special codes for AutomatiQ users:**
- `AUTOMATIQ35` - **35% off** Mobile and Residential Proxies
- `AUTOMATIQ40` - **40% off** ISP (Static) Proxies

Maintaining this open-source project sustainably is made possible thanks to our sponsor, **NodeMaven**.

## Getting Started

**Requirements:** Python 3.11+ and [Google Chrome](https://www.google.com/chrome/)

```bash
pip install automatiq
```

Set your API key (AutomatiQ uses Gemini 3.5 Flash by default, but any [litellm-supported provider](https://docs.litellm.ai/docs/providers) works):

```bash
# On Linux/macOS
export GEMINI_API_KEY=your-key-here

# On Windows (PowerShell)
$env:GEMINI_API_KEY="your-key-here"
```

Run the magic command:

```bash
automatiq run https://example.com
```

That's it. Browse the site, press `Ctrl+C`, and the agent takes over.

## Usage Modes

AutomatiQ offers two main ways to operate depending on your workflow:

### 1. All-in-one execution
The `run` command records a session and immediately launches the agent to write the script.
```bash
automatiq run https://example.com
```

### 2. Step-by-step execution
If you want to record multiple sessions, or run the agent later, you can split the process:
```bash
automatiq record https://example.com   # Opens the browser and records your session
automatiq agent                        # Builds an automation script from the last recording
automatiq agent --target path/to/sess  # Builds an automation script from a specific recording
```

## Models & Custom Endpoints

AutomatiQ relies on [LiteLLM](https://github.com/BerriAI/litellm) under the hood, meaning you can easily swap the default Gemini models for OpenAI, Anthropic, GitHub Copilot, or **Local LLMs** (like Ollama, LM Studio, or vLLM).

To change the default models on the fly, use the `--model` (for the Agent) and `--recorder-model` (for Vision compilation) flags.

### Using Local Models (Ollama, LM Studio, vLLM)
If you are running a local inference server with an OpenAI-compatible endpoint, use the `--base-url` flag. You must prefix your model name with `openai/` so LiteLLM knows to route it through the OpenAI protocol.

**Example using Ollama (running locally on port 11434):**
```bash
automatiq run https://example.com \
  --model openai/llama3.3 \
  --recorder-model openai/llava \
  --base-url http://localhost:11434/v1
```

*For permanent configuration without CLI flags, see [Configuration](#configuration) below.*

## Proxy

Route the recording browser through an HTTP or SOCKS proxy — useful for testing geo-restricted content, avoiding IP bans, or recording through rotating residential proxies.

```bash
# One-off: pass a proxy URL for this recording
automatiq record --proxy socks5://127.0.0.1:1080 https://example.com

# One-off: force a direct connection (overrides config)
automatiq run --no-proxy https://example.com
```

For permanent configuration, edit `~/.automatiq/config.toml`:

```toml
[recorder_proxy]
enabled = true
server  = "http://user:pass@host:3128"   # or socks5://host:1080
# provider = "myproxies:rotate"          # dynamic "module:callable" for rotating proxies
```

> [!Tip]
> Looking for a reliable proxy provider? Our sponsor **[NodeMaven](https://go.nodemaven.com/automatiq)** offers 99.9% uptime residential & ISP proxies — use promo code `AUTOMATIQ35` (35% off Mobile/Residential) or `AUTOMATIQ40` (40% off ISP/Static).

**Dynamic provider:** The `provider` field is a `"module:callable"` string. At launch, AutomatiQ imports the module and calls the function (no arguments) to get a proxy URL. This lets you plug in rotating proxy services without hardcoding a single IP. The module just needs to be importable (place it in your working directory or on `PYTHONPATH`).

```python
# myproxies.py — a minimal rotating provider
import requests

def rotate() -> str:
    requests.get("http://127.0.0.1:8000/rotate", timeout=30)
    return "http://127.0.0.1:3128"
```

Precedence: `--no-proxy` > `--proxy URL` > `provider` > `server`. If the provider fails or returns nothing, AutomatiQ falls back to `server`. This only routes the recording browser's egress — LLM API calls, blocklist downloads, and agent tool HTTP are unaffected.

## Reference

### Keyboard Shortcuts

| Phase | Key | Action |
|:-----:|:---:|:------:|
| Recording | `Ctrl+C` | Stop recording and save session |
| Compilation | `Esc` | Skip AI analysis for remaining segments |
| Compilation | `y` / `n` | Confirm or deny the skip prompt |
| Agent | `q` | Quit the agent session |
| Agent | `Esc` | Cancel current LLM call or code execution |

*Note: `Ctrl+C` force-quits the application at any phase.*

### CLI Options

| Flag | Description |
|------|-------------|
| `--target PATH` | Path to a specific session folder to run the agent on |
| `--name NAME` | Custom name for the session folder (`record` and `run` only) |
| `--model MODEL` | LiteLLM model string for the agent |
| `--recorder-model MODEL` | Vision model for video-clip analysis |
| `--base-url URL` | Custom OpenAI-compatible API endpoint |
| `--max-steps N` | Maximum agent loop iterations (default: 100) |
| `--sandbox-timeout SEC` | Seconds per IPython cell (default: 60) |
| `--output-dir PATH` | Root directory for all output (default: ./output) |
| `--proxy URL` | Route the recording browser through a proxy (`record` and `run` only) |
| `--no-proxy` | Force a direct connection, overriding config (`record` and `run` only) |
| `--no-banner` | Skip the startup animation |
| `--verbose` | Show detailed diagnostic output |
| `-V`, `--version` | Show version |
| `-h`, `--help` | Show help message |

### Configuration

On first run, AutomatiQ creates `~/.automatiq/config.toml` with commented defaults. Edit this file to permanently override models, custom endpoints, timeouts, and recording settings.

```toml
[models]
agent    = "gemini/gemini-3.5-flash"
recorder = "gemini/gemini-3.1-flash-lite"
# base_url = "http://localhost:11434/v1"   # Uncomment for Ollama / LM Studio / vLLM

[agent]
max_steps       = 100
sandbox_timeout = 60

[recording]
fps                   = 3
segment_pad           = 2
merge_gap_threshold   = 1.5
max_frames_per_prompt = 8

[recorder_proxy]
# enabled  = false
# server   = "http://user:pass@host:3128"
# provider = "myproxies:rotate"   # dynamic "module:callable" for rotating proxies
```

*Priority order: **CLI flag** > `~/.automatiq/config.toml` > built-in defaults.*

## Development

AutomatiQ is managed using [uv](https://docs.astral.sh/uv/).

```bash
# Clone and setup environment
git clone https://github.com/StoneSteel27/AutomatiQ.git
cd AutomatiQ
uv sync

# Run the project from source
uv run automatiq run https://example.com
```

### Dev Setup
Development dependencies (pytest, ruff, pre-commit, etc.) are installed automatically via `uv sync`. This ensures `ruff`, `build`, `twine`, `pytest`, and `pre-commit` hooks (lint + format on every commit) are properly configured in your isolated environment. To set up the git hooks:

```bash
uv run pre-commit install
```

Run tests:
```bash
uv run pytest
```

## License

MIT
