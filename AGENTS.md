# Agent Guidelines

## Project Map

- `src/automatiq/mcp/` - MCP server: FastMCP stdio server (`server.py`), runtime, annotation, logging_setup, vision, status_log. Every module stays under 700 lines.
- `src/automatiq/core/` - Recorder engine: config, events, cancel_standard, browser_manager, bin_manager, telemetry, and `recorder/` (with `cdp/`, `compile/`, `extension/` subpackages).
- `tests/` - Pytest suite: 16 modules, 101 tests.
- Legacy CLI (`record`/`run`/`agent`/`resume`/`feedback`), LLM investigator agent, IPython sandbox, telemetry server backend, and banner scripts live only on the `legacy/v0.3.x` branch - do not port them back without explicit instruction.

## Daily Commands

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
```

- **Use `uv` strictly.** Do not use `pip` or `poetry`.
- Run tests regularly with `uv run pytest -q`.

## Doctrine

- **Recorder changes are test-first.** Fake CDP events fed to real handlers; new CDP event types, attributes, or output artifacts come with factories in `conftest.py`, handler tests, and compile tests in the same change.
- **Tests never touch the network, a real browser, or a real LLM.** Use fake CDP events and monkeypatch seams - e.g. patch `automatiq.mcp.runtime.vision_preflight` - instead of live services.
- **The MCP surface stays five tools** (start_recording, stop_recording, wait_for_completion, get_status, annotate_user_interactions). New capabilities extend existing tools or the per-session README, not MCP resources.
- **Never log or echo `recorder_api_key` values.**
- **Recordings under `automatiq_sessions/` are unredacted secrets** - never commit them. Same for `.env`.

## Config

Precedence: `AUTOMATIQ_*` env > `~/.automatiq/config.toml` > defaults.

Models: never hardcode model names. The model comes from `[models] recorder` config (a litellm string) only. The vision key comes ONLY from `[models] recorder_api_key` in `~/.automatiq/config.toml` (keyless `base_url` local endpoints need none); provider env vars are never consulted as a key source and are overwritten by the config key when present; the key is read fresh per recording.

## Progressive Disclosure

- For the project's long-term philosophy, roadmap, and core design principles, read `VISION.md`.

## Sponsors

<sup>Want to Sponsor this Project? Contact me via discord: [@moltensteel](https://discordapp.com/users/772033037788905482)</sup>

<details open>
<summary><b>Our Sponsors</b></summary>

</br>

Maintaining this open-source project sustainably is made possible thanks to our sponsors.

---

<a href="https://go.nodemaven.com/automatiqamdaugust">
  <img align="right" src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/nodemaven_banner.png" alt="NodeMaven - High Quality Proxies" width="400">
</a>

### [NodeMaven](https://go.nodemaven.com/automatiqamdaugust) — High Quality Proxy Infrastructure

Running web automation and scraping scripts reliably requires high-quality proxies to avoid rate limits, IP bans, and CAPTCHA blocks.

- **99.9% uptime** with sticky sessions up to 7 days.
- All proxies have a **fraud score under 97%** — **No KYC** required.
- Earn up to **10% cashback** on the data you use.

**Special codes for AutomatiQ users:**
- `AUTOMATIQ35` — **35% off** Mobile and Residential Proxies
- `AUTOMATIQ40` — **40% off** ISP (Static) Proxies

---

<a href="https://www.swiftproxy.net/?ref=AutomatiQ">
  <img align="right" src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/swiftproxy_Banner.png" alt="Swiftproxy - Residential & Static Proxies" width="400">
</a>

### [Swiftproxy](https://www.swiftproxy.net/?ref=AutomatiQ) — Residential & Static Residential Proxies

Whether you're building browser agents, AI-powered automation workflows, or large-scale data pipelines, Swiftproxy provides the proxy infrastructure to keep your sessions stable and your blocks low.

- **90M+ clean residential IPs** across global locations.
- **Static residential proxies** for stable sessions, account isolation, and multi-account workflows.
- **Non-expiring traffic** on dynamic residential proxies — use it whenever you need it.
- **Free testing** available to evaluate performance before integrating.

**AutomatiQ community offer:**
- `PROXY90` — **10% off** Residential and Static Residential Proxies

---

<a href="https://www.rapidproxy.io/?ref=AutomatiQ">
  <img align="right" src="https://raw.githubusercontent.com/StoneSteel27/AutomatiQ/main/assets/Rapidproxy_banner.png" alt="RapidProxy - Residential Proxy Network" width="400">
</a>

### [RapidProxy](https://www.rapidproxy.io/?ref=AutomatiQ) — High-Performance Residential Proxy Network

Built for developers and teams running web scrapers, browser automation, AI agents, and monitoring tools at scale.

- **90M+ residential IPs** with smart rotation for resilient requests.
- **High-concurrency support** for workloads at scale.
- **AI-powered CAPTCHA bypass** to reduce interruptions.
- **Non-expiring traffic** — use purchased bandwidth whenever you need it.

**AutomatiQ community offer:**
- **Free trial** available.
- Pricing starts at **$0.65/GB**.
- `RAPID10` — **10% off**

</details>
