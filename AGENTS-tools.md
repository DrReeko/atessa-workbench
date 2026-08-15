<!-- atessa-tools v3 -->
## Atessa Tools (shared CLIs on PATH)

**Written for agents.** Every entry states what the command does and why it exists, so
you never have to infer whether it applies. Shared Python entry points are installed on PATH
across Windows, Linux, and macOS.
**Just run the command from any shell.** API-backed commands read credentials from
`~/.atessa/config` or `ATESSA_API_KEY`; direct/local search sources do not require proxy credentials.
Add `--help` to any command for options.

Prefer these over native web tools, which are broken against the atessa.top proxy.

### `atessa-search "query" [-n N] [--source S] [--json] [--answer]`
- **Does:** One query across Reddit, Stack Overflow, GitHub issues, YouTube transcripts,
  support forums, Hacker News, YouTube comments, Dev.to, or general web.
- **Why:** Those sites block or rate-limit agent fetches and are exactly where
  version-specific breakage gets solved.
- **Backends & Sources:** Default `--source all` queries exactly Reddit, Stack Overflow,
  GitHub issues, YouTube transcripts, Discourse forums, and Hacker News. Sources omitted from
  `all` by default include `youtube_comments`, `devto`, and `web`. Reddit and Discourse are hit
  via DuckDuckGo `site:` queries; YouTube video selection uses YouTube search page scraping, with
  transcript text obtained via `youtube-transcript-api` (first 800 chars) and comments via `yt-dlp`
  (sampled top 10 comments).
- **Citation & Error Handling:** Only direct post or video page URLs are citable source links.
  Sampled transcript and comment excerpts are not query-filtered and MUST be verified against the
  video or post page before citing. Multi-source search labels individual provider failures in its
  results and exits nonzero when every provider fails.
- **Gotchas:** `--source` narrows to one site (`reddit`, `stackoverflow`, `github`,
  `youtube_transcripts`, `youtube_comments`, `discourse`, `devto`, `hackernews`, `web`).
  `--answer` returns the proxy's synthesized prose via `/tools/web-search` instead — costs 1 request
  and yields **no citable URLs** (synthesized non-citable answer).

### `atessa-read <url> [--raw]`
- **Does:** Any URL → clean markdown you can quote from.
- **Why:** JS-heavy pages return an empty shell to a plain fetch.
- **CLI Fallback Chain:** `urltomarkdown` (Mozilla Readability / jsdom / Turndown) → Jina Reader
  (`r.jina.ai`) → direct fetch with local tag stripping. Pass `--raw` for untouched HTML.
  The TUI Read pane uses bounded direct local extraction before remote services and rejects local,
  private, link-local, or reserved destinations, including redirects.

### `atessa-chat "prompt" [--model M] [--stream] [--max N]`
- **Does:** One-shot call to any model on the proxy (`/v1/chat/completions`).
- **Why:** A second opinion from a different model family, or offload bulk work to a
  cheap fast model without burning your own context. Stateless — pass full context.

### `atessa-image "prompt" -o out.png [--quality low|medium|high|auto] [--model M] [--aspect square|landscape|portrait]`
- **Does:** Writes a real PNG to disk from a text prompt using the proxy's `/responses` endpoint with `image_generation` tool.
- **Why:** A text-only agent cannot ship a site or deck that needs artwork. This makes
  assets a build step instead of a placeholder comment.
- **Gotchas:** Aspect is requested from the Responses API image-generation tool and verified after generation.
  Default model is `gpt-5.6-luna`. The `--model` parameter is forwarded to `/responses`.
  Consult `/v1/models` or live provider status for current model availability.

### `atessa-view <image-path> ["prompt"] [--model M] [--max N]`
- **Does:** Describes or OCRs a **local** image file.
- **Why:** Your model may have no vision, or the harness may not pass images through.
  This gets a text-only agent eyes on a file it can only see the path of.
- **Gotcha:** Local files only — the proxy cannot fetch URLs. Download first.

### `atessa-shot ["question"] [-o out.png] [--keep]`
- **Does:** Captures the whole screen, then answers a question about it — one step.
- **Why:** `atessa-view` needs a file that already exists. This lifts two agent limits at
  once: you cannot capture a screen, and you may not have vision. The user does nothing.
- **vs `atessa-view`:** `view` = a file that already exists. `shot` = make the file from
  the screen, then read it. Same vision backend, different problem.
- **Gotcha:** Capture is discarded unless `--keep` or `-o`. Needs PowerShell (Windows),
  `screencapture` (macOS), or grim / import / spectacle / gnome-screenshot (Linux).

### `atessa-explain [file|-] [--model M] [--max N]`
- **Does:** Diagnoses an error, traceback, or log excerpt (`atessa-explain [file|-]`). Also `<cmd> 2>&1 | atessa-explain -`.
- **Why:** Every `File "...", line N` or `path:N` reference in the input is resolved against real
  files under the current working directory and quoted with surrounding source code context.
- **Gotcha:** Only reads files under the current working directory.

### `atessa-council "question" model_a,model_b[,...] [--judge M] [--max N]`
- **Does:** Fans one question to several models in parallel; a judge model synthesizes
  the best combined answer and ranks the members.
- **Why:** One model has one set of blind spots. A defensible answer on a real decision
  is worth several paid calls plus a judge call.
- **Gotcha:** Architecture calls and trade-off reviews — not routine factual questions,
  where the extra calls add nothing.

### `atessa-bench "prompt" model_a,model_b[,...] [--max N]`
- **Does:** Races one prompt across models; reports time-to-first-byte, total time,
  approximate tokens/sec, and credit cost multiplier, sorted fastest first.
- **Why:** Catches a currently-degraded model before committing a workflow to it.
- **Gotcha:** Credit numbers (`<weight>x` or `-`) are cached estimates read from a local `weights.json`
  file next to config, not live per-request billing metrics. Each tested model incurs 1 request call even on error.

### `atessa-git [review|draft|commit] [--model M] [--max N]`
- **Does:** Reviews or drafts a commit message from your staged (or fallback unstaged) Git diff.
  `commit` also runs `git commit -F -`.
- **Why:** Grounding in the literal diff lets a reviewer catch regressions before they land.
- **Gotcha:** Diff text is cut to the first 12,000 characters before sending to the model (with a truncation marker).
  `commit` requires a staged diff and really commits — only run it when you mean it. `draft` prints and never commits.

### `atessa-arena "prompt" [model_a,model_b] [--vote a|b|tie] [--max N]`
- **Does:** Runs two models blind, prints both answers as A/B, reveals identities only after a vote, and updates local ratings.
- **Why:** Model names bias evaluation; blind comparison reveals which output you actually prefer.
- **Safety:** Only models with imported per-request cost at or below `7×` are eligible. `--vote` is required for non-interactive use; ratings live only in `~/.atessa/arena.json`.

### `atessa-shell "desired outcome" [--model M] [--max N] [--json]`
- **Does:** Returns exactly one OS-aware command and a risk classification. It **never executes** the command.
- **Why:** Agents need a copyable command that matches the current shell without silently changing the machine.
- **Gotcha:** Inspect the result and run it yourself. The risk label is a heuristic, not a safety proof.

### `atessa-models [list|routes|set ROLE MODEL] [--json]`
- **Does:** Lists the live catalog with imported request multipliers, shows persisted role routes, or changes one route in `~/.atessa/config`.
- **Why:** Routing model roles once keeps every CLI and the workbench consistent.
- **Gotcha:** `set` is persistent. `list` calls `/v1/models`; `routes` and `set` do not expose credentials.

### `atessa-activity [--days N] [--json]`
- **Does:** Summarizes the local request and quota-credit ledger.
- **Why:** Shows which tool and model choices consumed request multipliers without a remote billing round trip.
- **Gotcha:** This is local `~/.atessa/spend.json`, not provider billing, and makes no API call.

### `atessa-ghsearch "query" [--type repos|people] [--deep] [--json]`
- **Does:** Natural-language GitHub search using direct GitHub REST API endpoints.
- **Why:** Finds repos and people by intent rather than exact repo name.
- **Gotcha:** Uses concise lexical query expansion. Authenticated requests use `GITHUB_TOKEN` when present for a 30 req/min budget; unauthenticated searches are limited to 10 req/min/IP.

### `atessa-ping [filter] [--family F] [--quick]`
- **Does:** Concurrent health check across proxy models, or only the configured role routes with `--quick`.
- **Why:** Shows which models are answering before you commit a workflow to them.
- **Gotcha:** Each probed model costs a 1-token request. `--quick` limits the probe to the active role routes in `~/.atessa/config`.

### `atessa` — the TUI Workbench
Single command launcher (`atessa`). Eagerly registers 14 production panes: Chat (`chat`), Search (`search`),
Read (`read`), Image (`image`), View (`view`), Shot (`shot`), Council (`council`), Bench (`bench`),
Arena (`arena`), Explain (`explain`), Git (`git`), Command (`shell`), Models (`models`), and Activity (`activity`).
Every workbench tool also has a matching CLI except that the TUI is the only interface for the paste-in
Credit costs importer. `atessa-ghsearch` and `atessa-ping` remain CLI-only tools.

Also on PATH (WSL): `agent-reach` + per-platform tools. Live channels: GitHub (`gh`),
YouTube (`yt-dlp`), RSS/Atom, Bilibili search, Jina web-read. Run `agent-reach doctor`
to confirm and to see what else can be unlocked.
<!-- /atessa-tools -->
