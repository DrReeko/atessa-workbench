"""Native, cross-platform command-line interface for the Atessa toolbelt.

Every public command is a console-script entry point to this module.  It uses the
same API, configuration, local accounting, and platform capture primitives as
the Textual workbench; no shell, Git Bash, or WSL process is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import platform
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Callable, NoReturn

import httpx

from .api import ApiError, AtessaAPI
from .config import Config, ROLE_KEYS
from .metering import estimate_tokens
from .sources import SEARCH_SOURCES, extract_article, is_safe_url, search_source, youtube_id, youtube_transcript
from .spend import get_spend_path, record_call, record_models, recent_days
from .weights import format_weight, load_weights, weight_for
from .screens.compare import ARENA_MAX_WEIGHT, arena_eligible_models, load_elo, save_elo, update_elo
from .screens.core import (
    MAX_READ_BYTES,
    READ_TIMEOUT,
    UA,
    _bad_reader_response,
    strip_html,
)
from .screens.dev import (
    COMMAND_PROMPT,
    COMMIT_PROMPT,
    EXPLAIN_PROMPT,
    MAX_DIFF,
    REVIEW_PROMPT,
    _risk,
    _single_command,
    _strip_fences,
    traceback_sources,
)
from .screens.media import ASPECT_SUFFIX, VIEW_DEFAULT_PROMPT, _capture_cmds, _capture_missing_message

DEFAULT_CHAT_MODEL = "claude-sonnet-4.6"
DEFAULT_POWER_MODEL = "kimi-k2.7-code"
DEFAULT_IMAGE_MODEL = "gpt-5.6-luna"
DEFAULT_VISION_MODEL = "claude-sonnet-4.6"
DEFAULT_FAST_MODEL = "ling-3.0-flash"
GITHUB_API = "https://api.github.com"
GITHUB_USER_AGENT = "atessa-tui/0.3.0"
MAX_INPUT_BYTES = 512 * 1024


class CliError(Exception):
    """An expected command error, rendered without a traceback."""


@dataclass(frozen=True)
class GitHubResult:
    id: int | None
    name: str
    full_name: str
    html_url: str
    description: str
    stargazers_count: int
    forks_count: int
    language: str | None
    topics: list[str]
    updated_at: str
    type: str = "repo"


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=description)


def _print_error(prog: str, error: Exception | str) -> NoReturn:
    print(f"{prog}: {error}", file=sys.stderr)
    raise SystemExit(1)


def _run(coro):
    try:
        return asyncio.run(coro)
    except (ApiError, CliError, OSError, ValueError, httpx.HTTPError) as error:
        _print_error("atessa", error)


def _require_api(cfg: Config) -> AtessaAPI:
    if not cfg.api_key:
        raise CliError("no API key. Set it in ~/.atessa/config or export ATESSA_API_KEY.")
    return AtessaAPI(cfg)


async def _with_api(cfg: Config, action: Callable[[AtessaAPI], object]):
    api = _require_api(cfg)
    try:
        return await action(api)
    finally:
        await api.aclose()


def _rows_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    result = ["  ".join(cell.ljust(widths[index]) for index, cell in enumerate(header))]
    result.append("  ".join("-" * width for width in widths))
    result.extend("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows)
    return "\n".join(result)


def _model_list(raw: str) -> list[str]:
    models = [model.strip() for model in raw.split(",") if model.strip()]
    if not models:
        raise CliError("no models (comma-separated list required)")
    return models


def _print_search_rows(rows: list[dict[str, str]], source: str) -> None:
    if not rows:
        print("(no results)")
        return
    for row in rows:
        label = row.get("source") or SEARCH_SOURCES.get(source, source)
        title = str(row.get("title") or "").strip() or "(untitled)"
        print(f"[{label}] {title}")
        if url := str(row.get("url") or "").strip():
            print(f"  {url}")
        if snippet := " ".join(str(row.get("snippet") or "").split()):
            print(f"  {snippet[:300]}")
        print()


def _parser_search() -> argparse.ArgumentParser:
    parser = _parser("atessa-search", "Search developer sources and return cited links.")
    parser.add_argument("query", nargs="+", help="query text")
    parser.add_argument("-n", "--max-results", type=_positive_int, default=5, dest="limit")
    parser.add_argument("--source", default="all", choices=tuple(SEARCH_SOURCES))
    parser.add_argument("--json", "--raw", action="store_true", dest="raw")
    parser.add_argument("--answer", action="store_true", help="use the proxy synthesized search answer")
    return parser


def search_main() -> None:
    args = _parser_search().parse_args()
    query = " ".join(args.query)
    if args.answer:
        async def action(api: AtessaAPI):
            return await api.web_search(query, max_results=args.limit)
        result = _run(_with_api(Config(), action))
        print(result)
        return
    try:
        rows = search_source(args.source, query, args.limit)
    except Exception as error:
        _print_error("atessa-search", f"{args.source} search failed: {error}")
    if args.raw:
        print(json.dumps(rows, indent=2))
    else:
        _print_search_rows(rows, args.source)


def websearch_main() -> None:
    parser = _parser("websearch", "Legacy proxy-synthesized web search alias.")
    parser.add_argument("query", nargs="+")
    parser.add_argument("-n", "--max-results", type=_positive_int, default=5, dest="limit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    query = " ".join(args.query)

    async def action(api: AtessaAPI):
        return await api.web_search(query, max_results=args.limit)

    answer = _run(_with_api(Config(), action))
    if args.json:
        print(json.dumps({"answer": answer}, indent=2))
    else:
        print(answer)


def models_main() -> None:
    parser = _parser("atessa-models", "Browse models and configure role routing.")
    parser.add_argument("command", nargs="?", default="list", choices=("list", "routes", "set"))
    parser.add_argument("role", nargs="?")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--json", action="store_true", dest="raw")
    args = parser.parse_args()
    cfg = Config()
    if args.command == "routes":
        routes = {role: cfg.model_for(role) for role in ROLE_KEYS}
        if args.raw:
            print(json.dumps(routes, indent=2))
        else:
            rows = []
            for role, model in routes.items():
                key = ROLE_KEYS[role]
                source = "env" if os.environ.get(key) else "config" if key in cfg.values else "default"
                rows.append((role, model, source))
            print(_rows_table(("ROLE", "MODEL", "SOURCE"), rows))
        return
    if args.command == "set":
        if not args.role or not args.model:
            parser.error("set requires ROLE and MODEL")
        if args.role not in ROLE_KEYS:
            _print_error("atessa-models", f"invalid role '{args.role}'")
        try:
            cfg.set_model(args.role, args.model)
        except (ValueError, OSError) as error:
            _print_error("atessa-models", error)
        payload = {"role": args.role, "model": args.model, "config": str(cfg.path)}
        print(json.dumps(payload, indent=2) if args.raw else f"{args.role} → {args.model} (saved to {cfg.path})")
        return
    if args.role or args.model:
        _print_error("atessa-models", "list takes no positional arguments")

    async def action(api: AtessaAPI):
        return await api.models(), dict(api.model_context)

    models, context = _run(_with_api(cfg, action))
    weights = load_weights()
    rows = [
        {
            "id": model,
            "multiplier": format_weight(weights.get(model)),
            "weight": weights.get(model),
            "context_length": context.get(model),
        }
        for model in models
    ]
    if args.raw:
        print(json.dumps(rows, indent=2))
    else:
        display = [
            (row["id"], row["multiplier"], _format_context(row["context_length"]))
            for row in rows
        ]
        print(_rows_table(("MODEL", "MULTIPLIER", "CONTEXT"), display) if display else "No models available.")


def _format_context(value: int | None) -> str:
    if not value:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value // 1_000}k"
    return str(value)


def activity_main() -> None:
    parser = _parser("atessa-activity", "Inspect the local request and credit ledger.")
    parser.add_argument("--days", type=_positive_int, default=7)
    parser.add_argument("--json", action="store_true", dest="raw")
    args = parser.parse_args()
    records = []
    for day, value in recent_days(args.days):
        tools = value.get("tools", {}) if isinstance(value.get("tools", {}), dict) else {}
        records.append({
            "date": day,
            "requests": int(value.get("requests", 0)),
            "credits": float(value.get("credits", 0.0)),
            "unpriced": int(value.get("unpriced", 0)),
            "tools": dict(sorted(tools.items(), key=lambda item: -float(item[1]))),
        })
    if args.raw:
        print(json.dumps({
            "note": "Local credit ledger from CLI/workbench operations. Separate from remote provider billing.",
            "days": records,
            "total_credits": round(sum(row["credits"] for row in records), 4),
            "total_requests": sum(row["requests"] for row in records),
        }, indent=2))
        return
    print("Atessa Local Activity Ledger")
    print("Note: Local CLI/workbench request & credit history (not remote provider billing).\n")
    if not records:
        print(f"No local activity recorded in {get_spend_path()}.")
        return
    rows = [
        (
            row["date"], str(row["requests"]), f"{row['credits']:.2f}", str(row["unpriced"]),
            ", ".join(f"{name} ({float(cost):.2f})" for name, cost in list(row["tools"].items())[:3]) or "-",
        )
        for row in records
    ]
    print(_rows_table(("DATE", "REQUESTS", "CREDITS", "UNPRICED", "TOP TOOLS"), rows))
    print(f"Total ({len(records)} days): {sum(row['requests'] for row in records)} requests, {sum(row['credits'] for row in records):.2f} credits")


async def _fetch_read_chain(url: str, raw: bool) -> str:
    if not is_safe_url(url):
        raise CliError(f"URL destination is not permitted: {url}")
    headers = {"User-Agent": UA}
    if raw:
        async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=10.0)) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.text[:2_000_000]
    if youtube_id(url):
        try:
            transcript = await asyncio.to_thread(youtube_transcript, url)
            if transcript.strip():
                return f"# YouTube transcript\n\n{transcript}"
        except Exception:
            pass
    body = ""
    current_url = url
    async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=10.0)) as client:
        for _ in range(5):
            if not is_safe_url(current_url):
                raise CliError(f"Redirect destination is not permitted: {current_url}")
            response = await client.get(current_url, headers=headers, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    break
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            if response.status_code < 400:
                body = response.text[:MAX_READ_BYTES]
                if body.strip():
                    try:
                        article = await asyncio.to_thread(extract_article, body, url)
                        if article.strip():
                            return article
                    except Exception:
                        pass
            break
        for reader_url in (
            f"https://urltomarkdown.herokuapp.com/?{urllib.parse.urlencode({'url': url, 'title': 'true', 'links': 'true'})}",
            f"https://r.jina.ai/{url}",
        ):
            try:
                if not is_safe_url(reader_url):
                    continue
                response = await client.get(reader_url, headers=headers, follow_redirects=True)
                if response.status_code < 400 and not _bad_reader_response(response.text[:2000]):
                    return response.text[:MAX_READ_BYTES]
            except httpx.HTTPError:
                continue
    result = strip_html(body)
    if result:
        return result
    raise CliError(f"Could not fetch {url} through any reader")


def read_main() -> None:
    parser = _parser("atessa-read", "Fetch a page as clean Markdown.")
    parser.add_argument("url")
    parser.add_argument("--raw", "--html", action="store_true")
    args = parser.parse_args()
    url = args.url if re.match(r"^https?://", args.url, flags=re.I) else f"https://{args.url}"
    print(_run(_fetch_read_chain(url, args.raw)))


def chat_main() -> None:
    parser = _parser("atessa-chat", "One-shot proxy chat.")
    parser.add_argument("prompt", nargs="+")
    parser.add_argument("--model", default=DEFAULT_CHAT_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    prompt = " ".join(args.prompt)

    async def action(api: AtessaAPI):
        messages = [{"role": "user", "content": prompt}]
        if not args.stream:
            return await api.chat(messages, model=args.model, max_tokens=args.max_tokens)
        chunks: list[str] = []
        async for chunk in api.chat_stream(messages, model=args.model, max_tokens=args.max_tokens):
            print(chunk, end="", flush=True)
            chunks.append(chunk)
        print()
        return ""

    result = _run(_with_api(Config(), action))
    if result:
        print(result)


async def _ask_models(api: AtessaAPI, prompt: str, models: list[str], max_tokens: int) -> list[tuple[str, bool, str]]:
    async def ask(model: str) -> tuple[str, bool, str]:
        try:
            return model, True, await api.chat([{"role": "user", "content": prompt}], model, max_tokens)
        except Exception as error:
            return model, False, str(error)
    return await asyncio.gather(*(ask(model) for model in models))


def council_main() -> None:
    parser = _parser("atessa-council", "Ask several models and have a judge synthesize the result.")
    parser.add_argument("question")
    parser.add_argument("models")
    parser.add_argument("--judge", default=DEFAULT_POWER_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    models = _model_list(args.models)

    async def action(api: AtessaAPI):
        answers = await _ask_models(api, args.question, models, args.max_tokens)
        for model, succeeded, answer in answers:
            print(f"=== {model} ===")
            print(answer if succeeded else f"error: {answer}")
            print()
        successful = [(model, answer) for model, succeeded, answer in answers if succeeded]
        if not successful:
            raise CliError("atessa-council: every council member failed; nothing to judge.")
        sections = "\n\n".join(f"### Answer from `{model}`\n{answer}" for model, answer in successful)
        prompt = (
            "You are judging a council of AI models. The original question was:\n\n"
            f"{args.question}\n\nHere are the successful answers:\n\n{sections}\n\n"
            "Write a short synthesis of the best combined answer, then rank the models from best to worst with a one-line justification for each."
        )
        print(f"=== JUDGE ({args.judge}) ===")
        return await api.chat([{"role": "user", "content": prompt}], args.judge, args.max_tokens)

    try:
        result = _run(_with_api(Config(), action))
    finally:
        record_models("council", models + [args.judge])
    print(result)


def bench_main() -> None:
    parser = _parser("atessa-bench", "Race streamed responses across models.")
    parser.add_argument("prompt")
    parser.add_argument("models")
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    models = _model_list(args.models)

    async def action(api: AtessaAPI):
        async def race(model: str):
            started = time.monotonic()
            ttft: float | None = None
            response = ""
            try:
                async for chunk in api.chat_stream([{"role": "user", "content": args.prompt}], model, args.max_tokens):
                    if ttft is None:
                        ttft = time.monotonic() - started
                    response += chunk
                total = time.monotonic() - started
                if not response.strip():
                    return model, float("inf"), total, 0, "empty", "empty output"
                return model, ttft if ttft is not None else float("inf"), total, estimate_tokens(response), "ok", response[:60].replace("\n", " ")
            except Exception as error:
                return model, float("inf"), time.monotonic() - started, 0, "error", str(error)[:60]
        return await asyncio.gather(*(race(model) for model in models))

    try:
        results = _run(_with_api(Config(), action))
    finally:
        record_models("bench", models)
    weights = load_weights()
    rows = []
    for model, ttft, total, tokens, status, output in sorted(results, key=lambda row: row[1]):
        rows.append((
            model,
            f"{ttft:.2f}" if ttft != float("inf") else "-",
            f"{total:.2f}",
            str(tokens),
            f"{tokens / total:.1f}" if total > 0 and status == "ok" else "-",
            format_weight(weights.get(model)), status, output,
        ))
    print(_rows_table(("model", "ttft s", "total s", "~tok", "tok/s", "credits", "status", "output"), rows))
    if not any(row[4] == "ok" for row in results):
        raise SystemExit(1)


def _capture_screen(path: Path) -> None:
    try:
        import mss
        import mss.tools
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            shot = sct.grab(sct.monitors[0])
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        if path.is_file() and path.stat().st_size:
            return
    except Exception:
        path.unlink(missing_ok=True)
    failures: list[str] = []
    for command in _capture_cmds(str(path)):
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f"{command[0]}: {error}")
            continue
        if result.returncode == 0 and path.is_file() and path.stat().st_size:
            return
        failures.append(f"{command[0]}: {(result.stderr or result.stdout or 'no output file').strip()[:200]}")
    raise CliError(_capture_missing_message() if not failures else f"Capture failed: {'; '.join(failures)}")


def image_main() -> None:
    parser = _parser("atessa-image", "Generate a PNG through the Atessa proxy.")
    parser.add_argument("prompt", nargs="+")
    parser.add_argument("-o", "--out")
    parser.add_argument("--quality", choices=("low", "medium", "high", "auto"), default="high")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--aspect", choices=("square", "landscape", "portrait", "wide", "tall"), default="square")
    args = parser.parse_args()
    output = Path(args.out) if args.out else Path(f"image_{int(time.time())}.png")
    if output.exists():
        _print_error("atessa-image", f"output file '{output}' already exists")
    suffix_key = {"wide": "landscape", "tall": "portrait"}.get(args.aspect, args.aspect)
    prompt = " ".join(args.prompt) + ASPECT_SUFFIX[suffix_key]

    async def action(api: AtessaAPI):
        return await api.image_gen(prompt, quality=args.quality, model=args.model)

    image = _run(_with_api(Config(), action))
    output.write_bytes(image)
    record_call("image", args.model)
    print(f"{output} ({len(image)} bytes)")

def view_main() -> None:
    parser = _parser("atessa-view", "Describe or OCR a local image.")
    parser.add_argument("image")
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("--model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    image = Path(args.image)
    if not image.is_file():
        _print_error("atessa-view", f"file not found: {image}")
    prompt = " ".join(args.prompt) or VIEW_DEFAULT_PROMPT

    async def action(api: AtessaAPI):
        return await api.vision(str(image), prompt, args.model, args.max_tokens)

    print(_run(_with_api(Config(), action)))
    record_call("view", args.model)


def shot_main() -> None:
    parser = _parser("atessa-shot", "Capture every monitor and answer a question about it.")
    parser.add_argument("prompt", nargs="*")
    parser.add_argument("-o", "--out")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--model", default=DEFAULT_VISION_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    keep = args.keep or bool(args.out)
    path = Path(args.out) if args.out else Path(tempfile.gettempdir()) / f"atessa-shot-{time.time_ns()}.png"
    if path.exists():
        _print_error("atessa-shot", f"output file '{path}' already exists")
    try:
        _capture_screen(path)
        prompt = " ".join(args.prompt) or "Describe this screen in detail. Transcribe any visible text verbatim."

        async def action(api: AtessaAPI):
            return await api.vision(str(path), prompt, args.model, args.max_tokens)

        print(_run(_with_api(Config(), action)))
        record_call("shot", args.model)
    finally:
        if not keep:
            path.unlink(missing_ok=True)
    if keep:
        print(f"capture: {path}", file=sys.stderr)


def explain_main() -> None:
    parser = _parser("atessa-explain", "Diagnose a log, error, or traceback using local source context.")
    parser.add_argument("file", nargs="?", default="-")
    parser.add_argument("--model", default=DEFAULT_FAST_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    try:
        if args.file == "-":
            text = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1).decode("utf-8", "replace")
        else:
            text = Path(args.file).read_bytes()[:MAX_INPUT_BYTES + 1].decode("utf-8", "replace")
    except OSError as error:
        _print_error("atessa-explain", error)
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        _print_error("atessa-explain", f"input exceeds {MAX_INPUT_BYTES} bytes")
    if not text.strip():
        _print_error("atessa-explain", "no input")
    prompt = EXPLAIN_PROMPT.format(
        os=platform.platform(), machine=platform.machine() or "unknown", python=sys.version.split()[0], cwd=Path.cwd(), text=text, sources=(f"\nReal source from this machine:\n\n{traceback_sources(text)}" if traceback_sources(text) else ""),
    )

    async def action(api: AtessaAPI):
        return await api.chat([{"role": "user", "content": prompt}], args.model, args.max_tokens)

    print(_run(_with_api(Config(), action)))
    record_call("explain", args.model)


def _git(command: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise CliError(str(error)) from error


def git_main() -> None:
    parser = _parser("atessa-git", "Review, draft, or commit a real Git diff.")
    parser.add_argument("action", nargs="?", default="review", choices=("review", "draft", "commit"))
    parser.add_argument("--model", default=DEFAULT_POWER_MODEL)
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    if _git(["git", "rev-parse", "--is-inside-work-tree"]).returncode:
        _print_error("atessa-git", "not a git repository")
    staged = _git(["git", "diff", "--staged"])
    if staged.returncode:
        _print_error("atessa-git", staged.stderr.strip() or "git diff failed")
    diff = staged.stdout
    source = "staged"
    if args.action == "commit" and not diff.strip():
        _print_error("atessa-git", "nothing is staged; commit requires staged changes (git add)")
    if args.action != "commit" and not diff.strip():
        unstaged = _git(["git", "diff"])
        if unstaged.returncode:
            _print_error("atessa-git", unstaged.stderr.strip() or "git diff failed")
        diff, source = unstaged.stdout, "unstaged"
    if not diff.strip():
        _print_error("atessa-git", "no staged or unstaged changes")
    diff = diff[:MAX_DIFF] + ("\n... (diff truncated)" if len(diff) > MAX_DIFF else "")
    if args.action == "review":
        prompt = REVIEW_PROMPT.format(source=source, diff=diff)
    else:
        prompt = COMMIT_PROMPT.format(diff=diff)
    tree_before = _git(["git", "write-tree"]).stdout.strip() if args.action == "commit" else ""

    async def action(api: AtessaAPI):
        return await api.chat([{"role": "user", "content": prompt}], args.model, args.max_tokens)

    result = _strip_fences(_run(_with_api(Config(), action)))
    record_call("git", args.model)
    print(result)
    if args.action != "commit":
        return
    tree_after = _git(["git", "write-tree"]).stdout.strip()
    if tree_after != tree_before:
        _print_error("atessa-git", "staged changes modified during prompt generation; aborting commit")
    committed = _git(["git", "commit", "-F", "-"], input_text=result)
    if committed.returncode:
        _print_error("atessa-git", (committed.stdout + committed.stderr).strip() or "git commit failed")
    if output := (committed.stdout + committed.stderr).strip():
        print(output)


def shell_main() -> None:
    parser = _parser("atessa-shell", "Suggest exactly one platform-aware command; never executes it.")
    parser.add_argument("request", nargs="+")
    parser.add_argument("--model", default=DEFAULT_FAST_MODEL)
    parser.add_argument("--max", type=_positive_int, default=512, dest="max_tokens")
    parser.add_argument("--json", action="store_true", dest="raw")
    args = parser.parse_args()
    request = " ".join(args.request)
    prompt = COMMAND_PROMPT.format(
        os=platform.platform(), shell="cmd.exe (PowerShell must be invoked explicitly)" if platform.system() == "Windows" else "/bin/sh", cwd=Path.cwd(), request=request,
    )

    async def action(api: AtessaAPI):
        return await api.chat([{"role": "user", "content": prompt}], args.model, args.max_tokens)

    command = _single_command(_run(_with_api(Config(), action)))
    record_call("shell", args.model)
    risk = _risk(command)
    if args.raw:
        print(json.dumps({"command": command, "risk": risk}))
    else:
        print(command)
        print(f"atessa-shell: not executed; risk: {risk}", file=sys.stderr)


def arena_main() -> None:
    parser = _parser("atessa-arena", "Blind A/B comparison with local persistent ratings.")
    parser.add_argument("prompt")
    parser.add_argument("models", nargs="?")
    parser.add_argument("--pair", "--models", dest="pair")
    parser.add_argument("--vote", choices=("a", "b", "tie", "1", "2", "t"))
    parser.add_argument("--max", type=_positive_int, default=1024, dest="max_tokens")
    args = parser.parse_args()
    pair_input = args.pair or args.models
    cfg = Config()

    async def action(api: AtessaAPI):
        models = await api.models()
        if pair_input:
            pair = _model_list(pair_input)
            if len(pair) != 2:
                raise CliError("explicit models option must specify exactly two comma-separated model ids")
            if any(model not in models for model in pair):
                raise CliError("one or more explicit models were not found in the catalog")
            if any(weight_for(model) is None or weight_for(model) > ARENA_MAX_WEIGHT for model in pair):
                raise CliError(f"explicit models must have imported cost ≤{ARENA_MAX_WEIGHT:g}×")
        else:
            eligible = arena_eligible_models(models)
            if len(eligible) < 2:
                raise CliError(f"at least two models with imported cost ≤{ARENA_MAX_WEIGHT:g}× are required")
            pair = random.sample(eligible, 2)
        answers = await _ask_models(api, args.prompt, pair, args.max_tokens)
        if not all(ok for _, ok, _ in answers):
            raise CliError("both arena models must answer successfully")
        return pair, [answer for _, _, answer in answers]

    pair, answers = _run(_with_api(cfg, action))
    record_models("arena", pair)
    print("=== Model A ===")
    print(answers[0])
    print("\n=== Model B ===")
    print(answers[1])
    vote = args.vote
    if vote is None:
        if not sys.stdin.isatty():
            _print_error("atessa-arena", "option --vote a|b|tie is required in non-interactive mode")
        while vote is None:
            entered = input("Vote [1=Model A, 2=Model B, t=Tie]: ").strip().casefold()
            vote = {"a": "a", "1": "a", "b": "b", "2": "b", "tie": "tie", "t": "tie"}.get(entered)
    vote = {"1": "a", "2": "b", "t": "tie"}.get(vote, vote)
    print("\n=== Result ===")
    print(f"Model A was: {pair[0]}")
    print(f"Model B was: {pair[1]}")
    try:
        state = load_elo()
        update_elo(state, pair[0], pair[1], {"a": 1.0, "b": 0.0, "tie": 0.5}[vote])
        save_elo(state)
    except (OSError, ValueError) as error:
        _print_error("atessa-arena", f"could not update ratings: {error}")
    print("Winner: Model A" if vote == "a" else "Winner: Model B" if vote == "b" else "Result: Tie")


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": GITHUB_USER_AGENT}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _query_terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]*", query.casefold())
    stop = {"a", "an", "and", "app", "application", "for", "find", "i", "in", "is", "it", "like", "of", "repo", "repository", "software", "that", "the", "to", "tool", "with"}
    terms = [word for word in words if word not in stop]
    return terms[:8] or words[:8]


def _github_queries(query: str, deep: bool, people: bool) -> list[str]:
    if people:
        return [query]
    terms = _query_terms(query)
    base = " ".join(terms)
    queries = [f"{base} in:name,description,topics archived:false"]
    if deep:
        queries.extend([
            f"{base} in:readme archived:false",
            f"{base} topic:{terms[0]} archived:false" if terms else base,
            f"{base} stars:>=10 archived:false",
        ])
    return list(dict.fromkeys(query_item[:256] for query_item in queries if query_item.strip()))
def _github_error(response: httpx.Response) -> CliError:
    try:
        detail = response.json().get("message", response.text)
    except (ValueError, AttributeError):
        detail = response.text
    remaining = response.headers.get("x-ratelimit-remaining")
    reset = response.headers.get("x-ratelimit-reset")
    suffix = f" (remaining {remaining}; reset {reset})" if remaining is not None else ""
    return CliError(f"GitHub search failed: {detail}{suffix}")


async def github_search(query: str, kind: str, deep: bool) -> dict:
    queries = _github_queries(query, deep, kind == "people")
    endpoint = "/search/users" if kind == "people" else "/search/repositories"
    async with httpx.AsyncClient(base_url=GITHUB_API, headers=_github_headers(), timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        async def search(expanded: str):
            response = await client.get(endpoint, params={"q": expanded, "per_page": 30})
            if response.status_code >= 400:
                raise _github_error(response)
            return response.json(), response.headers
        responses = await asyncio.gather(*(search(expanded) for expanded in queries))
    by_name: dict[str, GitHubResult] = {}
    rate = {"remaining": None, "reset": None, "limit": None}
    for data, headers in responses:
        rate = {
            "remaining": int(headers["x-ratelimit-remaining"]) if headers.get("x-ratelimit-remaining", "").isdigit() else None,
            "reset": int(headers["x-ratelimit-reset"]) if headers.get("x-ratelimit-reset", "").isdigit() else None,
            "limit": int(headers["x-ratelimit-limit"]) if headers.get("x-ratelimit-limit", "").isdigit() else None,
        }
        for item in data.get("items", []):
            if kind == "people":
                result = GitHubResult(
                    id=item.get("id"), name=str(item.get("login") or ""), full_name=str(item.get("login") or ""),
                    html_url=str(item.get("html_url") or ""), description=str(item.get("type") or ""),
                    stargazers_count=0, forks_count=0, language=None, topics=[], updated_at="", type="person",
                )
            else:
                result = GitHubResult(
                    id=item.get("id"), name=str(item.get("name") or ""), full_name=str(item.get("full_name") or ""),
                    html_url=str(item.get("html_url") or ""), description=str(item.get("description") or ""),
                    stargazers_count=int(item.get("stargazers_count") or 0), forks_count=int(item.get("forks_count") or 0),
                    language=item.get("language"), topics=list(item.get("topics") or []), updated_at=str(item.get("updated_at") or ""),
                )
            if result.full_name:
                existing = by_name.get(result.full_name)
                if existing is None or result.stargazers_count > existing.stargazers_count:
                    by_name[result.full_name] = result
    items = sorted(by_name.values(), key=lambda item: (-item.stargazers_count, item.full_name.casefold()))
    return {"query": query, "expanded_queries": queries, "total_count": len(items), "items": [asdict(item) for item in items], "rate_limit": rate}


def ghsearch_main() -> None:
    parser = _parser("atessa-ghsearch", "Natural-language GitHub repository or developer discovery.")
    parser.add_argument("query", nargs="+")
    parser.add_argument("--type", choices=("repos", "people"), default="repos", dest="kind")
    parser.add_argument("--deep", action="store_true", help="run four complementary lexical searches")
    parser.add_argument("--json", action="store_true", dest="raw")
    args = parser.parse_args()
    result = _run(github_search(" ".join(args.query), args.kind, args.deep))
    if args.raw:
        print(json.dumps(result, indent=2))
        return
    print(f"GitHub {args.kind}: {result['total_count']} unique results")
    print(f"Queries: {' | '.join(result['expanded_queries'])}")
    for item in result["items"]:
        print(f"\n{item['full_name']} · ★ {item['stargazers_count']:,}")
        print(f"  {item['html_url']}")
        if item["description"]:
            print(f"  {item['description']}")
        if item["language"] or item["topics"]:
            print(f"  {' · '.join(filter(None, [item['language'], ', '.join(item['topics'])]))}")
    rate = result["rate_limit"]
    if rate["remaining"] is not None:
        print(f"\nGitHub search budget: {rate['remaining']}/{rate['limit']} remaining", file=sys.stderr)


def ping_main() -> None:
    parser = _parser("atessa-ping", "Fast concurrent health & availability check across proxy models.")
    parser.add_argument("filter", nargs="?", help="optional model family filter (e.g. glm, kimi, claude)")
    parser.add_argument("--family", "-f", help="filter by model family substring")
    parser.add_argument("--quick", "-q", action="store_true", help="probe only configured active role routes")
    parser.add_argument("--json", action="store_true", dest="raw", help="output JSON")

    args = parser.parse_args()
    family_filter = (args.family or args.filter or "").casefold()
    cfg = Config()

    async def action(api: AtessaAPI):
        if args.quick:
            targets = list(dict.fromkeys(cfg.routes.values()))
        else:
            targets = await api.models()

        if family_filter:
            targets = [m for m in targets if family_filter in m.casefold()]

        if not targets:
            raise CliError(f"no models matched filter '{family_filter}'")

        return await api.ping_all_models(targets)

    results = _run(_with_api(cfg, action))

    if args.raw:
        print(json.dumps(results, indent=2))
        return

    header = ("MODEL", "STATUS", "LATENCY", "NOTES")
    rows = []
    for r in results:
        lat = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "-"
        notes = r["error"] or "OK"
        rows.append((r["model"], r["status"], lat, notes))

    print(_rows_table(header, rows))
COMMANDS: dict[str, Callable[[], None]] = {
    "activity": activity_main, "arena": arena_main, "bench": bench_main,
    "chat": chat_main, "council": council_main, "explain": explain_main,
    "ghsearch": ghsearch_main, "git": git_main, "image": image_main,
    "models": models_main, "ping": ping_main, "read": read_main, "search": search_main,
    "shell": shell_main, "shot": shot_main, "view": view_main,
    "websearch": websearch_main,
}


def _named_main(name: str) -> None:
    try:
        COMMANDS[name]()
    except BrokenPipeError:
        raise SystemExit(0)


def activity_entry() -> None: _named_main("activity")
def arena_entry() -> None: _named_main("arena")
def bench_entry() -> None: _named_main("bench")
def chat_entry() -> None: _named_main("chat")
def council_entry() -> None: _named_main("council")
def explain_entry() -> None: _named_main("explain")
def ghsearch_entry() -> None: _named_main("ghsearch")
def git_entry() -> None: _named_main("git")
def image_entry() -> None: _named_main("image")
def models_entry() -> None: _named_main("models")
def read_entry() -> None: _named_main("read")
def search_entry() -> None: _named_main("search")
def shell_entry() -> None: _named_main("shell")
def shot_entry() -> None: _named_main("shot")
def view_entry() -> None: _named_main("view")
def websearch_entry() -> None: _named_main("websearch")
def ping_entry() -> None: _named_main("ping")
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
        cmd = sys.argv.pop(1)
        COMMANDS[cmd]()
    else:
        print(f"Usage: python -m atessa_tui.cli <{'|'.join(sorted(COMMANDS.keys()))}> [args]")
