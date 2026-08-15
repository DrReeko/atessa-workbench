from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from textual.widgets import ContentSwitcher, Input, OptionList, Select, TextArea

from atessa_tui import weights
from atessa_tui.app import AtessaApp
from atessa_tui.screens import PANES
from atessa_tui.screens.compare import ModelPickerScreen
from atessa_tui.screens.importer import WeightsImportScreen


def value_of(widget):
    if isinstance(widget, Input):
        return widget.value
    if isinstance(widget, TextArea):
        return widget.text
    raise TypeError(type(widget))
async def test_pasted_cost_import_completes(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"ATESSA_HOME": str(tmp_path)}, clear=False):
        weights.refresh()
        app = AtessaApp()
        async with app.run_test(size=(150, 46)) as pilot:
            app.push_screen(
                WeightsImportScreen(
                    api=None,
                    catalog=["gpt-4", "free-model-1"],
                    api_context={"gpt-4": 1_000_000, "free-model-1": 400_000},
                )
            )
            await pilot.pause()
            app.screen.query_one("#import-paste", TextArea).text = (
                "gpt-4 ×2× 1M\nfree-model-1 Free 400K"
            )
            await pilot.click("#import-go")
            await pilot.pause()
            assert weights.load_weights() == {"free-model-1": 0.0, "gpt-4": 2.0}




async def test_desktop_interaction(tmp_path: Path) -> None:
    app = AtessaApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        assert len(PANES) == 14
        assert len({pane.META.key for pane in PANES}) == 14
        switcher = app.query_one("#workspace", ContentSwitcher)
        for pane_cls in PANES:
            key = pane_cls.META.key
            app.show_tool(key)
            await pilot.pause()
            assert switcher.current == f"pane-{key}"
            assert app.active_pane.META.key == key

            meta = pane_cls.META
            if meta.examples:
                first_label, first_text = meta.examples[0]
                app.active_pane.load_example(first_text)
                await pilot.pause()
                target_selector = getattr(pane_cls, "EXAMPLE_SELECTOR", None)
                if target_selector:
                    w = app.active_pane.query_one(target_selector)
                    assert value_of(w) == first_text

            renders_dir = tmp_path / "renders"
            renders_dir.mkdir(exist_ok=True)
            shot = renders_dir / f"production-{key}.svg"
            app.save_screenshot(shot)
            assert shot.exists() and shot.stat().st_size > 8000

        app.show_tool("chat")
        await pilot.press("ctrl+j")
        assert app.active_pane.META.key == "search"
        await pilot.press("ctrl+k")
        assert app.active_pane.META.key == "chat"
        assert not app.query_one("#inspector").has_class("hidden-panel")
        await pilot.press("ctrl+i")
        assert app.query_one("#inspector").has_class("hidden-panel")
        await pilot.press("ctrl+i")
        assert not app.query_one("#inspector").has_class("hidden-panel")

        app.show_tool("image")
        await pilot.pause()
        for selector in ("#image-quality", "#image-aspect"):
            select = app.query_one(selector, Select)
            assert select.value is not Select.NULL
            label = select.query_one("#label")
            assert str(label.render()).strip(), selector

        app.show_tool("council")
        await pilot.pause()
        pane = app.query_one("#pane-council")
        pane._models = ["alpha", "bravo", "charlie"]
        pane._set_selected(["bravo"])
        assert pane._selected == ["bravo"]
        await pilot.click("#council-choose")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        listing = screen.query_one("#picker-list", OptionList)
        assert listing.option_count == 3
        rows = [str(listing.get_option_at_index(i).prompt) for i in range(3)]
        assert any("☑" in row and "bravo" in row for row in rows)
        listing.focus()
        await pilot.press("space")
        assert pane._selected == ["bravo"]
        await pilot.click("#picker-done")
        await pilot.pause()
        assert pane._selected == ["alpha", "bravo"]
        await pilot.click("#council-choose-judge")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        listing = screen.query_one("#picker-list", OptionList)
        assert listing.option_count == 3
        listing.focus()
        await pilot.press("space")  # pick alpha
        await pilot.press("down")
        await pilot.press("space")  # pick bravo — single-select replaces
        await pilot.pause()
        assert screen._selected == {"bravo"}
        await pilot.click("#picker-done")
        await pilot.pause()
        assert pane._judge == "bravo"
        assert "Judge: bravo" in str(pane.query_one("#council-judge").content)


async def test_responsive_layout(tmp_path: Path) -> None:
    for size, expected in [((100, 36), "narrow"), ((58, 30), "tiny")]:
        app = AtessaApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.screen.has_class(expected)
            app.show_tool("chat")
            await pilot.pause()
            renders_dir = tmp_path / "renders"
            renders_dir.mkdir(exist_ok=True)
            shot = renders_dir / f"production-{expected}-chat.svg"
            app.save_screenshot(shot)
            assert shot.exists() and shot.stat().st_size > 5000



async def test_theme_cycle_switches_palette(tmp_path: Path) -> None:
    from atessa_tui import themes as theme_palette
    from textual.widgets import Select

    app = AtessaApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        selector = app.query_one("#theme-select", Select)
        assert app.theme == theme_palette.PALETTE_ORDER[0]
        assert selector.value == theme_palette.PALETTE_ORDER[0]
        canvas_start = app.query_one("#workspace").styles.background
        selector.focus()
        await pilot.press("down")
        await pilot.pause()
        assert app.theme == theme_palette.PALETTE_ORDER[1]
        assert selector.value == theme_palette.PALETTE_ORDER[1]
        assert app.query_one("#workspace").styles.background != canvas_start

        await pilot.press("up")
        await pilot.pause()
        assert app.theme == theme_palette.PALETTE_ORDER[0]

        app.action_cycle_theme()
        await pilot.pause()
        assert app.theme == theme_palette.PALETTE_ORDER[1]

        for _ in range(len(theme_palette.PALETTE_ORDER) - 1):
            app.action_cycle_theme()
            await pilot.pause()
        assert app.theme == theme_palette.PALETTE_ORDER[0]

async def test_models_catalog_renders_live_claude_ids(tmp_path: Path) -> None:
    app = AtessaApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        app.show_tool("models")
        await pilot.pause()
        pane = app.query_one("#pane-models")
        pane._models = ["claude-sonnet-4.6", "claude-opus-4.6", "gpt-5.6-luna"]
        pane.api.model_context = {model: 100_000 for model in pane._models}
        pane._populate("claude")
        assert pane._visible_models == ["claude-sonnet-4.6", "claude-opus-4.6"]


async def test_ctrl_c_copies_with_quit_hint(tmp_path: Path) -> None:
    app = AtessaApp()
    async with app.run_test(size=(150, 46)) as pilot:
        app.show_tool("shell")
        await pilot.pause()
        pane = app.query_one("#pane-shell")
        pane._set_command("echo hello")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app._notifications
        assert "Text copied. To quit, type CTRL-Q" in list(app._notifications)[-1].message
def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_desktop_interaction(Path(tmp)))
        asyncio.run(test_responsive_layout(Path(tmp)))
        asyncio.run(test_pasted_cost_import_completes(Path(tmp)))
        asyncio.run(test_models_catalog_renders_live_claude_ids(Path(tmp)))
        asyncio.run(test_ctrl_c_copies_with_quit_hint(Path(tmp)))
    print("ALL TUI TESTS OK")


if __name__ == "__main__":
    main()
