@everyone

**AutomatiQ v0.3.1 is officially live! 🚀**

This release introduces absolute stealth recorder architecture, native Brave integration, robust session redirect tracking, anonymous telemetry, and a fully interactive multiline terminal feedback interface.

---

### 🛡️ Default Browser: Brave
We have switched our default recorder browser to **Brave** out of the box to leverage its native adblocking, tracker blocking, and fingerprint randomization.
*(If Brave isn't installed locally on your system, AutomatiQ will automatically download and set up a sandboxed portable copy under `~/.automatiq/browsers`).*

### 🕶️ Redesigned Anti-Detection & CDP Stealth
The recorder engine has been rebuilt from the ground up:
* **Zero CDP Fingerprints**: Telemetry and visuals are now injected via a custom background extension worker instead of using the detectable `CDP Runtime.addBinding` hooks, completely neutralizing bot-detection scanners.
* **Debugger Neutralization**: Integrated evasion protocols automatically bypass debugger timing traps and infinite anti-debugging `eval("debugger")` loops without ever halting execution.
* **Benchmark Tested**: We've personally tested the recorder against popular bot-detection benchmarks and found zero leaks. If you find a target where the recorder is caught, let us know!

### 📬 Safe, Anonymous Feedback & Light Telemetry
Because web scraping and automation can operate in sensitive environments, we know developers aren't always comfortable posting public logs or filing public GitHub issues.

* **Anonymous Feedback Box**: Run `automatiq feedback` (without arguments) to open an interactive, fully anonymous multiline feedback input box and send your thoughts directly to the team.
* **Ultra-Lightweight Error Telemetry**: Collects only high-level aggregate volume metrics (e.g., exception names, step counts, and active module) to help us catch bugs early. **No personal data—like URLs, credentials, file paths, prompts, or generated code—is ever sent.**
  * Disable anytime inline with `automatiq --no-telemetry` or permanently inside `~/.automatiq/config.toml`.

---

**Get the update:**
```bash
pip install -U automatiq
```

Thank you for all your support as we build the cleanest and stealthiest web reverse-engineering tool on the market! 💻🔥
