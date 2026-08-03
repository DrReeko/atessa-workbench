# Handoff: Model Health & Availability Probe Tool (`atessa-status`)

## Context & Motivation
Users need a fast, non-intrusive CLI and TUI tool to check which upstream proxy models are currently online and responding, particularly when specific model families (e.g. GLM, Kimi) experience temporary outages or capacity limits. 

The tool should present status information respectfully and professionally (standard infrastructure diagnostics tone, like `ping` or `status`), without offensive or alarmist language.

**CRITICAL INSTRUCTION FOR IMPLEMENTATION AGENT:**
- **DO NOT USE VISION / IMAGE INPUTS.** Probe vision models with simple 1-token text completions or basic capability flags. Do not attempt screenshot capture, OCR, or image generation during status probes.

---

## Tool Specification: `atessa-status`

### 1. Proposed Tool Names
- **`atessa-status`** *(Recommended primary name — standard CLI status convention like `gh status`)*
- **`atessa-ping`** *(Alias / alternate entrypoint for quick health checks)*

---

### 2. Core Architecture & Probe Mechanics

1. **Model Discovery:**
   - Fetch live catalog from `/v1/models` (or fall back to known family groups from `sources.py` / `weights.json`).

2. **Fast Concurrent Probing (`asyncio` + `httpx.AsyncClient`):**
   - Probe candidates concurrently using `asyncio.gather` bounded by an `asyncio.Semaphore(10)`.
   - Payload: Minimal 1-token completion request (`max_tokens=1`, `messages=[{"role": "user", "content": "hi"}]`).
   - Timeout: Strict 4-second timeout per probe.

3. **Status Classification:**
   - `ONLINE` (200 OK, valid response text within timeout)
   - `UNAVAILABLE` (5xx server errors, 404, or 429 capacity/rate limits)
   - `AUTH_ERROR` (401/403 credential issues)
   - `TIMEOUT` (> 4s latency)

4. **Grouped Reporting by Family:**
   - Group results by model vendor/family prefix: `GLM`, `Kimi / Moonshot`, `Claude / Anthropic`, `GPT / OpenAI`, `DeepSeek`, `Qwen`, etc.

---

### 3. Command Line Interface Design

```bash
# General status overview across model families
atessa-status

# Filter probes to specific family or keyword
atessa-status --family glm
atessa-status --family kimi

# Quick probe only of configured role routes (default, power, vision, etc.)
atessa-status --quick

# Machine-readable JSON output for scripts and subagents
atessa-status --json
```

---

### 4. Implementation Steps for Next Agent

1. **Add Probe Logic to Core (`atessa_tui/api.py`):**
   - Implement `async def probe_model_health(model_id: str, timeout: float = 4.0) -> dict` returning status, latency, and error summary.
   - Implement `async def probe_all_models(family: str | None = None, quick_roles: bool = False) -> list[dict]`.

2. **Create CLI Entry Point (`atessa-status`):**
   - Add CLI implementation in `atessa_tui/cli.py` under `def cmd_status()`.
   - Register entrypoint in `pyproject.toml` / `setup.py` under `[project.scripts]`.

3. **Integrate into TUI Workbench (`atessa`):**
   - Add status indicator / health view to `ModelsPane` (`atessa_tui/screens/system.py` or new status tab).

4. **Tests:**
   - Add contract tests in `tests/test_cli.py` mocking healthy, timing out, and 500 error HTTP responses.

---

### 5. Verification Checklist for Implementing Agent
- [ ] `atessa-status` executes in < 3 seconds total across catalog using async concurrency.
- [ ] Handles 429, 500, 503, and timeout errors gracefully without crashing or hanging.
- [ ] No image/vision API calls are made during probing.
- [ ] Output is clean, professional, and grouped by model provider family.
