# Atessa Toolbelt

## About

This is a customer-made, fan-built repository. It has no association with
[atessa.top](https://atessa.top) in any way other than being a fan of the
service.


> **A Note from the Author:**  
> Sharing this slop by request... I built this toolbelt for myself because I use these models all day, every day. I wrapped standard but poorly understood security methodologies into a fast, portable TUI and CLI workflow that works across Windows, Linux, and macOS. You'll find a ton of AI slop also... I've tried to generalize it for you guys, but I don't have a ton of time to clean the edge cases up right now. Feel free to use it, adapt it, sell it, chew it, and inspect it—but please be kind as not all functions are tested fully!  
>  
> **THIS IS A BROKEN, SECURITY NIGHTMARE. DON'T USE THIS ON A PRODUCTION MACHINE!!! YOU'VE BEEN WARNED!!**  
>  
> *Batteries not included. Your mile may vary. Items settled during shipping. Hide your cat.*

---

**Atessa Toolbelt** is a unified, cross-platform terminal suite providing both an interactive Textual TUI workbench (`atessa`) and 17 standalone command-line tools. It bridges model routing, real-time web search, visual inspection, code debugging, and decision benchmarking into clean, zero-friction terminal utilities.

---

## Key Features

- **14-Pane TUI Workbench (`atessa`):** A keyboard and mouse-driven Textual interface bundling Chat, Web Search, Article Reader, Image Generation, Vision, Screen Capture, Model Council, Benchmarking, ELO Arena, Explain/Diagnostics, Git Reviewer, Command Generator, Model Selector, and Activity Ledger into a single UI.
- **17 Native CLI Tools:** Run one-shot tasks directly from Git Bash, Windows CMD/PowerShell, or Linux/macOS terminals without launching a GUI or web browser.
- **Role-Based Model Routing:** Map model roles once (`default`, `vision`, `ocr`, `power`, `image`) in `~/.atessa/config` or via the TUI, ensuring consistent performance across all tools.
- **Local Privacy & Security:** Subprocess calls use fixed list-argument execution (never unvetted `shell=True`). Web fetching includes strict private/loopback URL rejection, and natural language command drafting requires explicit user review and confirmation before running.
- **Zero-Network Local Fallbacks:** Inspection, metering, role mapping, local ELO scoring, and local trace diagnostics work offline without unnecessary API calls.

---

## Installation

### Prerequisites
- Python 3.10 or higher
- Git (for version control integration)
- Windows PowerShell (for screen capture on Windows), `screencapture` (macOS), or `grim`/`imagemagick`/`spectacle`/`gnome-screenshot` (Linux)

### From Source (Developer / Local Installation)

```bash
git clone https://github.com/your-username/atessa-tui.git
cd atessa-tui
pip install -e .
```

### From Python Wheel

```bash
pip install atessa_tui-0.3.0b1-py3-none-any.whl
```

### Windows Installer
Run `AtessaSetup-0.3.0b1.exe` for a per-user installation under `%LOCALAPPDATA%\Programs\Atessa`. It automatically exposes all 18 commands on your user `PATH`.

---

## Quick Start & Configuration

1. **Set your API Key:**  
   Create `~/.atessa/config` or set the `ATESSA_API_KEY` environment variable:

   ```ini
   ATESSA_API_KEY=sk-proxy-your-api-key-here
   ```

2. **Launch the TUI Workbench:**

   ```bash
   atessa
   ```

3. **Or run any CLI command directly:**

   ```bash
   # One-shot chat
   atessa-chat "Explain optimistic concurrency in 2 sentences"

   # Search developer sources (Reddit, Stack Overflow, GitHub, YouTube transcripts)
   atessa-search "ModuleNotFoundError textual" --source stackoverflow

   # Convert any web page to clean Markdown
   atessa-read https://textual.textualize.io/

   # Review staged Git changes before committing
   atessa-git review
   ```

---

## CLI Command Reference

All commands support `--help` and accept optional flags such as `--json` or `--model`.

| Command | Purpose / Function | Key Options & Notes |
| :--- | :--- | :--- |
| `atessa` | Launch the full 14-pane Textual TUI workbench. | Full interactive UI |
| `atessa-chat` | One-shot streaming conversation with any routed model. | `[prompt] [--model M] [--stream]` |
| `atessa-search` | Multi-source developer search with verified page citations. | `[query] [--source S] [--json] [--answer]` |
| `atessa-read` | Convert any web URL into clean, readable Markdown. | `<url> [--raw]` |
| `atessa-image` | Generate PNG assets steering aspect and quality. | `"prompt" -o out.png [--aspect square\|landscape\|portrait]` |
| `atessa-view` | Describe or OCR a local image file. | `<image-path> ["prompt"] [--model M]` |
| `atessa-shot` | Capture desktop screens and analyze the screenshot. | `["question"] [-o out.png] [--keep]` |
| `atessa-council` | Fan a question to N models and synthesize a verdict with a judge model. | `"question" model_a,model_b [--judge M]` |
| `atessa-bench` | Race a prompt across models, reporting TTFT, tok/s, and weight. | `"prompt" model_a,model_b` |
| `atessa-arena` | Blind A/B model comparison with local ELO ratings. | `"prompt" [model_a,model_b] [--vote a\|b\|tie]` |
| `atessa-explain` | Diagnose tracebacks, compiler errors, or logs using local source code context. | `[file\|-] [--model M]` |
| `atessa-git` | Review, draft, or commit staged Git diffs conventional-commit style. | `[review\|draft\|commit] [--model M]` |
| `atessa-shell` | Translate natural language into a single OS-aware command (never auto-executed). | `"desired outcome" [--json]` |
| `atessa-models` | Browse model catalog, view request weights, and set role routes. | `[list\|routes\|set ROLE MODEL]` |
| `atessa-activity`| Inspect the local request count and credit ledger. | `[--days N] [--json]` |
| `atessa-ghsearch`| Lexical natural-language search over GitHub repositories and users. | `"query" [--type repos\|people] [--deep] [--json]` |

The `atessa-models` command and Models pane fetch the complete live model catalog from the configured `/v1/models` endpoint. Use **Reload** to refresh it; filtering `claude` shows Claude entries whenever the provider exposes them.
| `websearch` | Legacy proxy-synthesized search query alias. | `"query"` |

---

## TUI Workbench Overview

The TUI workbench organizes 14 production tool panes into four semantic groups:

- **Create (Keys 1–5, S):**
  - `1`: **Chat** — Streaming multi-turn conversation with model switching.
  - `2`: **Search** — Synthesized web answers or direct source lookups.
  - `3`: **Read** — Safe local and fallback URL-to-Markdown reader.
  - `4`: **Image** — PNG asset studio with aspect and quality steering.
  - `5`: **Vision** — Describe or transcribe local images.
  - `S`: **Screen Capture** — Instant desktop capture + vision analysis.
- **Compare (Keys 6–8):**
  - `6`: **Council** — Parallel model synthesis with an AI judge.
  - `7`: **Benchmark** — Real-time TTFT and throughput speed races.
  - `8`: **Arena** — Blind double-blind A/B evaluation with ELO ratings.
- **Develop (Keys 9, G, D):**
  - `9`: **Explain** — Context-aware stack trace and log diagnosis.
  - `G`: **Git** — Diff review, conventional commit drafting, and safe execution.
  - `D`: **Command** — Shell command suggestion with risk safety banners.
- **System (Keys M, A):**
  - `M`: **Models** — Live model catalog, request weight breakdown, and role routing.
  - `A`: **Activity** — Local ledger and usage history viewer.

### Key Bindings & Navigation
- `Ctrl+J` / `Ctrl+K`: Cycle through tool panes sequentially.
- `Down` while the theme selector is focused: Apply the next built-in theme immediately; `Up` moves backward. The selector is also available in the top bar.
- `Ctrl+T`: Cycle through built-in workbench themes (Midnight, Nord, Dracula, Tokyo Night, Aurora).
- `Ctrl+I`: Toggle the side inspector panel.
- `Ctrl+L`: Clear active pane output or chat log.
- `Esc`: Close modals or popups.

---

## Architecture & Security Principles

1. **Safe Subprocess Execution:** All external process calls (such as `git` or screen capture helpers) execute via explicit list arrays (`argv`) rather than `shell=True` strings.
2. **Explicit User Approval for Shell Commands:** The `Command` tool generates single-line commands but **never** executes them automatically. Running a suggested command requires an explicit click on **Run**, and high-risk operations require an additional confirmation step.
3. **URL & Destination Safety:** The reader module filters target URLs before making requests, blocking loopback (`127.0.0.1`, `localhost`), link-local, private IP ranges, and malformed destinations.
4. **Local Data Isolation:** Credentials, ledger files, arena ratings, and role routes reside in `~/.atessa/` on the local machine and are never transmitted in telemetry.

---

## Development & Testing

Run the included automated test suite to verify CLI and TUI functionality:

```bash
# Run CLI contract tests
python tests/test_cli.py

# Run Textual UI interaction and responsive tests
python tests/test_tui.py
```

---

## License

Distributed under the [MIT License](LICENSE). See `LICENSE` for more information.
