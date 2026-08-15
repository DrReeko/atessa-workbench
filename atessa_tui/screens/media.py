"""Media panes: image generation, local image vision, screenshot + describe.

ImagePane   — prompt -> PNG via the proxy's /responses image_generation tool.
              Aspect is steered via a prompt suffix (proxy ignores size params).
VisionPane  — local image path + question -> vision-model answer (proxy cannot
              fetch remote URLs; images go up as base64 data URLs).
ShotPane    — cross-platform screen capture (list-args subprocess, never
              shell=True) followed by an automatic vision description.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, LoadingIndicator, Log, Markdown, Select

from .. import capabilities
from ..api import ApiError
from ..config import OCR_FALLBACKS
from ..weights import weight_for
from .base import ToolMeta, ToolPane

ASPECT_SUFFIX = {
    "square": "",
    "landscape": ". Composition: a wide 16:9 cinematic landscape orientation.",
    "portrait": ". Composition: a tall 9:16 vertical portrait orientation.",
}

VIEW_DEFAULT_PROMPT = "Describe this image in detail. Transcribe any visible text verbatim."
SHOT_DEFAULT_PROMPT = "Describe this screenshot in detail. Transcribe any visible text verbatim."
OCR_PROMPT = (
    "Transcribe every piece of text in this image verbatim, preserving reading "
    "order and line breaks. Output only the transcribed text, no commentary."
)

_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "Add-Type -AssemblyName System.Drawing; "
    "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
    "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
    "$g = [System.Drawing.Graphics]::FromImage($bmp); "
    "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
    "$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); "
    "$g.Dispose(); $bmp.Dispose()"
)

LINUX_HINTS = "install one of: grim (wayland), imagemagick, spectacle, gnome-screenshot"


def _capture_cmds(path: str) -> list[list[str]]:
    """Return screenshot backends in preference order for this platform."""
    if sys.platform == "win32" or platform.system() == "Windows":
        script_path = Path(path).as_posix().replace("'", "''")
        return [[
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _PS_SCRIPT.format(path=script_path),
        ]]
    if platform.system() == "Darwin":
        return [["screencapture", "-x", path]] if shutil.which("screencapture") else []
    return [
        cmd
        for cmd in (
            ["grim", path],
            ["import", "-window", "root", path],
            ["spectacle", "-b", "-n", "-o", path],
            ["gnome-screenshot", "-f", path],
        )
        if shutil.which(cmd[0])
    ]

def _capture_missing_message() -> str:
    """Explain the platform-specific screenshot prerequisite."""
    system = platform.system()
    if sys.platform == "win32" or system == "Windows":
        return "No screenshot backend found - Windows PowerShell is required"
    if system == "Darwin":
        return "No screenshot backend found - macOS screencapture is required"
    return f"No screenshot backend found - {LINUX_HINTS}"


class ImagePane(ToolPane):
    """Generate a PNG from a prompt; quality + aspect options."""

    META = ToolMeta(
        key="image",
        title="Image",
        purpose="One sentence to a finished PNG on disk, shaped for where you'll use it.",
        request_estimate="1",
        group="Media",
        role="image",
        action="Generate",
        input_label="Prompt",
        output_label="PNG on disk",
        flow="Describe an image → AI generates it → save the PNG",
        examples=(
            ("Cozy cabin", "a cozy log cabin in a snowy forest at dusk, warm windows"),
            ("Logo sketch", "minimalist geometric fox logo, flat vector style"),
            ("Cityscape", "rainy neon cyberpunk street market, cinematic lighting"),
        ),
        avoid="editing existing images; exact pixel sizes (proxy ignores size params)",
    )
    EXAMPLE_SELECTOR = "#image-prompt"
    RESULT_SELECTOR = "#image-result"

    DEFAULT_CSS = """
    ImagePane #image-spinner { display: none; }
    ImagePane #image-spinner.busy { display: block; }
    """

    def compose_body(self) -> ComposeResult:
        yield Label("VISUAL BRIEF", classes="field-label")
        with Horizontal(classes="input-row"):
            yield Input(
                placeholder="Describe the image to generate",
                id="image-prompt",
            )
            yield Button("Generate", variant="primary", id="image-generate")
        yield Label("OUTPUT OPTIONS", classes="field-label")
        with Horizontal(id="image-opts", classes="input-row"):
            yield Select(
                [
                    ("Quality · High", "high"),
                    ("Quality · Medium", "medium"),
                    ("Quality · Low", "low"),
                    ("Quality · Auto", "auto"),
                ],
                value="high",
                allow_blank=False,
                id="image-quality",
            )
            yield Select(
                [
                    ("Aspect · Square", "square"),
                    ("Aspect · Landscape", "landscape"),
                    ("Aspect · Portrait", "portrait"),
                ],
                value="square",
                allow_blank=False,
                id="image-aspect",
            )
        yield LoadingIndicator(id="image-spinner")
        yield Label("ARTIFACT LOG", classes="field-label")
        yield Log(id="image-result")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "image-prompt":
            event.stop()
            self.run_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "image-generate":
            event.stop()
            self.run_primary()

    def _set_busy(self, busy: bool) -> None:
        spinner = self.query_one("#image-spinner", LoadingIndicator)
        spinner.set_class(busy, "busy")
        self.query_one("#image-generate", Button).disabled = busy

    def run_primary(self) -> None:
        spinner = self.query_one("#image-spinner", LoadingIndicator)
        if spinner.has_class("busy"):
            return
        prompt = self.query_one("#image-prompt", Input).value.strip()
        if not prompt:
            self.notify("Enter a visual brief first", severity="warning")
            return
        quality = str(self.query_one("#image-quality", Select).value)
        aspect = str(self.query_one("#image-aspect", Select).value)
        self._set_busy(True)
        self._generate(prompt, quality, aspect)

    @work(exclusive=True, group="image")
    async def _generate(self, prompt: str, quality: str, aspect: str) -> None:
        log = self.query_one("#image-result", Log)
        model = self.model_for("image")
        request_prompt = prompt + ASPECT_SUFFIX.get(aspect, "")
        try:
            try:
                png = await self.api.image_gen(
                    request_prompt,
                    quality=quality,
                    model=model,
                )
                if not png:
                    raise ApiError("image tool returned an empty artifact")
                out = Path.cwd() / f"atessa-image-{time.time_ns()}.png"
                out.write_bytes(png)
            except (ApiError, OSError, ValueError) as error:
                log.write_line(f"error: {error}")
                self.record("error", str(error), model)
                self.notify(str(error), severity="error", timeout=10)
                return
            log.write_line(f"saved: {out} ({len(png)} bytes)")
            log.write_line(f"options: {quality} quality · {aspect}")
            log.write_line(f"prompt: {prompt}")
            log.write_line("")
            self.notify(f"Saved {out.name}")
            self.record("ok", f"{out.name} ({len(png)} bytes)", model)
        finally:
            self._set_busy(False)


class VisionPane(ToolPane):
    """Describe / OCR a local image with the vision-role model."""

    META = ToolMeta(
        key="view",
        title="View",
        purpose="Read the text and meaning inside any image on disk.",
        request_estimate="1",
        group="Media",
        role="vision",
        action="Describe",
        input_label="Image path and question",
        output_label="Description or OCR",
        flow="Choose an image → ask a question → AI answers from that file",
        examples=(
            ("Full OCR", "Transcribe every piece of visible text verbatim, preserving layout."),
            ("Describe", VIEW_DEFAULT_PROMPT),
            ("Extract data", "Extract any tables or structured data as Markdown."),
        ),
        avoid="remote URLs (proxy cannot fetch them; local files only)",
    )
    EXAMPLE_SELECTOR = "#view-question"
    RESULT_SELECTOR = "#view-answer"

    DEFAULT_CSS = """
    VisionPane #view-spinner { display: none; }
    VisionPane #view-spinner.busy { display: block; }
    """
    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0


    def compose_body(self) -> ComposeResult:
        yield Label("LOCAL IMAGE", classes="field-label")
        yield Input(placeholder="Path to a local image", id="view-path")
        yield Label("QUESTION / OCR INSTRUCTION", classes="field-label")
        with Horizontal(classes="input-row"):
            yield Input(
                value=VIEW_DEFAULT_PROMPT,
                placeholder="What should the vision model inspect?",
                id="view-question",
            )
            yield Button("Describe", variant="primary", id="view-describe")
            yield Button("Read text", id="view-ocr")
        yield Label(
            "LOCAL ONLY · image bytes are sent as base64; remote URLs are not fetched",
            id="view-note",
        )
        yield LoadingIndicator(id="view-spinner")
        yield Label("VISUAL FINDINGS", classes="field-label")
        with VerticalScroll(id="view-out"):
            yield Markdown("", id="view-answer")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("view-path", "view-question"):
            event.stop()
            self.run_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "view-describe":
            event.stop()
            self.run_primary()
        elif event.button.id == "view-ocr":
            event.stop()
            self.run_ocr()

    def _set_busy(self, busy: bool) -> None:
        spinner = self.query_one("#view-spinner", LoadingIndicator)
        spinner.set_class(busy, "busy")
        self.query_one("#view-describe", Button).disabled = busy
        self.query_one("#view-ocr", Button).disabled = busy

    def run_primary(self) -> None:
        spinner = self.query_one("#view-spinner", LoadingIndicator)
        if spinner.has_class("busy"):
            return
        path = self.query_one("#view-path", Input).value.strip()
        if not path:
            self.notify("Enter an image path first", severity="warning")
            return
        image = Path(path).expanduser().resolve()
        if not image.is_file():
            message = f"File not found: {image}"
            self.notify(message, severity="error")
            self.record("error", message, self.model_for("vision"))
            return
        try:
            size = image.stat().st_size
        except OSError as error:
            self.notify(str(error), severity="error")
            self.record("error", str(error), self.model_for("vision"))
            return
        if size > 20 * 1024 * 1024:
            self.notify(f"File too large ({size / (1024*1024):.1f}MB, max 20MB)", severity="error")
            return
        question = self.query_one("#view-question", Input).value.strip() or VIEW_DEFAULT_PROMPT
        self.query_one("#view-note", Label).update(
            f"FILE  {image} · {size} bytes · sent as base64"
        )
        self.query_one("#view-answer", Markdown).update("")
        self._run_id += 1
        self._set_busy(True)
        self._describe(str(image), question, image.name, self._run_id)

    @work(exclusive=True, group="view")
    async def _describe(self, path: str, question: str, name: str, run_id: int) -> None:
        answer_view = self.query_one("#view-answer", Markdown)
        model = self.model_for("vision")
        try:
            if self._run_id != run_id:
                return
            try:
                answer = await self.api.vision(path, question, model=model)
                if not answer.strip():
                    raise ApiError("vision model returned an empty response")
            except (ApiError, OSError, ValueError) as error:
                if self._run_id == run_id:
                    answer_view.update(f"**error:** {error}")
                    self.record("error", str(error), model)
                    self.notify(str(error), severity="error", timeout=10)
                return
            if self._run_id == run_id:
                answer_view.update(answer)
                self.record("ok", name, model)
        finally:
            if self._run_id == run_id:
                self._set_busy(False)

    def run_ocr(self) -> None:
        spinner = self.query_one("#view-spinner", LoadingIndicator)
        if spinner.has_class("busy"):
            return
        path = self.query_one("#view-path", Input).value.strip()
        if not path:
            self.notify("Enter an image path first", severity="warning")
            return
        image = Path(path).expanduser().resolve()
        if not image.is_file():
            self.notify(f"File not found: {image}", severity="error")
            return
        try:
            size = image.stat().st_size
        except OSError as error:
            self.notify(str(error), severity="error")
            return
        if size > 20 * 1024 * 1024:
            self.notify(f"File too large ({size / (1024*1024):.1f}MB, max 20MB)", severity="error")
            return
        self.query_one("#view-answer", Markdown).update("")
        self._run_id += 1
        self._set_busy(True)
        self._read_text(str(image), image.name, self._run_id)

    @work(exclusive=True, group="view")
    async def _read_text(self, path: str, name: str, run_id: int) -> None:
        """Transcribe with the free OCR model, falling back if it is unavailable."""
        answer_view = self.query_one("#view-answer", Markdown)
        chosen = self.model_for("ocr")
        chain = [chosen] + [m for m in OCR_FALLBACKS if m != chosen]
        last_error: Exception | None = None
        try:
            for model in chain:
                if self._run_id != run_id:
                    return
                answer_view.update(f"_reading text with {model}…_")
                try:
                    answer = await self.api.vision(path, OCR_PROMPT, model=model)
                except (ApiError, OSError, ValueError) as error:
                    last_error = error
                    continue
                if not answer.strip():
                    last_error = ApiError("empty response")
                    continue
                if self._run_id != run_id:
                    return
                cost = weight_for(model)
                tag = "free" if cost == 0 else f"{cost:g} credits" if cost else "cost unknown"
                answer_view.update(f"**Text read by {model}** _({tag})_\n\n{answer}")
                self.record("ok", f"read text · {name}", model)
                return
            if self._run_id == run_id:
                answer_view.update(f"**error:** {last_error}")
                self.record("error", f"read text: {last_error}", chosen)
                self.notify(str(last_error), severity="error", timeout=10)
        finally:
            if self._run_id == run_id:
                self._set_busy(False)

class ShotPane(ToolPane):
    """Capture the screen, then auto-describe it with the vision model."""

    META = ToolMeta(
        key="shot",
        title="Shot",
        purpose="Ask a question about everything visible across your connected monitors.",
        request_estimate="1",
        group="Media",
        role="vision",
        action="Capture",
        input_label="Question about every monitor",
        output_label="AI answer from the desktop image",
        flow="Ask a question → capture every connected monitor → AI answers from the image",
        examples=(
            ("Describe", SHOT_DEFAULT_PROMPT),
            ("Read error", "Find any error message on screen and explain what it means."),
            ("Summarize", "Summarize what application is open and what it is showing."),
        ),
        avoid="window-only capture (full virtual screen only)",
    )
    EXAMPLE_SELECTOR = "#shot-question"
    RESULT_SELECTOR = "#shot-answer"

    DEFAULT_CSS = """
    ShotPane #shot-spinner { display: none; }
    ShotPane #shot-spinner.busy { display: block; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._run_id = 0
    def compose_body(self) -> ComposeResult:
        yield Label("SCREEN QUESTION", classes="field-label")
        with Horizontal(classes="input-row"):
            yield Input(
                value=SHOT_DEFAULT_PROMPT,
                placeholder="What should the vision model inspect?",
                id="shot-question",
            )
            yield Button("Capture", variant="primary", id="shot-capture")
        yield Label(
            "CAPTURE  every connected monitor · temporary PNG used for the AI answer",
            id="shot-path",
        )
        yield LoadingIndicator(id="shot-spinner")
        yield Label("SCREEN FINDINGS", classes="field-label")
        with VerticalScroll(id="shot-out"):
            yield Markdown("", id="shot-answer")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "shot-question":
            event.stop()
            self.run_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "shot-capture":
            event.stop()
            self.run_primary()

    def run_primary(self) -> None:
        spinner = self.query_one("#shot-spinner", LoadingIndicator)
        if spinner.has_class("busy"):
            return
        self.query_one("#shot-answer", Markdown).update("")
        self._run_id += 1
        self._set_busy(True)
        self._capture(self._run_id)

    def _fail(self, message: str, run_id: int | None = None) -> None:
        if run_id is not None and self._run_id != run_id:
            return
        self.query_one("#shot-answer", Markdown).update(f"**error:** {message}")
        self.notify(message, severity="error", timeout=10)
        self.record("error", message, self.model_for("vision"))
        self._set_busy(False)

    def _capture_with_mss(self, path: str) -> str:
        """Capture every monitor in-process. Returns an error string on failure."""
        try:
            import mss
            import mss.tools

            factory = getattr(mss, "MSS", None) or mss.mss
            with factory() as sct:
                shot = sct.grab(sct.monitors[0])
                mss.tools.to_png(shot.rgb, shot.size, output=path)
        except Exception as error:
            return f"mss: {error}"
        return ""

    @work(thread=True, exclusive=True, group="shot-capture")
    def _capture(self, run_id: int) -> None:
        artifact = Path(tempfile.gettempdir()) / f"atessa-shot-{time.time_ns()}.png"
        failures: list[str] = []

        if capabilities.has("mss"):
            error = self._capture_with_mss(str(artifact))
            if not error and artifact.is_file() and artifact.stat().st_size > 0:
                if self._run_id != run_id:
                    artifact.unlink(missing_ok=True)
                    return
                self.app.call_from_thread(self._on_captured, str(artifact), run_id)
                return
            failures.append(error or "mss: produced no image")

        commands = _capture_cmds(str(artifact))
        if not commands and not failures:
            self.app.call_from_thread(self._fail, _capture_missing_message(), run_id)
            return
        for cmd in commands:
            artifact.unlink(missing_ok=True)
            if self._run_id != run_id:
                return
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(f"{cmd[0]}: {error}")
                continue
            if proc.returncode == 0 and artifact.is_file() and artifact.stat().st_size > 0:
                if self._run_id != run_id:
                    artifact.unlink(missing_ok=True)
                    return
                self.app.call_from_thread(self._on_captured, str(artifact), run_id)
                return
            detail = (proc.stderr or proc.stdout or "no output file").strip()[:200]
            failures.append(f"{cmd[0]}: {detail}")
        artifact.unlink(missing_ok=True)
        if self._run_id == run_id:
            self.app.call_from_thread(self._fail, f"Capture failed: {'; '.join(failures)}", run_id)

    def _on_captured(self, path: str, run_id: int) -> None:
        if self._run_id != run_id:
            Path(path).unlink(missing_ok=True)
            return
        artifact = Path(path)
        try:
            size = artifact.stat().st_size
        except OSError as error:
            artifact.unlink(missing_ok=True)
            self._fail(f"Capture failed: {error}", run_id)
            return
        self.query_one("#shot-path", Label).update(
            f"CAPTURE  {artifact} · {size} bytes"
        )
        question = self.query_one("#shot-question", Input).value.strip() or SHOT_DEFAULT_PROMPT
        self._describe(path, question, run_id)

    @work(exclusive=True, group="shot-describe")
    async def _describe(self, path: str, question: str, run_id: int) -> None:
        answer_view = self.query_one("#shot-answer", Markdown)
        model = self.model_for("vision")
        try:
            if self._run_id != run_id:
                return
            try:
                answer = await self.api.vision(path, question, model=model)
                if not answer.strip():
                    raise ApiError("vision model returned an empty response")
            except (ApiError, OSError, ValueError) as error:
                if self._run_id == run_id:
                    answer_view.update(f"**error:** {error}")
                    self.record("error", str(error), model)
                    self.notify(str(error), severity="error", timeout=10)
                return
            if self._run_id == run_id:
                answer_view.update(answer)
                self.record("ok", path, model)
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as error:
                self.record("error", f"screenshot cleanup: {error}", model)
            finally:
                if self._run_id == run_id:
                    self._set_busy(False)
