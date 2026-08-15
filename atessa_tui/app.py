"""Atessa production workbench. Entry point: ``atessa``."""
from __future__ import annotations

from datetime import datetime
from rich.markup import escape

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Select,
    Static,
    TextArea,
)

from textual.theme import Theme

from .api import AtessaAPI
from .config import Config, ROLE_KEYS
from .spend import record_call, today_totals
from .screens import NAV_GROUPS, PANE_BY_KEY, PANES, TOOL_GLYPHS
from . import themes as theme_palette
from .screens.base import CapabilityStrip, GuideModal, ToolMeta, ToolPane
from .screens.unlock import UnlockScreen


def _system_clipboard(text: str) -> bool:
    """Copy through the OS clipboard tool. Textual's OSC 52 is ignored by many
    Windows terminals, so drive clip.exe / pbcopy / wl-copy / xclip directly."""
    import shutil
    import subprocess
    import sys

    if sys.platform == "win32":
        candidates = [["clip.exe"], ["clip"]]
    elif sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
        if shutil.which("clip.exe"):  # WSL
            candidates.insert(0, ["clip.exe"])
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(
                cmd, input=text.encode("utf-8"), check=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


SHORTCUTS = {
    "chat": "1",
    "search": "2",
    "read": "3",
    "image": "4",
    "view": "5",
    "shot": "S",
    "council": "6",
    "bench": "7",
    "arena": "8",
    "explain": "9",
    "git": "G",
    "shell": "D",
    "models": "M",
    "activity": "A",
}


class ToolItem(ListItem):
    """A semantic, keyboard-selectable navigation item."""

    def __init__(self, meta: ToolMeta, group: str) -> None:
        super().__init__(id=f"nav-{meta.key}")
        self.meta = meta
        self.group = group

    @property
    def searchable_text(self) -> str:
        meta = self.meta
        return " ".join(
            (
                meta.key,
                meta.title,
                meta.purpose,
                self.group,
                meta.action,
                meta.input_label,
                meta.output_label,
            )
        ).casefold()

    def compose(self) -> ComposeResult:
        yield Label(
            f"{TOOL_GLYPHS.get(self.meta.key, '◆')}  {self.meta.title}",
            classes="nav-title",
        )
        yield Label(SHORTCUTS[self.meta.key], classes="nav-key")

class ThemeSelect(Select[str]):
    """Theme list whose arrow keys change the palette immediately."""

    async def _on_key(self, event) -> None:
        if event.key == "down":
            event.stop()
            event.prevent_default()
            self.app.action_cycle_theme()
            return
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.app.action_previous_theme()
            return
        await super()._on_key(event)


class AtessaApp(App):
    """Persistent API-backed workbench containing all fourteen tools."""

    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = True

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Tools"),
        Binding("ctrl+i", "toggle_inspector", "Inspector"),
        Binding("alt+left", "focus_sidebar", "Focus tools", show=False),
        Binding("alt+right", "focus_workbench", "Focus workbench", show=False),
        Binding("f6", "toggle_focus_region", "Tools / workbench"),
        Binding("ctrl+enter", "run_tool", "Run"),
        Binding("ctrl+j", "next_tool", "Next", show=False),
        Binding("ctrl+k", "previous_tool", "Previous", show=False),
        Binding("?", "guide", "Guide"),
        Binding("ctrl+c", "copy_result", "Copy result", show=False, priority=True),
        Binding("/", "focus_filter", "Filter", show=False),
        Binding("1", "shortcut('1')", "Chat", show=False, priority=True),
        Binding("2", "shortcut('2')", "Search", show=False, priority=True),
        Binding("3", "shortcut('3')", "Read", show=False, priority=True),
        Binding("4", "open_tool('image')", "Image", show=False),
        Binding("5", "open_tool('view')", "Vision", show=False),
        Binding("s", "open_tool('shot')", "Shot", show=False),
        Binding("6", "open_tool('council')", "Council", show=False),
        Binding("7", "open_tool('bench')", "Benchmark", show=False),
        Binding("8", "open_tool('arena')", "Arena", show=False),
        Binding("9", "open_tool('explain')", "Explain", show=False),
        Binding("g", "open_tool('git')", "Git", show=False),
        Binding("d", "open_tool('shell')", "Command", show=False),
        Binding("m", "open_tool('models')", "Models"),
        Binding("a", "open_tool('activity')", "Activity", show=False),
        Binding("u", "unlock", "Unlock"),
        Binding("ctrl+t", "cycle_theme", "Theme"),
        Binding("ctrl+q", "quit", "Quit"),
    ]
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config()
        self.api = AtessaAPI(self.cfg)
        self.activity: list[dict[str, str]] = []
        self.active_tool = "chat"
        self.sidebar_visible = True
        self.inspector_visible = True
        self._api_closed = False
        self._keys = [pane.META.key for pane in PANES]
        self._theme_index = 0
        self._register_palettes()
        self.theme = theme_palette.PALETTE_ORDER[0]
    def compose(self) -> ComposeResult:
        with Horizontal(id="app-shell"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="Filter tools  /", id="tool-filter")
                with ListView(id="tool-list"):
                    for group, keys in NAV_GROUPS:
                        slug = group.casefold()
                        yield ListItem(
                            Label(group.upper()),
                            id=f"nav-section-{slug}",
                            classes="nav-section",
                            disabled=True,
                        )
                        for key in keys:
                            yield ToolItem(PANE_BY_KEY[key].META, group)
                yield Static(
                    f"[b]API[/b]  [#63d7c7]● {self.cfg.base_url.removeprefix('https://')}[/]\n"
                    "[dim]Config-routed runtime models[/dim]",
                    id="api-strip",
                )
            with Vertical(id="main-column"):
                with Horizontal(id="topbar"):
                    yield Static("◆", id="tool-mark")
                    with Vertical(id="page-copy"):
                        yield Label("ATESSA WORKBENCH", id="breadcrumb")
                        yield Label("Chat", id="page-title")
                    guide_button = Button("? Guide", id="guide-button")
                    guide_button.can_focus = False
                    yield guide_button
                    command_button = Button("Commands  ^P", id="command-button")
                    command_button.can_focus = False
                    yield command_button
                    yield ThemeSelect(
                        [(name, name) for name in theme_palette.PALETTE_ORDER],
                        value=theme_palette.PALETTE_ORDER[0],
                        allow_blank=False,
                        id="theme-select",
                    )
                    yield Static("● READY", id="ready-badge")
                    run_button = Button("Send  ^↵", id="run-button", variant="primary")
                    run_button.can_focus = False
                    yield run_button

                with Horizontal(id="workspace-row"):
                    with ContentSwitcher(initial="pane-chat", id="workspace"):
                        for pane_cls in PANES:
                            yield pane_cls()
                    with Vertical(id="inspector"):
                        with Horizontal(id="inspector-head"):
                            yield Label("INSPECTOR", classes="pane-title")
                            close_inspector = Button("×", id="close-inspector")
                            close_inspector.can_focus = False
                            yield close_inspector
                        yield Label("ACTIVE TOOL", classes="field-label")
                        yield Static("Chat", id="context-title")
                        yield Markdown("", id="context-help")
                        yield Static("", id="context-flow")
                        yield Label("RUNTIME ROUTES", classes="field-label")
                        inspector_roles = DataTable(id="insp-roles", cursor_type="none")
                        inspector_roles.can_focus = False
                        yield inspector_roles
                        yield Label("RECENT ACTIVITY", id="insp-activity-title", classes="field-label")

                with Horizontal(id="statusbar"):
                    yield Static("NORMAL", id="mode-indicator")
                    yield Static(self.cfg.base_url, id="endpoint-status", classes="status-item")
                    yield Static("Chat ready", id="last-activity", classes="status-item")
                    yield Static("", id="spend-status", classes="status-item")
                    yield Static("^c copy  ·  ? guide", classes="status-item")

    def on_mount(self) -> None:
        table = self.query_one("#insp-roles", DataTable)
        table.add_columns("ROUTE", "MODEL")
        self.refresh_roles()
        self.refresh_spend()
        self._select_nav(self.active_tool)
        self._update_context(self.active_tool)
        self._apply_breakpoint(self.size.width, self.size.height)
        if not self.cfg.api_key:
            self.notify("No API key found in ~/.atessa/config", severity="error", timeout=10)

    @on(ListView.Selected, "#tool-list")
    def select_tool(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ToolItem):
            self.show_tool(event.item.meta.key)

    @on(Input.Changed, "#tool-filter")
    def filter_tools(self, event: Input.Changed) -> None:
        needle = event.value.casefold().strip()
        visible_groups: set[str] = set()
        for item in self.query(ToolItem):
            item.display = not needle or needle in item.searchable_text
            if item.display:
                visible_groups.add(item.group.casefold())
        for group, _keys in NAV_GROUPS:
            self.query_one(f"#nav-section-{group.casefold()}", ListItem).display = (
                group.casefold() in visible_groups
            )

    @on(Button.Pressed, "#guide-button")
    def guide_clicked(self) -> None:
        self.action_guide()

    @on(Button.Pressed, "#command-button")
    def commands_clicked(self) -> None:
        self.action_command_palette()

    @on(Button.Pressed, "#run-button")
    def run_clicked(self) -> None:
        self.action_run_tool()

    @on(Button.Pressed, "#close-inspector")
    def close_inspector(self) -> None:
        self.action_toggle_inspector()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Seed every CSS variable app.tcss references.

        This is the base palette; each registered Theme overrides the subset it
        defines, so switching themes re-resolves every $variable live.
        """
        return dict(theme_palette.DEFAULT_VARIABLES)

    def _register_palettes(self) -> None:
        for palette in theme_palette.PALETTES:
            self.register_theme(
                Theme(
                    palette.name,
                    primary=palette.primary,
                    background=palette.background,
                    foreground=palette.foreground,
                    surface=palette.surface,
                    panel=palette.panel,
                    accent=palette.accent,
                    variables=dict(palette.variables),
                )
            )

    def action_cycle_theme(self) -> None:
        """Advance the live theme list and synchronize the selector."""
        names = theme_palette.PALETTE_ORDER
        if not names:
            return
        try:
            current = names.index(self.theme)
        except ValueError:
            current = self._theme_index
        self._theme_index = (current + 1) % len(names)
        self._set_theme(names[self._theme_index])

    def _set_theme(self, name: str) -> None:
        if name not in theme_palette.PALETTE_ORDER:
            return
        self._theme_index = theme_palette.PALETTE_ORDER.index(name)
        self.theme = name
        if self.is_mounted:
            selector = self.query_one("#theme-select", Select)
            if selector.value != name:
                selector.value = name
        self.notify(f"Theme: {name}")

    def action_previous_theme(self) -> None:
        names = theme_palette.PALETTE_ORDER
        if not names:
            return
        self._theme_index = (self._theme_index - 1) % len(names)
        self._set_theme(names[self._theme_index])

    @on(Select.Changed, "#theme-select")
    def theme_selected(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self._set_theme(str(event.value))

    @property
    def active_pane(self) -> ToolPane:
        return self.query_one(f"#pane-{self.active_tool}", ToolPane)

    def show_tool(self, key: str) -> None:
        if key not in PANE_BY_KEY:
            return
        self.active_tool = key
        self.query_one("#workspace", ContentSwitcher).current = f"pane-{key}"
        self._select_nav(key)
        self._update_context(key)

    def _select_nav(self, key: str) -> None:
        listing = self.query_one("#tool-list", ListView)
        for index, child in enumerate(listing.children):
            if isinstance(child, ToolItem) and child.meta.key == key:
                listing.index = index
                break

    def _update_context(self, key: str) -> None:
        meta = PANE_BY_KEY[key].META
        self.query_one("#tool-mark", Static).update(TOOL_GLYPHS.get(key, "◆"))
        self.query_one("#breadcrumb", Label).update("ATESSA WORKBENCH")
        self.query_one("#page-title", Label).update(meta.title)
        self.query_one("#run-button", Button).label = f"{meta.action}  ^↵"
        self.query_one("#context-title", Static).update(meta.title)
        route = meta.role or "tool-selected"
        model = self.cfg.model_for(meta.role) if meta.role else "varies by selection"
        self.query_one("#context-help", Markdown).update(
            f"{meta.purpose}\n\n**Route:** `{route}` → `{model}`"
        )
        self.query_one("#context-flow", Static).update(
            f"INPUT  {meta.input_label}\n→ OUTPUT  {meta.output_label}"
        )
        self.query_one("#last-activity", Static).update(f"{meta.title} ready")

    def action_open_tool(self, key: str) -> None:
        self.show_tool(key)
    def action_shortcut(self, key: str) -> None:
        """Use 1/2/3 for Models tabs when Models is active; otherwise open tools."""
        if self.active_tool == "models" and key in {"1", "2", "3"}:
            tab_id = {"1": "tab-catalog", "2": "tab-ping", "3": "tab-routes"}[key]
            self.query_one("#pane-models").query_one("TabbedContent").active = tab_id
            return
        tool_key = {"1": "chat", "2": "search", "3": "read"}.get(key)
        if tool_key:
            self.show_tool(tool_key)

    def action_focus_filter(self) -> None:
        filter_input = self.query_one("#tool-filter", Input)
        filter_input.focus()
        filter_input.cursor_position = len(filter_input.value)

    def action_focus_sidebar(self) -> None:
        """Move directly to tool navigation, without tabbing through chrome."""
        self.query_one("#tool-list", ListView).focus()

    def action_focus_workbench(self) -> None:
        """Move directly to the current tool's primary input or control."""
        pane = self.active_pane
        if pane.EXAMPLE_SELECTOR:
            try:
                pane.query_one(pane.EXAMPLE_SELECTOR).focus()
                return
            except Exception:
                pass
        for widget in pane.query("Input, TextArea, Select, OptionList, SelectionList, DataTable, Button"):
            if widget.can_focus and not widget.disabled:
                widget.focus()
                return

    def action_toggle_focus_region(self) -> None:
        focused = self.focused
        if focused is not None and self.query_one("#sidebar") in focused.ancestors:
            self.action_focus_workbench()
        else:
            self.action_focus_sidebar()

    def _tool_index(self) -> int:
        return self._keys.index(self.active_tool)

    def action_next_tool(self) -> None:
        self.show_tool(self._keys[(self._tool_index() + 1) % len(self._keys)])

    def action_previous_tool(self) -> None:
        self.show_tool(self._keys[(self._tool_index() - 1) % len(self._keys)])

    def action_guide(self) -> None:
        pane = self.active_pane
        self.push_screen(GuideModal(pane.META), self._load_example)

    def action_copy_result(self) -> None:
        """Ctrl+C: copy the focused selection, else the active pane's result."""
        text = self._focused_selection()
        if not text:
            text = self.active_pane.result_text()
        if not text.strip():
            self.notify(
                "Nothing to copy yet. To quit, type CTRL-Q", severity="warning"
            )
            return
        self.copy_to_clipboard(text)
        if not _system_clipboard(text):
            self.notify(
                "Copied via terminal escape only; if your terminal ignores it, "
                "select with the mouse instead. To quit, type CTRL-Q",
                severity="warning",
            )
            return
        self.notify("Text copied. To quit, type CTRL-Q")

    def _focused_selection(self) -> str:
        """Selected text in the focused input, so Ctrl+C keeps editing behavior."""
        widget = self.focused
        if widget is None or not isinstance(widget, (Input, TextArea)):
            return ""
        try:
            return widget.selected_text or ""
        except Exception:
            return ""

    def action_unlock(self) -> None:
        self.push_screen(UnlockScreen(), lambda _: self._refresh_capability_strips())

    def _refresh_capability_strips(self) -> None:
        for strip in self.query(CapabilityStrip):
            strip.refresh_capabilities()

    def _load_example(self, value: str | None) -> None:
        if value:
            self.active_pane.load_example(value)
            self.notify("Example loaded; edit it or run the tool", title=self.active_pane.META.title)

    def action_run_tool(self) -> None:
        badge = self.query_one("#ready-badge", Static)
        badge.update("● RUNNING")
        badge.add_class("running")
        self.query_one("#last-activity", Static).update(
            f"Running {self.active_pane.META.title}…"
        )
        self.active_pane.run_primary()
        self.set_timer(0.8, self._restore_ready_badge)

    def _restore_ready_badge(self) -> None:
        try:
            badge = self.query_one("#ready-badge", Static)
        except Exception:
            return
        badge.update("● READY")
        badge.remove_class("running")

    def action_toggle_sidebar(self) -> None:
        self.sidebar_visible = not self.sidebar_visible
        self.query_one("#sidebar").set_class(not self.sidebar_visible, "hidden-panel")

    def action_toggle_inspector(self) -> None:
        self.inspector_visible = not self.inspector_visible
        self.query_one("#inspector").set_class(not self.inspector_visible, "hidden-panel")

    def record_activity(
        self,
        tool: str,
        status: str,
        detail: str,
        model: str = "",
        credits: float | None = None,
        count_spend: bool = True,
    ) -> None:
        # Ordinary panes make one request per event. Fan-out panes pre-count
        # each model request and pass count_spend=False with the combined total.
        cost = record_call(tool, model) if count_spend and model else credits
        record = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tool": str(tool),
            "status": str(status),
            "detail": str(detail),
            "model": str(model),
            "credits": cost,
        }
        self.activity.insert(0, record)
        del self.activity[200:]
        lines = [
            f"[dim]{escape(item['time'])}[/dim] [b]{escape(item['tool'])}[/b] "
            f"{escape(item['status'])} {escape(item['detail'][:100])}"
            for item in self.activity[:8]
        ]
        try:
            self.query_one("#insp-activity", Static).update("\n".join(lines))
            self.query_one("#last-activity", Static).update(
                f"{record['time']} · {record['tool']} · {record['status']}"
            )
            self._restore_ready_badge()
            self.refresh_spend()
            activity_pane = self.query_one("#pane-activity", ToolPane)
            refresh = getattr(activity_pane, "refresh_activity", None)
            if callable(refresh):
                refresh()
            if tool == "models" and status == "ok":
                self.refresh_roles()
        except Exception:
            pass

    def refresh_spend(self) -> None:
        """Keep the status bar showing what today has cost."""
        totals = today_totals()
        requests = totals.get("requests", 0)
        if not requests:
            text = ""
        else:
            credits = totals.get("credits", 0.0)
            plural = "API call" if requests == 1 else "API calls"
            text = f"today: {requests} {plural} · {credits:g} quota credits"
            if totals.get("unpriced"):
                text += f" ({totals['unpriced']} unpriced)"
        try:
            self.query_one("#spend-status", Static).update(text)
        except Exception:
            pass

    def refresh_roles(self) -> None:
        try:
            table = self.query_one("#insp-roles", DataTable)
        except Exception:
            return
        table.clear()
        for role in ROLE_KEYS:
            table.add_row(role, self.cfg.model_for(role))
        if self.is_mounted and self.active_tool in PANE_BY_KEY:
            self._update_context(self.active_tool)
            for pane in self.query(ToolPane):
                refresh = getattr(pane, "refresh_routes", None)
                if callable(refresh):
                    refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_breakpoint(event.size.width, event.size.height)

    def _apply_breakpoint(self, width: int, height: int) -> None:
        medium = width < 120
        compact = width < 88
        self.set_class(medium, "-medium")
        self.set_class(compact, "-compact")
        self.set_class(height < 36, "-short")
        self.screen.set_class(medium, "narrow")
        self.screen.set_class(compact, "tiny")

    async def on_unmount(self) -> None:
        if not self._api_closed:
            self._api_closed = True
            await self.api.aclose()


def main() -> None:
    AtessaApp().run()


if __name__ == "__main__":
    main()
