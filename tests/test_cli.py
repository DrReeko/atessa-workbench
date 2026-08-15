from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from atessa_tui import cli, sources, weights


def invoke(entry, args: list[str], env_extra: dict[str, str] | None = None) -> tuple[str, str]:
    old_argv = sys.argv
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.argv = ["atessa-test", *args]
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    sys.stdout = captured_out
    sys.stderr = captured_err

    with patch.dict(os.environ, env_extra or {}):
        try:
            entry()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    return captured_out.getvalue(), captured_err.getvalue()


def test_github_query_expansion_is_bounded_and_lexical() -> None:
    queries = cli._github_queries("terminal session gif recorder and screenshots", deep=True, people=False)
    assert len(queries) == 4
    assert all("archived:false" in query for query in queries)
    assert all(len(query) <= 256 for query in queries)
    assert "terminal session gif recorder screenshots" in queries[0]


def test_github_search_deduplicates_and_reports_rate_limit() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        query = request.url.params["q"]
        duplicate = {
            "id": 1,
            "name": "vhs",
            "full_name": "charmbracelet/vhs",
            "html_url": "https://github.com/charmbracelet/vhs",
            "description": "Record terminal GIFs",
            "stargazers_count": 9000 if "readme" not in query else 9100,
            "forks_count": 100,
            "language": "Go",
            "topics": ["terminal", "gif"],
            "updated_at": "2026-08-01T00:00:00Z",
        }
        unique = {
            "id": 2,
            "name": "asciinema",
            "full_name": "asciinema/asciinema",
            "html_url": "https://github.com/asciinema/asciinema",
            "description": "Terminal recorder",
            "stargazers_count": 8000,
            "forks_count": 200,
            "language": "Python",
            "topics": [],
            "updated_at": "2026-08-01T00:00:00Z",
        }
        return httpx.Response(200, json={"items": [duplicate, unique]}, headers={"x-ratelimit-remaining": "9", "x-ratelimit-limit": "10", "x-ratelimit-reset": "1234"})

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    cli.httpx.AsyncClient = Client
    try:
        result = asyncio.run(cli.github_search("terminal gif recorder", "repos", True))
    finally:
        cli.httpx.AsyncClient = original

    assert len(calls) == 4
    assert result["total_count"] == 2
    assert result["items"][0]["full_name"] == "charmbracelet/vhs"
    assert result["items"][0]["stargazers_count"] == 9100
    assert result["rate_limit"] == {"remaining": 9, "reset": 1234, "limit": 10}


def test_github_rate_error_includes_budget() -> None:
    request = httpx.Request("GET", "https://api.github.com/search/repositories")
    response = httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "456"}, json={"message": "API rate limit exceeded"})
    error = cli._github_error(response)
    assert "remaining 0; reset 456" in str(error)


def test_models_routes_uses_native_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config"
        config.write_text("ATESSA_MODEL_DEFAULT=custom-model\n", encoding="utf-8")
        output, _ = invoke(cli.models_entry, ["routes", "--json"], {"ATESSA_CONFIG": str(config)})
        assert json.loads(output)["default"] == "custom-model"

def test_model_defaults_use_live_routes() -> None:
    from atessa_tui.config import ROLE_DEFAULTS

    assert ROLE_DEFAULTS["default"] == "ling-3.0-flash"
    assert ROLE_DEFAULTS["vision"] == "gpt-5.6-luna"
    assert ROLE_DEFAULTS["image"] == "gpt-5.6-luna"
    assert cli.DEFAULT_CHAT_MODEL == "claude-sonnet-4.6"
    assert cli.DEFAULT_VISION_MODEL == "claude-sonnet-4.6"
    assert cli.DEFAULT_IMAGE_MODEL == "gpt-5.6-luna"


def test_activity_reads_local_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        spend = tmp_path / "spend.json"
        spend.write_text(json.dumps({"days": {"2026-08-01": {"requests": 2, "credits": 3.5, "unpriced": 0, "tools": {"chat": 3.5}}}}), encoding="utf-8")
        output, _ = invoke(cli.activity_entry, ["--json"], {"ATESSA_HOME": str(tmp_path)})
        payload = json.loads(output)
        assert payload["total_requests"] == 2
        assert payload["days"][0]["tools"] == {"chat": 3.5}


def test_search_formats_source_rows() -> None:
    with patch.object(cli, "search_source", lambda source, query, limit: [{"source": "GitHub issues", "title": "Fix", "url": "https://example.com/fix", "snippet": "A working fix."}]):
        output, _ = invoke(cli.search_entry, ["--source", "github", "working", "fix"])
        assert "GitHub issues" in output
        assert "https://example.com/fix" in output


def test_site_results_discards_index_noise_and_keeps_relevant_threads() -> None:
    def mock_web_results(query: str, limit: int) -> list[dict[str, str]]:
        return [
            {
                "title": "ClaudeCode",
                "url": "https://www.reddit.com/r/ClaudeCode/",
                "snippet": "Reddit community for Claude Code",
            },
            {
                "title": "best way to control Claude code with open-webui?",
                "url": "https://www.reddit.com/r/ClaudeCode/comments/1qwddv4/best_way_to_control_claude_code_with_openwebui/",
                "snippet": "Happy Coder currently supports only Codex",
            },
        ]

    with patch.object(sources, "web_results", mock_web_results):
        rows = sources.site_results("claude code openwebui", "reddit.com", 5)
        assert len(rows) == 1
        assert rows[0]["url"].startswith("https://www.reddit.com/r/ClaudeCode/comments/")


def test_discourse_search_queries_every_forum_without_greedy_cutoff() -> None:
    calls: list[str] = []

    def fake_site_results(query: str, site: str, limit: int) -> list[dict[str, str]]:
        calls.append(site)
        return [{"title": site, "url": f"https://{site}/t/topic/1", "snippet": ""}]

    with patch.object(sources, "site_results", fake_site_results):
        rows = sources.discourse_results("Android remote coding", 2)

    assert calls == ["discuss.pytorch.org", "discuss.huggingface.co", "community.openai.com"]
    assert len(rows) == 2


def test_chat_stream_prints_chunks_without_network() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "config").write_text("ATESSA_API_KEY=fixture\n", encoding="utf-8")

        async def fake_stream(*args, **kwargs):
            yield "hello "
            yield "world"

        with patch.object(cli.AtessaAPI, "chat_stream", fake_stream):
            output, _ = invoke(
                cli.chat_entry,
                ["hello", "--stream"],
                {"ATESSA_HOME": str(tmp_path), "ATESSA_CONFIG": str(tmp_path / "config")},
            )
            assert output == "hello world\n"


def test_shot_uses_capture_and_removes_temp_file() -> None:
    artifact: list[Path] = []

    def mock_capture(path: Path) -> None:
        path.write_bytes(b"png")
        artifact.append(path)

    async def mock_vision(self, image_path: str, prompt: str, model: str, max_tokens: int = 2048) -> str:
        return f"parsed {Path(image_path).name}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "config").write_text("ATESSA_API_KEY=fixture\n", encoding="utf-8")
        with patch.object(cli, "_capture_screen", mock_capture), patch.object(cli.AtessaAPI, "vision", mock_vision):
            output, _ = invoke(
                cli.shot_entry,
                ["describe"],
                {"ATESSA_HOME": str(tmp_path), "ATESSA_CONFIG": str(tmp_path / "config")},
            )
            assert "parsed" in output
            assert artifact and not artifact[0].exists()


def test_ping_entry_outputs_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "config").write_text("ATESSA_API_KEY=fixture\n", encoding="utf-8")

        async def mock_ping_all(self, models: list[str], max_concurrency: int = 10):
            return [
                {"model": "glm-4", "status": "UNAVAILABLE", "latency_ms": 120, "error": "HTTP 503"},
                {"model": "claude-sonnet-5", "status": "ONLINE", "latency_ms": 250, "error": None},
            ]

        with patch.object(cli.AtessaAPI, "models", lambda self: asyncio.sleep(0, result=["glm-4", "claude-sonnet-5"])), \
             patch.object(cli.AtessaAPI, "ping_all_models", mock_ping_all):
            output, _ = invoke(
                cli.ping_entry,
                [],
                {"ATESSA_HOME": str(tmp_path), "ATESSA_CONFIG": str(tmp_path / "config")},
            )
            assert "ONLINE" in output
            assert "UNAVAILABLE" in output
            assert "glm-4" in output
def test_weights_save_completes_and_persists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"ATESSA_HOME": tmp}, clear=False):
            weights.refresh()
            weights.save_weights({"gpt-4": 2.5, "free-model-1": 0.0})
            assert weights.load_weights() == {"free-model-1": 0.0, "gpt-4": 2.5}
            weights.refresh()
def main() -> None:
    test_weights_save_completes_and_persists()
    test_model_defaults_use_live_routes()
    test_github_rate_error_includes_budget()
    test_github_search_deduplicates_and_reports_rate_limit()
    test_models_routes_uses_native_config()
    test_activity_reads_local_ledger()
    test_search_formats_source_rows()
    test_site_results_discards_index_noise_and_keeps_relevant_threads()
    test_discourse_search_queries_every_forum_without_greedy_cutoff()
    test_chat_stream_prints_chunks_without_network()
    test_shot_uses_capture_and_removes_temp_file()
    test_ping_entry_outputs_table()
    print("ALL CLI TESTS OK")


if __name__ == "__main__":
    main()
