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
    TabbedContent,
    TabPane,
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


class ModelsFilterInput(Input):
    """Filter model names without consuming the Models tab number keys."""

    async def _on_key(self, event) -> None:
        await super()._on_key(event)

    def check_consume_key(self, key: str, character: str | None) -> bool:
        if key in {"1", "2", "3"}:
            return False
        return super().check_consume_key(key, character)

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
        purpose="Chat with the selected model and keep the thread together.",
        request_estimate="1 per message",
        group="Core",
        role="default",
        action="Send",
        input_label="Message",
        output_label="Reply in this thread",
        flow="Write a message → AI replies in the same conversation",
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
        purpose="Search developer sources for fixes, examples, and current discussions.",
        request_estimate="0",
        group="Core",
        action="Search",
        input_label="Question or error",
        output_label="Linked results with a short answer",
        flow="Ask a question → search sources → AI summarizes the useful results",
        examples=(
            ("Reddit fix", "latest real-world fix for a Python Textual focus bug"),
            ("YouTube walkthrough", "recent video walkthrough fixing Windows Python packaging"),
            ("GitHub issue", "find the current workaround for a breaking library regression"),
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
        purpose="Turn a URL, video, or feed into readable text.",
        request_estimate="0",
        group="Core",
        action="Fetch",
        input_label="URL, video, or feed",
        output_label="Readable Markdown",
        flow="Paste a URL → fetch it → get readable text",
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
    """Live model catalog, health probing, and role routing organized into 3 clean tabs."""

    META = ToolMeta(
        key="models",
        title="Models",
        purpose="See the live model list, check availability, and choose each role’s model.",
        request_estimate="0",
        group="Core",
        action="Reload catalog",
        input_label="Filter model names",
        output_label="Models, health, and role settings",
        flow="Choose a tab → inspect models → set a role if needed",
        examples=(
            ("GPT models", "gpt"),
            ("Claude models", "claude"),
            ("Fast models", "flash"),
        ),
        avoid="changing a role when you only want a one-off model comparison",
    )
    EXAMPLE_SELECTOR = "#model-filter"
    RESULT_SELECTOR = "#models-catalog-table"
    BINDINGS = [
        Binding("r", "reload", "Reload"),
        Binding("k", "ping_health", "Probe Health"),
    ]

    DEFAULT_CSS = """
    ModelsPane { height: 100%; }
    ModelsPane .core-flow { height: 2; padding: 0 1; color: $text-muted; }
    ModelsPane #models-status { height: 1; padding: 0 1; }
    ModelsPane #models-spinner { height: 1; display: none; }
    ModelsPane #models-spinner.busy { display: block; }
    ModelsPane TabbedContent { height: 1fr; margin: 0 1; }
    ModelsPane TabPane { padding: 1; }
    ModelsPane #model-filter-row { height: 3; }
    ModelsPane #model-filter { width: 1fr; }
    ModelsPane #models-catalog-table { height: 1fr; border: solid $primary; }
    ModelsPane #models-ping-table { height: 1fr; border: solid $primary; }
    ModelsPane #models-routes-table { height: 1fr; border: solid $primary; }
    ModelsPane .ping-bar { height: 3; }
    ModelsPane .route-assign-row { height: 3; margin-top: 1; }
    ModelsPane .route-assign-row Button { width: 1fr; margin-right: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._models: list[str] = []
        self._visible_models: list[str] = []
        self._ping_results: dict[str, dict] = {}
    def compose_body(self) -> ComposeResult:
        yield Static("Loading live catalog…", id="models-status")
        yield LoadingIndicator(id="models-spinner")
        with TabbedContent(initial="tab-catalog"):
            with TabPane("1. Catalog & Costs", id="tab-catalog"):
                with Horizontal(id="model-filter-row"):
                    yield ModelsFilterInput(placeholder="Type to filter model names (e.g. gpt, kimi, flash)…", id="model-filter")
                    yield Button("Credit costs", id="models-import")
                    yield Button("Reload", id="models-reload")
                yield DataTable(id="models-catalog-table", cursor_type="row")

            with TabPane("2. Health & Uptime Probe", id="tab-ping"):
                with Horizontal(classes="ping-bar"):
                    yield Button("▶ Run Live Health Check Across All Models", id="models-ping", variant="primary")
                yield DataTable(id="models-ping-table", cursor_type="row")

            with TabPane("3. Role Routes Configuration", id="tab-routes"):
                yield Static("1/2/3 switch tabs here · choose a row, then set that role’s model.", classes="core-flow")
                yield Static("These 5 routes dictate which model powers each tool across the toolbelt:", classes="core-flow")
                yield DataTable(id="models-routes-table", cursor_type="row")
                yield Static("Select a row above, then click a button below to change that role's assigned model:", classes="core-flow")
                with Horizontal(classes="route-assign-row"):
                    for role in ("default", "vision", "ocr", "power", "image"):
                        yield Button(f"Set → {role.upper()}", id=f"models-assign-{role}")
    def _show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def on_mount(self) -> None:
        cat_table = self.query_one("#models-catalog-table", DataTable)
        cat_table.add_columns("MODEL NAME", "CREDIT COST", "CONTEXT SIZE", "CAPABILITIES")

        ping_table = self.query_one("#models-ping-table", DataTable)
        ping_table.add_columns("STATUS", "MODEL ID", "LATENCY", "DIAGNOSTIC NOTES")

        route_table = self.query_one("#models-routes-table", DataTable)
        route_table.add_columns("TOOL ROLE", "CONFIGURED MODEL", "PURPOSE")

        self._refresh_routes()
        self.run_primary()

    def _refresh_routes(self) -> None:
        table = self.query_one("#models-routes-table", DataTable)
        table.clear()
        purposes = {
            "default": "Primary fallback chat & fast search model",
            "vision": "Screenshot & image analysis model",
            "ocr": "Text extraction model",
            "power": "Complex coding & architecture review model",
            "image": "PNG image generation model",
        }
        for role in ROLE_KEYS:
            table.add_row(role.upper(), self.model_for(role), purposes.get(role, ""))

    def _populate(self, needle: str = "") -> None:
        table = self.query_one("#models-catalog-table", DataTable)
        table.clear()
        folded = needle.casefold()
        self._visible_models = [m for m in self._models if folded in m.casefold()]
        for model in self._visible_models:
            weight = weight_for(model)
            cost_str = "Free" if weight == 0 else (f"{weight:g} credits/req" if weight is not None else "cost not imported")
            ctx = format_context(self.api.model_context.get(model)) if self.api.model_context.get(model) else "-"
            meta = model_meta(model)
            caps = "Vision" if (meta and meta["vision"]) else "Text"
            table.add_row(model, cost_str, ctx, caps)

        costed = sum(1 for m in self._visible_models if weight_for(m) is not None)
        age = import_age_days()
        detail = f" · {costed}/{len(self._visible_models)} costs loaded" if costed else " · no credit costs loaded"
        self.query_one("#models-status", Static).update(
            f"Catalog · {len(self._visible_models)} shown / {len(self._models)} total{detail}"
        )
    @on(Input.Changed, "#model-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        if self._models:
            self._populate(event.value)
    @on(Button.Pressed, "#models-ping")
    def action_ping_health(self) -> None:
        if not self._visible_models:
            self.notify("No visible models to ping", severity="warning")
            return
        table = self.query_one("#models-ping-table", DataTable)
        table.clear()
        table.add_row("PROBING…", "Pinging models in parallel…", "-", "-")
        self.notify("Starting live health probe…")
        self.run_worker(self._ping_worker(list(self._visible_models)), thread=False)

    async def _ping_worker(self, models: list[str]) -> None:
        try:
            results = await self.api.ping_all_models(models)
            table = self.query_one("#models-ping-table", DataTable)
            table.clear()
            online = 0
            unavail = 0
            for r in results:
                self._ping_results[r["model"]] = r
                if r["status"] == "ONLINE":
                    online += 1
                    status_str = f"[green]ONLINE[/green]"
                    lat_str = f"{r['latency_ms']}ms"
                    notes = "OK"
                elif r["status"] == "TIMEOUT":
                    unavail += 1
                    status_str = "[yellow]TIMEOUT[/yellow]"
                    lat_str = f"{r['latency_ms']}ms"
                    notes = f"> 3.5s"
                else:
                    unavail += 1
                    status_str = "[red]UNAVAILABLE[/red]"
                    lat_str = f"{r['latency_ms']}ms" if r.get('latency_ms') else "-"
                    notes = r.get("error") or "HTTP Error"
                table.add_row(status_str, r["model"], lat_str, notes)

            self.query_one("#models-status", Static).update(
                f"Health Probe Complete · [green]{online} Online[/green] · [red]{unavail} Unavailable[/red]"
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

    @on(Button.Pressed, ".route-assign-row Button")
    def _assignment_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        role = event.button.id.removeprefix("models-assign-")
        cat_table = self.query_one("#models-catalog-table", DataTable)
        if cat_table.cursor_row is not None and cat_table.cursor_row < len(self._visible_models):
            model = self._visible_models[cat_table.cursor_row]
            self.action_assign_explicit(role, model)
        else:
            self.notify("Click/select a model row in Tab 1 (Catalog) first", severity="warning")

    def action_assign_explicit(self, role: str, model: str) -> None:
        try:
            self.cfg.set_model(role, model)
            self._refresh_routes()
            self.notify(f"Assigned {role.upper()} → {model}")
            self.app.record_activity("models", "ok", f"assigned {role}", model)
        except Exception as exc:
            self.notify(f"Assignment failed: {exc}", severity="error")
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
        spinner.add_class("busy")
        status.update("Loading live catalog…")
        try:
            try:
                models = await self.api.models()
            except (ApiError, httpx.HTTPError) as exc:
                status.update(f"Catalog failed · {escape(str(exc))}")
                self.notify(f"models: {exc}", severity="error")
                self.app.record_activity("models", "error", str(exc))
                return
            self._models = models
            if not self.is_mounted or not self.query("#models-catalog-table"):
                self.app.record_activity("models", "ok", f"loaded {len(models)} models")
                return
            self._populate(self.query_one("#model-filter", Input).value)
            self._refresh_routes()
            self.app.record_activity("models", "ok", f"loaded {len(models)} models")
        finally:
            spinner.remove_class("busy")
