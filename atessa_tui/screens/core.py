"""Production core panes: streaming chat, search, URL reader, and model routing."""
from __future__ import annotations

import asyncio
import html
import re
import urllib.parse

import httpx
from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    LoadingIndicator,
    Markdown,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from .. import capabilities
from ..api import ApiError
from ..config import ROLE_KEYS
from ..metering import format_context, model_meta
from ..weights import import_age_days, weight_for
from .importer import WeightsImportScreen
from ..sources import (
    SEARCH_SOURCES,
    extract_article,
    feed_entries,
    is_safe_url,
    looks_like_feed,
    search_source,
    youtube_id,
    youtube_transcript,
)
from .base import ToolMeta, ToolPane


CHAT_ROLES = ("default", "power", "vision")
ASSIGNMENT_KEYS = {"default": "d", "vision": "v", "ocr": "o", "power": "p", "image": "i"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
READ_TIMEOUT = 15.0
MAX_READ_BYTES = 5 * 1024 * 1024  # 5 MB




def strip_html(text: str) -> str:
    """Convert an HTML response to readable plain Markdown-compatible text."""
    text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def sanitize_markdown_text(text: str, allow_links: bool = False) -> str:
    """Sanitize untrusted external content so it cannot inject Markdown images or links unexpectedly."""
    if not text:
        return ""
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    if not allow_links:
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("[", "\\[").replace("]", "\\]")
    return text

def _bad_reader_response(body: str) -> bool:
    return not body.strip() or "AuthenticationRequiredError" in body


class ChatPane(ToolPane):
    """Persistent, role-routed conversation with token-by-token streaming."""

    META = ToolMeta(
        key="chat",
        title="Chat",
        purpose="Swap which model answers mid-thread, with the conversation intact.",
        request_estimate="1 per message",
        group="Core",
        role="default",
        action="Send",
        input_label="Message",
        output_label="Reply, history intact",
        examples=(
            ("Explain", "Explain optimistic concurrency in practical terms."),
            ("Draft", "Draft a concise release note for a safer config migration."),
            ("Review", "List the failure modes in a background job queue design."),
        ),
        avoid="one-shot web research; use Search when current sources matter",
    )
    EXAMPLE_SELECTOR = "#chat-input"
    RESULT_SELECTOR = "#chat-log"
    BINDINGS = [Binding("ctrl+l", "clear", "Clear chat")]

    DEFAULT_CSS = """
    ChatPane { height: 100%; }
    ChatPane .core-flow { height: 2; padding: 0 1; color: $text-muted; }
    ChatPane #chat-model-band { height: 1; padding: 0 1; }
    ChatPane #chat-log { height: 1fr; margin: 0 1; border-top: solid $primary; }
    ChatPane .chat-user { margin-top: 1; padding: 0 1; border-left: thick $primary; }
    ChatPane .chat-assistant { margin-top: 1; padding: 0 1; border-left: thick $success; }
    ChatPane #chat-compose { height: 3; margin: 0 1; }
    ChatPane #chat-input { width: 1fr; }
    ChatPane #chat-role { width: 34; margin-left: 1; }
    ChatPane #chat-send { width: 9; margin-left: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, str]] = []
        self.role = "default"
        self._streaming = False
        self._run_id = 0
    def compose_body(self) -> ComposeResult:
        yield Static("", id="chat-model-band")
        yield VerticalScroll(id="chat-log")
        with Horizontal(id="chat-compose"):
            yield Input(placeholder="Message the model…", id="chat-input")
            yield Select(
                self._role_options(),
                value="default",
                allow_blank=False,
                id="chat-role",
            )
            yield Button("Send", variant="primary", id="chat-send")

    def on_mount(self) -> None:
        self._update_model_band()

    def _update_model_band(self) -> None:
        model = self.model_for(self.role)
        self.query_one("#chat-model-band", Static).update(
            f"[b]ROUTE[/b]  {escape(self.role)}  →  {escape(model)}    "
            f"[dim]turns {len(self.messages) // 2}[/dim]"
        )

    def _role_options(self) -> list[tuple[str, str]]:
        """Options that name the control itself, since Select shows only its value."""
        return [
            (f"Model: {role} · {self.model_for(role)}", role) for role in CHAT_ROLES
        ]

    def refresh_routes(self) -> None:
        select = self.query_one("#chat-role", Select)
        current = self.role
        select.set_options(self._role_options())
        select.value = current
        self._update_model_band()

    @on(Select.Changed, "#chat-role")
    def _role_changed(self, event: Select.Changed) -> None:
        role = str(event.value)
        if role in CHAT_ROLES:
            self.role = role
            self._update_model_band()

    @on(Input.Submitted, "#chat-input")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_primary()

    @on(Button.Pressed, "#chat-send")
    def _send_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_primary()

    def run_primary(self) -> None:
        field = self.query_one("#chat-input", Input)
        text = field.value.strip()
        if not text:
            self.notify("Enter a message first", severity="warning")
            return
        if self._streaming:
            self.notify("Wait for the current response", severity="warning")
            return
        field.value = ""
        self._run_id += 1
        self._send(text, self._run_id)

    def action_clear(self) -> None:
        if self._streaming:
            self.notify("Wait for the current response before clearing", severity="warning")
            return
        self.messages.clear()
        self.query_one("#chat-log", VerticalScroll).remove_children()
        self._update_model_band()
        self.notify("Conversation cleared")

    @work(exclusive=True, group="chat-stream")
    async def _send(self, text: str, run_id: int) -> None:
        if self._run_id != run_id:
            return
        log = self.query_one("#chat-log", VerticalScroll)
        model = self.model_for(self.role)
        self.messages.append({"role": "user", "content": text})
        await log.mount(Markdown(text, classes="chat-user"))
        response = Markdown("_waiting for first token…_", classes="chat-assistant")
        await log.mount(response)
        log.scroll_end(animate=False)
        self._streaming = True
        reply = ""
        try:
            try:
                async for delta in self.api.chat_stream(self.messages, model=model):
                    reply += delta
                    response.update(reply)
                    log.scroll_end(animate=False)
            except (ApiError, httpx.HTTPError) as exc:
                if reply:
                    response.update(f"{reply}\n\n> **Stream interrupted:** {exc}")
                    self.messages.append({"role": "assistant", "content": reply})
                else:
                    response.update(f"**error:** {exc}")
                    self.messages.pop()
                self.notify(str(exc), severity="error")
                self.app.record_activity("chat", "error", str(exc), model)
                return
            if not reply:
                raise ApiError("stream completed without response text")
            self.messages.append({"role": "assistant", "content": reply})
            self.app.record_activity(
                "chat", "ok", f"turn {len(self.messages) // 2} · {len(reply)} chars", model
            )
        finally:
            if self._run_id == run_id:
                self._streaming = False
                self._update_model_band()

class SearchPane(ToolPane):
    """Server-backed web search with a compact result-count control."""

    META = ToolMeta(
        key="search",
        title="Search",
        purpose="One query across the sources where real fixes get posted.",
        request_estimate="0",
        group="Core",
        action="Search",
        input_label="Question or error",
        output_label="Grouped, linked results",
        examples=(
            ("Reddit fix", "latest real-world fix for a Python Textual focus bug"),
            ("YouTube walkthrough", "recent video walkthrough fixing Windows Python packaging"),
            ("GitHub issue", "current workaround for a breaking library regression"),
        ),
        avoid="reading one known URL; use Read for that",
    )
    EXAMPLE_SELECTOR = "#search-query"
    RESULT_SELECTOR = "#search-answer"

    DEFAULT_CSS = """
    SearchPane { height: 100%; }
    SearchPane .core-flow { height: 2; padding: 0 1; color: $text-muted; }
    SearchPane #search-controls { height: 3; margin: 0 1; }
    SearchPane #search-query { width: 1fr; }
    SearchPane #search-source { width: 26; margin-left: 1; }
    SearchPane #search-limit { width: 12; margin-left: 1; }
    SearchPane #search-run { width: 10; margin-left: 1; }
    SearchPane #search-status { height: 1; padding: 0 1; }
    SearchPane #search-spinner { height: 1; display: none; }
    SearchPane #search-spinner.busy { display: block; }
    SearchPane #search-results { height: 1fr; margin: 0 1; padding: 0 1; border-top: solid $primary; }
    SearchPane #search-answer { width: 1fr; height: auto; }
    """
    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="search-controls"):
            yield Input(
                placeholder="Describe the coding problem or exact error…", id="search-query"
            )
            yield Select(
                [(label, key) for key, label in SEARCH_SOURCES.items()],
                value="all",
                allow_blank=False,
                id="search-source",
            )
            yield Select(
                [("3 each", 3), ("5 each", 5), ("8 each", 8)],
                value=3,
                allow_blank=False,
                id="search-limit",
            )
            yield Button("Search", variant="primary", id="search-run")
        yield Static("Ready", id="search-status")
        yield LoadingIndicator(id="search-spinner")
        with VerticalScroll(id="search-results"):
            yield Markdown("", id="search-answer")

    @on(Input.Submitted, "#search-query")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_primary()

    @on(Button.Pressed, "#search-run")
    def _search_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_primary()

    def run_primary(self) -> None:
        query = self.query_one("#search-query", Input).value.strip()
        if not query:
            self.notify("Enter a search query first", severity="warning")
            return
        limit = int(self.query_one("#search-limit", Select).value)
        source = str(self.query_one("#search-source", Select).value)
        self._run_id += 1
        self._search(query, limit, source, self._run_id)

    @work(exclusive=True, group="web-search")
    async def _search(self, query: str, limit: int, source: str, run_id: int) -> None:
        if self._run_id != run_id:
            return
        spinner = self.query_one("#search-spinner", LoadingIndicator)
        status = self.query_one("#search-status", Static)
        answer_widget = self.query_one("#search-answer", Markdown)
        label = SEARCH_SOURCES.get(source, source)
        spinner.add_class("busy")
        estimate = (
            "1 Atessa request"
            if source == "web" and not capabilities.has("ddgs")
            else "0 Atessa requests"
        )
        status.update(
            f"Searching {escape(label)} · up to {limit} results · {estimate} · {escape(query)}"
        )
        answer_widget.update("")
        try:
            if source == "web" and not capabilities.has("ddgs"):
                answer = await self.api.web_search(query, max_results=limit)
                if self._run_id != run_id:
                    return
                answer_widget.update(sanitize_markdown_text(answer, allow_links=False))
                status.update(f"Complete · proxy · limit {limit} · {len(answer)} chars")
                self.app.record_activity("search", "ok", f"{query} · {limit} results")
                return

            required = {
                "web": "ddgs",
                "reddit": "ddgs",
                "youtube_transcripts": "youtube",
                "youtube_comments": "youtube_comments",
                "discourse": "ddgs",
                "devto": "ddgs",
            }.get(source)
            if required and not capabilities.has(required):
                install = (
                    "pip install youtube-transcript-api ddgs"
                    if source == "youtube_transcripts"
                    else "pip install yt-dlp ddgs"
                    if source == "youtube_comments"
                    else "pip install ddgs"
                )
                answer_widget.update(
                    f"**{escape(label)} is locked.** Unlock it with:\n\n```\n{install}\n```"
                )
                status.update(f"{escape(label)} locked")
                return

            rows = await asyncio.to_thread(search_source, source, query, limit)
            if self._run_id != run_id:
                return
            if not rows:
                answer_widget.update(f"No {escape(label)} results for **{escape(query)}**.")
                status.update(f"{escape(label)} · no results")
                return

            lines = [f"### {escape(label)} · {len(rows)} results", ""]
            current_source = ""
            for index, row in enumerate(rows, 1):
                origin = str(row.get("source") or "")
                if origin and origin != current_source:
                    current_source = origin
                    lines.extend(["", f"**{sanitize_markdown_text(origin)}**", ""])
                title = str(row.get("title") or "").strip() or "(untitled)"
                clean_title = sanitize_markdown_text(title)
                url = str(row.get("url") or "").strip()
                if url and is_safe_url(url):
                    lines.append(f"{index}. [{clean_title}]({url.replace(')', '%29')})")
                else:
                    lines.append(f"{index}. {clean_title}")
                snippet = str(row.get("snippet") or "").strip()
                if snippet:
                    lines.append(f"   {sanitize_markdown_text(snippet)}")
            answer_widget.update("\n".join(lines))
            status.update(f"Complete · {escape(label)} · {len(rows)} results")
            self.app.record_activity(
                "search", "ok", f"{label}: {query} · {len(rows)} results"
            )
        except Exception as error:
            if self._run_id == run_id:
                answer_widget.update(f"**error:** {escape(str(error))}")
                status.update(f"{escape(label)} search failed")
                self.notify(str(error), severity="error")
                self.app.record_activity("search", "error", f"{label}: {error}")
        finally:
            if self._run_id == run_id:
                spinner.remove_class("busy")


class ReadPane(ToolPane):
    """Fetch a URL through two reader services, then direct HTML stripping."""

    META = ToolMeta(
        key="read",
        title="Read",
        purpose="Any URL, video or feed as clean text. Extraction runs locally.",
        request_estimate="0",
        group="Core",
        action="Fetch",
        input_label="URL, video, or feed",
        output_label="Clean Markdown",
        examples=(
            ("Example page", "https://example.com"),
            ("Python docs", "https://docs.python.org/3/whatsnew/"),
            ("Textual docs", "https://textual.textualize.io/guide/"),
        ),
        avoid="searching across sites; use Search when no specific URL is known",
    )
    EXAMPLE_SELECTOR = "#read-url"
    RESULT_SELECTOR = "#read-page"

    DEFAULT_CSS = """
    ReadPane { height: 100%; }
    ReadPane .core-flow { height: 2; padding: 0 1; color: $text-muted; }
    ReadPane #read-controls { height: 3; margin: 0 1; }
    ReadPane #read-url { width: 1fr; }
    ReadPane #read-run { width: 9; margin-left: 1; }
    ReadPane #read-status { height: 1; padding: 0 1; }
    ReadPane #read-spinner { height: 1; display: none; }
    ReadPane #read-spinner.busy { display: block; }
    ReadPane #read-page-scroll { height: 1fr; margin: 0 1; padding: 0 1; border-top: solid $primary; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="read-controls"):
            yield Input(placeholder="https://example.com", id="read-url")
            yield Button("Fetch", variant="primary", id="read-run")
        yield Static("Ready · bounded fallback chain", id="read-status")
        yield LoadingIndicator(id="read-spinner")
        with VerticalScroll(id="read-page-scroll"):
            yield Markdown("", id="read-page")
    @on(Input.Submitted, "#read-url")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.run_primary()

    @on(Button.Pressed, "#read-run")
    def _read_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_primary()

    def run_primary(self) -> None:
        url = self.query_one("#read-url", Input).value.strip()
        if not url:
            self.notify("Enter a URL first", severity="warning")
            return
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            url = f"https://{url}"
            self.query_one("#read-url", Input).value = url
        self._run_id += 1
        self._fetch(url, self._run_id)

    @work(exclusive=True, group="url-read")
    async def _fetch(self, url: str, run_id: int) -> None:
        if self._run_id != run_id:
            return
        spinner = self.query_one("#read-spinner", LoadingIndicator)
        status = self.query_one("#read-status", Static)
        page = self.query_one("#read-page", Markdown)
        spinner.add_class("busy")
        status.update(f"Fetching · {escape(url)}")
        page.update(f"_fetching {url}…_")
        try:
            text, source = await self._fetch_chain(url)
            if self._run_id != run_id:
                return
            if text is None:
                message = f"Could not fetch {url} through any reader"
                page.update(f"**error:** {escape(message)}")
                status.update("Failed · all 3 steps exhausted")
                self.notify(message, severity="error")
                self.app.record_activity("read", "error", message)
                return
            safe_text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
            page.update(safe_text)
            status.update(f"Complete · {escape(source)} · {len(safe_text)} chars")
            self.app.record_activity("read", "ok", f"{url} · {source}")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if self._run_id == run_id:
                page.update(f"**error:** {escape(str(exc))}")
                status.update(f"Fetch failed · {escape(str(exc))}")
                self.notify(str(exc), severity="error")
                self.app.record_activity("read", "error", str(exc))
        finally:
            if self._run_id == run_id:
                spinner.remove_class("busy")
    async def _fetch_chain(self, url: str) -> tuple[str | None, str]:
        if not is_safe_url(url):
            raise ValueError(f"URL destination is not permitted: {url}")

        if capabilities.has("youtube") and youtube_id(url):
            try:
                transcript = await asyncio.to_thread(youtube_transcript, url)
                if transcript.strip():
                    return f"# YouTube transcript\n\n{transcript}", "YouTube transcript"
            except Exception:
                pass

        body = ""
        current_url = url
        headers = {"User-Agent": UA}

        async with httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=10.0)) as client:
            for _ in range(5):
                if not is_safe_url(current_url):
                    raise ValueError(f"Redirect destination is not permitted: {current_url}")
                try:
                    response = await client.get(current_url, headers=headers, follow_redirects=False)
                except httpx.HTTPError:
                    break

                if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        break
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue

                if response.status_code < 400:
                    content_bytes = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        content_bytes.extend(chunk)
                        if len(content_bytes) > MAX_READ_BYTES:
                            break
                    body = content_bytes.decode("utf-8", errors="replace")
                break

            if body.strip():
                if capabilities.has("feedparser") and looks_like_feed(url, body):
                    try:
                        entries = await asyncio.to_thread(feed_entries, body)
                        if entries.strip():
                            return entries, "RSS feed"
                    except Exception:
                        pass
                if capabilities.has("trafilatura"):
                    try:
                        article = await asyncio.to_thread(extract_article, body, url)
                        if article.strip():
                            return article, "trafilatura"
                    except Exception:
                        pass

            try:
                um_url = f"https://urltomarkdown.herokuapp.com/?{urllib.parse.urlencode({'url': url, 'title': 'true', 'links': 'true'})}"
                if is_safe_url(um_url):
                    response = await client.get(um_url, headers=headers, follow_redirects=True)
                    if response.status_code < 400 and not _bad_reader_response(response.text[:2000]):
                        return response.text[:MAX_READ_BYTES], "urltomarkdown"
            except httpx.HTTPError:
                pass

            try:
                jina_url = f"https://r.jina.ai/{url}"
                if is_safe_url(jina_url):
                    response = await client.get(jina_url, headers=headers, follow_redirects=True)
                    if response.status_code < 400 and not _bad_reader_response(response.text[:2000]):
                        return response.text[:MAX_READ_BYTES], "Jina Reader"
            except httpx.HTTPError:
                pass

        stripped = await asyncio.to_thread(strip_html, body) if body else ""
        if stripped:
            return stripped, "direct HTML fallback"
        return None, ""


class ModelsPane(ToolPane):
    """Live model catalog with persistent keyboard and mouse role assignment."""

    META = ToolMeta(
        key="models",
        title="Models",
        purpose="Pick the model behind every tool once; every pane follows the route.",
        request_estimate="0",
        group="Core",
        action="Reload catalog",
        input_label="Catalog filter",
        output_label="Role routes",
        examples=(
            ("GPT family", "gpt"),
            ("Fast models", "flash"),
            ("Image-capable", "image"),
        ),
        avoid="temporary per-request model selection; assignments change persistent routing",
    )
    EXAMPLE_SELECTOR = "#model-filter"
    RESULT_SELECTOR = "#models-table"
    BINDINGS = [
        Binding("d", "assign('default')", "→ default"),
        Binding("v", "assign('vision')", "→ vision"),
        Binding("o", "assign('ocr')", "→ ocr"),
        Binding("p", "assign('power')", "→ power"),
        Binding("i", "assign('image')", "→ image"),
        Binding("k", "ping_health", "Ping Health"),
        Binding("r", "reload", "Reload"),
    ]

    DEFAULT_CSS = """
    ModelsPane { height: 100%; }
    ModelsPane .core-flow { height: 2; padding: 0 1; color: $text-muted; }
    ModelsPane #models-status { height: 1; padding: 0 1; }
    ModelsPane #models-spinner { height: 1; display: none; }
    ModelsPane #models-spinner.busy { display: block; }
    ModelsPane #models-layout { height: 1fr; margin: 0 1; }
    ModelsPane #models-catalog-pane { width: 1fr; margin-right: 1; }
    ModelsPane #model-filter-row { height: 3; }
    ModelsPane #model-filter { width: 1fr; }
    ModelsPane #models-reload { width: 10; margin-left: 1; }
    ModelsPane #model-catalog { height: 1fr; border: solid $primary; }
    ModelsPane #models-routes-pane { width: 2fr; max-width: 58; }
    ModelsPane #models-routes-title, ModelsPane #models-keys { height: 2; padding: 0 1; }
    ModelsPane #models-table { height: 10; }
    ModelsPane #models-assignments { height: 3; }
    ModelsPane #models-assignments Button { width: 1fr; margin-right: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._models: list[str] = []
        self._visible_models: list[str] = []
        self._ping_results: dict[str, dict] = {}
    def compose_body(self) -> ComposeResult:
        yield Static("Loading live catalog…", id="models-status")
        yield LoadingIndicator(id="models-spinner")
        yield Static("", id="models-detail")
        with Horizontal(id="models-layout"):
            with Vertical(id="models-catalog-pane"):
                yield Label("LIVE CATALOG · type to filter", classes="models-band")
                with Horizontal(id="model-filter-row"):
                    yield Input(placeholder="Filter model IDs…", id="model-filter")
                    yield Button("Ping", id="models-ping")
                    yield Button("Reload", id="models-reload")
                    yield Button("Credit costs", id="models-import")
                yield OptionList(Option("loading catalog…", disabled=True), id="model-catalog")
                yield Static("[b]CURRENT RUNTIME ROUTES[/b]", id="models-routes-title")
                yield DataTable(id="models-table", cursor_type="none")
                yield Static(
                    "Highlight a catalog model, then click a role or press d / v / p / i.",
                    id="models-keys",
                )
                with Horizontal(id="models-assignments"):
                    for role in ("default", "vision", "ocr", "power", "image"):
                        yield Button(
                            f"{ASSIGNMENT_KEYS[role]}  → {role}", id=f"models-assign-{role}"
                        )

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("ROUTE", "PERSISTED MODEL")
        self._refresh_routes()
        self.run_primary()

    def _refresh_routes(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.clear()
        for role in ROLE_KEYS:
            table.add_row(role, self.model_for(role))

    def _populate(self, needle: str = "") -> None:
        catalog = self.query_one("#model-catalog", OptionList)
        catalog.clear_options()
        folded = needle.casefold()
        self._visible_models = [model for model in self._models if folded in model.casefold()]
        if self._visible_models:
            options = []
            for model in self._visible_models:
                p = self._ping_results.get(model)
                if p:
                    if p["status"] == "ONLINE":
                        lat = f"{p['latency_ms']}ms" if p.get("latency_ms") else ""
                        label = f"[green]✔ ONLINE {lat:6}[/green]  {model}"
                    elif p["status"] == "TIMEOUT":
                        label = f"[yellow]⏱ TIMEOUT    [/yellow]  {model}"
                    else:
                        err = p.get("error") or "DOWN"
                        label = f"[red]✖ {err:11}[/red]  {model}"
                else:
                    label = f"[dim]-- PROBE UNKNOWN[/dim]  {model}"
                options.append(Option(label, id=model))
            catalog.add_options(options)
            catalog.highlighted = 0
        else:
            catalog.add_option(Option("(no matches)", disabled=True))
        costed = sum(1 for model in self._visible_models if weight_for(model) is not None)
        age = import_age_days()
        if not costed:
            detail = " · ⚠ no credit costs imported"
        elif costed < len(self._visible_models):
            detail = f" · ⚠ {costed}/{len(self._visible_models)} with credit costs"
        elif age is None:
            detail = " · ⚠ costs imported, date unknown"
        elif age >= 14:
            detail = f" · ⚠ costs are {age} days old — re-import"
        elif age == 0:
            detail = " · credit costs updated today"
        else:
            detail = f" · credit costs updated {age} day{'s' if age != 1 else ''} ago"
        self.query_one("#models-status", Static).update(
            f"Catalog · {len(self._visible_models)} shown / {len(self._models)} total{detail}"
        )

    def _model_detail(self, model: str) -> str:
        """Health status first, then credit cost, context, and capabilities."""
        bits = []
        p = self._ping_results.get(model)
        if p:
            if p["status"] == "ONLINE":
                bits.append(f"[green]ONLINE ({p['latency_ms']}ms)[/green]")
            elif p["status"] == "TIMEOUT":
                bits.append("[yellow]TIMEOUT[/yellow]")
            else:
                bits.append(f"[red]UNAVAILABLE ({p.get('error', 'DOWN')})[/red]")
        weight = weight_for(model)
        if weight is None:
            bits.append("cost not imported")
        elif weight == 0:
            bits.append("Free")
        else:
            unit = "credit" if weight == 1 else "credits"
            bits.append(f"{weight:g} {unit}/req")
        context = self.api.model_context.get(model)
        if context:
            bits.append(f"{format_context(context)} context")
        meta = model_meta(model)
        if meta and meta["vision"]:
            bits.append("vision")
        return f"[b]{model}[/b] · {' · '.join(bits)}"
    @on(Input.Changed, "#model-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        if self._models:
            self._populate(event.value)

    @on(Input.Submitted, "#model-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one("#model-catalog", OptionList).focus()

    @on(OptionList.OptionHighlighted, "#model-catalog")
    def _catalog_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        model = self._highlighted_model()
        self.query_one("#models-detail", Static).update(
            self._model_detail(model) if model else ""
        )

    @on(Button.Pressed, "#models-ping")
    def action_ping_health(self) -> None:
        if not self._visible_models:
            self.notify("No visible models to ping", severity="warning")
            return
        status = self.query_one("#models-status", Static)
        status.update(f"Pinging {len(self._visible_models)} models…")
        self.run_worker(self._ping_worker(list(self._visible_models)), thread=False)

    async def _ping_worker(self, models: list[str]) -> None:
        try:
            results = await self.api.ping_all_models(models)
            for r in results:
                self._ping_results[r["model"]] = r
            online = sum(1 for r in results if r["status"] == "ONLINE")
            unavail = sum(1 for r in results if r["status"] != "ONLINE")
            filter_val = self.query_one("#model-filter", Input).value
            self._populate(filter_val)
            self.query_one("#models-status", Static).update(
                f"Health Probe Complete · [green]{online} Online[/green] · [red]{unavail} Unavailable/Timeout[/red]"
            )
            self.notify(f"Ping complete: {online} online, {unavail} unavailable")
        except Exception as err:
            self.notify(f"Ping failed: {err}", severity="error")
    @on(Button.Pressed, "#models-reload")
    def _reload_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_primary()

    @on(Button.Pressed, "#models-import")
    def _import_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.push_screen(
            WeightsImportScreen(
                api=self.api,
                catalog=list(self._models),
                api_context=dict(self.api.model_context),
            ),
            lambda count: self._populate(
                self.query_one("#model-filter", Input).value
            ),
        )

    @on(Button.Pressed, "#models-assignments Button")
    def _assignment_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        role = event.button.id.removeprefix("models-assign-")
        self.action_assign(role)

    def _highlighted_model(self) -> str | None:
        highlighted = self.query_one("#model-catalog", OptionList).highlighted
        if highlighted is None or highlighted >= len(self._visible_models):
            return None
        return self._visible_models[highlighted]

    def run_primary(self) -> None:
        self._load_models()

    def action_reload(self) -> None:
        self.run_primary()

    def action_assign(self, role: str) -> None:
        model = self._highlighted_model()
        if model is None:
            self.notify("Highlight a catalog model first", severity="warning")
            return
        try:
            self.cfg.set_model(role, model)
        except (OSError, KeyError, ValueError) as exc:
            self.notify(f"Could not assign {role}: {exc}", severity="error")
            self.app.record_activity("models", "error", f"assign {role}: {exc}", model)
            return
        self._refresh_routes()
        self.query_one("#models-status", Static).update(
            f"Assigned · {escape(role)} → {escape(model)} · saved to config"
        )
        self.notify(f"{role} → {model}")
        self.app.record_activity("models", "ok", f"assigned {role}", model)

    @work(exclusive=True, group="model-catalog")
    async def _load_models(self) -> None:
        spinner = self.query_one("#models-spinner", LoadingIndicator)
        status = self.query_one("#models-status", Static)
        catalog = self.query_one("#model-catalog", OptionList)
        spinner.add_class("busy")
        status.update("Loading live catalog…")
        catalog.clear_options()
        catalog.add_option(Option("loading catalog…", disabled=True))
        try:
            try:
                models = await self.api.models()
            except (ApiError, httpx.HTTPError) as exc:
                status.update(f"Catalog failed · {escape(str(exc))}")
                catalog.clear_options()
                catalog.add_option(Option("catalog unavailable", disabled=True))
                self.notify(f"models: {exc}", severity="error")
                self.app.record_activity("models", "error", str(exc))
                return
            self._models = models
            self._populate(self.query_one("#model-filter", Input).value)
            self._refresh_routes()
            self.app.record_activity("models", "ok", f"loaded {len(models)} models")
        finally:
            spinner.remove_class("busy")
