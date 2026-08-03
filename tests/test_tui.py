from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from textual.widgets import Checkbox, ContentSwitcher, Input, Select, TextArea

from atessa_tui.app import AtessaApp
from atessa_tui.screens import PANES
from atessa_tui.screens.compare import ModelPicker


def value_of(widget):
    if isinstance(widget, Input):
        return widget.value
    if isinstance(widget, TextArea):
        return widget.text
    raise TypeError(type(widget))


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

        picker = app.query_one("#council-models", ModelPicker)
        await picker.set_models(["alpha", "bravo", "charlie"], ["bravo"])
        assert picker.selected == ("bravo",)
        picker.query_one("#model-choice-0", Checkbox).toggle()
        await pilot.pause()
        assert picker.selected == ("alpha", "bravo")


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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(test_desktop_interaction(Path(tmp)))
        asyncio.run(test_responsive_layout(Path(tmp)))
    print("ALL TUI TESTS OK")


if __name__ == "__main__":
    main()
