# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller spec for the Atessa Toolbelt native bundle.

One dispatcher executable is built once and emitted under all 18 console names.
That keeps the frozen Python runtime shared and avoids re-analyzing the complete
dependency graph for every alias.  The TUI remains a console application.
"""

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis


VERSION = "0.3.0b1"
EXECUTABLES = [
    ("atessa", "atessa_tui.app", "main"),
    ("atessa-activity", "atessa_tui.cli", "activity_entry"),
    ("atessa-arena", "atessa_tui.cli", "arena_entry"),
    ("atessa-bench", "atessa_tui.cli", "bench_entry"),
    ("atessa-chat", "atessa_tui.cli", "chat_entry"),
    ("atessa-council", "atessa_tui.cli", "council_entry"),
    ("atessa-explain", "atessa_tui.cli", "explain_entry"),
    ("atessa-ghsearch", "atessa_tui.cli", "ghsearch_entry"),
    ("atessa-git", "atessa_tui.cli", "git_entry"),
    ("atessa-image", "atessa_tui.cli", "image_entry"),
    ("atessa-models", "atessa_tui.cli", "models_entry"),
    ("atessa-ping", "atessa_tui.cli", "ping_entry"),
    ("atessa-read", "atessa_tui.cli", "read_entry"),
    ("atessa-search", "atessa_tui.cli", "search_entry"),
    ("atessa-shell", "atessa_tui.cli", "shell_entry"),
    ("atessa-shot", "atessa_tui.cli", "shot_entry"),
    ("atessa-view", "atessa_tui.cli", "view_entry"),
    ("websearch", "atessa_tui.cli", "websearch_entry"),
]

entry_dir = Path("build/entry_points")
entry_dir.mkdir(parents=True, exist_ok=True)
entry_path = entry_dir / "atessa_dispatch.py"
entry_path.write_text(
    "from importlib import import_module\n"
    "from pathlib import Path\n"
    "import sys\n\n"
    f"ROUTES = {[(name, module, function) for name, module, function in EXECUTABLES]!r}\n\n"
    "def main():\n"
    "    name = Path(sys.argv[0]).stem.casefold()\n"
    "    for alias, module_name, function_name in ROUTES:\n"
    "        if name == alias:\n"
    "            getattr(import_module(module_name), function_name)()\n"
    "            return\n"
    "    raise SystemExit(f'unknown Atessa command: {name}')\n\n"
    "if __name__ == '__main__':\n"
    "    main()\n",
    encoding="utf-8",
)

datas = [("atessa_tui/*.tcss", "atessa_tui")]
textual_widgets = [
    "textual.widgets._button",
    "textual.widgets._checkbox",
    "textual.widgets._collapsible",
    "textual.widgets._content_switcher",
    "textual.widgets._data_table",
    "textual.widgets._input",
    "textual.widgets._label",
    "textual.widgets._list_item",
    "textual.widgets._list_view",
    "textual.widgets._loading_indicator",
    "textual.widgets._log",
    "textual.widgets._markdown",
    "textual.widgets._option_list",
    "textual.widgets._rich_log",
    "textual.widgets._select",
    "textual.widgets._selection_list",
    "textual.widgets._static",
    "textual.widgets._tabbed_content",
    "textual.widgets._tabs",
    "textual.widgets._text_area",
    "textual.widgets._tab_pane",
    "textual.widgets._tree",
]
hiddenimports = [
    "atessa_tui",
    "atessa_tui.app",
    "atessa_tui.cli",
    "atessa_tui.api",
    "atessa_tui.config",
    "atessa_tui.sources",
    "atessa_tui.spend",
    "atessa_tui.weights",
    "atessa_tui.capabilities",
    "atessa_tui.metering",
    "atessa_tui.themes",
    "atessa_tui.screens.core",
    "atessa_tui.screens.media",
    "atessa_tui.screens.compare",
    "atessa_tui.screens.dev",
    "atessa_tui.screens.base",
    "atessa_tui.screens.importer",
    "atessa_tui.screens.unlock",
    *textual_widgets,
]

# These are optional accounting/test helpers.  Excluding them prevents their
# optional scientific stacks from expanding a terminal bundle by hundreds of MB.
excludes = [
    "litellm",
    "tiktoken",
    "pytest",
    "_pytest",
    "torch",
    "transformers",
    "tensorflow",
    "onnxruntime",
    "cv2",
    "numpy",
    "scipy",
    "sympy",
]

analysis = Analysis(
    [str(entry_path)],
    pathex=[str(Path.cwd())],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exes = [
    EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    for name, _, _ in EXECUTABLES
]

collect_args = []
for exe in exes:
    collect_args.extend([exe, analysis.binaries, analysis.zipfiles, analysis.datas])

COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="atessa",
)
