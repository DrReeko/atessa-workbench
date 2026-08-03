"""Developer panes: error diagnosis, Git assistance, commands, and activity."""
from __future__ import annotations

import asyncio
import os
import platform
import re
import signal
import subprocess
import sys
from pathlib import Path
from rich.markup import escape

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    LoadingIndicator,
    Markdown,
    OptionList,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .. import capabilities
from ..api import ApiError
from ..spend import recent_days, today_totals, top_tools
from ..weights import format_credits
from .base import ToolMeta, ToolPane

MAX_DIFF = 12_000


def _kill_process_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

TRACEBACK_FRAME = re.compile(
    r'File "(?P<path>[^"]+)", line (?P<line>\d+)'
    r'|(?P<path2>[\w./\\:-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|rb)):(?P<line2>\d+)'
)
SOURCE_CONTEXT = 12
MAX_SOURCE_BYTES = 1_000_000
MAX_SOURCE_FRAMES = 3


def traceback_sources(text: str, root: Path | None = None) -> str:
    """Quote the real source around each file:line the excerpt names.

    Only files under the working directory are read, so a pasted traceback can
    never make the tool open something outside the project.
    """
    root = (root or Path.cwd()).resolve()
    seen: list[tuple[Path, int]] = []
    for match in TRACEBACK_FRAME.finditer(text):
        raw = match.group("path") or match.group("path2")
        number = match.group("line") or match.group("line2")
        if not raw or not number:
            continue
        try:
            path = Path(raw.strip())
            path = (path if path.is_absolute() else root / path).resolve()
        except (OSError, ValueError):
            continue
        if not path.is_file() or not path.is_relative_to(root):
            continue
        entry = (path, int(number))
        if entry not in seen:
            seen.append(entry)

    blocks: list[str] = []
    for path, number in seen[-MAX_SOURCE_FRAMES:]:
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        number = max(1, min(len(lines), number))
        start = max(1, min(len(lines), number - SOURCE_CONTEXT // 2))
        end = min(len(lines), start + SOURCE_CONTEXT)
        body = "\n".join(
            f"{'>' if index == number else ' '} {index:>5} | {lines[index - 1]}"
            for index in range(start, end + 1)
        )
        blocks.append(
            f"{path.relative_to(root).as_posix()} (line {number} marked with >):\n"
            f"```\n{body}\n```"
        )
    return "\n\n".join(blocks)


EXPLAIN_PROMPT = """You are a concise senior debugging assistant.
Diagnose the supplied compiler error, traceback, failed command, or log excerpt.
Use the environment context when commands or paths differ by OS. Respond in Markdown with:
1. **Meaning** — what failed.
2. **Likely cause** — the most probable root cause, noting uncertainty.
3. **Concrete fix** — exact commands or minimal code changes, ordered safest first.
4. **Verify** — one specific check proving the fix.
Do not invent files or facts not present in the excerpt. Warn before destructive steps.
When real source is supplied below, base the diagnosis on those exact lines and cite them.

Environment:
- OS: {os}
- Machine: {machine}
- Python: {python}
- Working directory: {cwd}

Error / log excerpt:
```text
{text}
```
{sources}"""

COMMIT_PROMPT = """Write a conventional-commit message for this staged Git diff.
The first line must be `type(scope): subject`, imperative, at most 72 characters.
Add a blank line and a short body explaining what changed and why when useful.
Output only the commit message: no fence or commentary.

{diff}"""

REVIEW_PROMPT = """Review this {source} Git diff for correctness, regressions, security,
and missing edge cases. Return concrete findings only. For every finding include severity
(blocker/major/minor/nit), file and changed-line location, impact, and a specific fix.
If no actionable defect is visible, say so briefly. Do not claim to have run the code.

```diff
{diff}
```"""

COMMAND_PROMPT = """Translate the request into EXACTLY ONE shell command for this environment.
OS: {os}
Shell used for execution: {shell}
Working directory: {cwd}
Output one single-line command only: no Markdown, backticks, explanation, prompt prefix,
or additional alternatives. Do not run it. Prefer a read-only command when that satisfies
the request. On Windows use cmd.exe syntax; invoke PowerShell explicitly if it is required.

Request: {request}"""

_ARTIFACT_RE = re.compile(
    r"(?i)(?:^|[\s:=])[^\n]*\.(?:png|jpe?g|webp|gif|svg|json|csv|md|txt|html|pdf|zip)(?:\b|$)"
)
_HIGH_RISK = re.compile(
    r"(?i)(?:\brm\b|\bunlink\b|\bshred\b|\brmdir\b|\bdel\b|"
    r"\bformat\b|\bmkfs\b|\bdiskpart\b|\bshutdown\b|\breboot\b|"
    r"remove-item\b|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|restore\b|checkout\s+--)|"
    r"drop\s+(?:database|table)|(?:curl|wget)[^\n]*\|\s*(?:sh|bash))"
)
_WRITE_RISK = re.compile(
    r"(?i)(?:\b(?:install|uninstall|delete|remove|move|copy|rename|write|set-content)\b|"
    r"\b(?:git\s+(?:commit|push|merge|rebase|checkout|switch|restore)|npm\s+(?:install|uninstall)|"
    r"pip\s+install|mkdir|touch|chmod|chown)\b|(?:^|\s)(?:>|>>)(?:\s|$))"
)


def _truncate(diff: str) -> str:
    if len(diff) <= MAX_DIFF:
        return diff
    return diff[:MAX_DIFF] + "\n... (diff truncated)"


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()
    return text.strip("`").strip()


def _single_command(text: str) -> str:
    command = _strip_fences(text)
    lines = [line.strip() for line in command.splitlines() if line.strip()]
    if len(lines) != 1 or "\x00" in command:
        raise ValueError("model did not return exactly one single-line command")
    return lines[0]


def _shell_context() -> str:
    if platform.system() == "Windows":
        return "cmd.exe (PowerShell must be invoked explicitly)"
    return "/bin/sh"


def _risk(command: str) -> str:
    if _HIGH_RISK.search(command):
        return "HIGH — destructive or difficult to reverse. Inspect every argument before Run."
    if _WRITE_RISK.search(command):
        return "ELEVATED — appears to change files, packages, Git state, or the system."
    return "LOWER — appears observational, but this is only a heuristic. Inspect before Run."


class ExplainPane(ToolPane):
    """Diagnose pasted failures with environment-aware, concrete fixes."""

    META = ToolMeta(
        key="explain",
        title="Explain",
        purpose="Fixes grounded in the source file your traceback names.",
        request_estimate="1",
        group="Develop",
        role="default",
        action="Diagnose",
        input_label="Error or traceback",
        output_label="Cause, fix, verify",
        examples=(
            ("Python import", "ModuleNotFoundError: No module named 'textual'"),
            ("TypeScript type", "TS2322: Type 'string | undefined' is not assignable to type 'string'."),
            ("Git repository", "fatal: not a git repository (or any parent up to mount point)"),
        ),
        avoid="pasting secrets or huge unfiltered logs; include the first causal error",
    )
    EXAMPLE_SELECTOR = "#explain-input"
    RESULT_SELECTOR = "#explain-answer"

    DEFAULT_CSS = """
    ExplainPane Label.field { height: 1; color: $text-muted; }
    ExplainPane TextArea#explain-input { height: 8; min-height: 4; }
    ExplainPane Horizontal#explain-actions { height: 3; }
    ExplainPane Button { width: auto; }
    ExplainPane LoadingIndicator { height: 1; display: none; }
    ExplainPane LoadingIndicator.busy { display: block; }
    ExplainPane VerticalScroll#explain-scroll {
        border: round $primary; height: auto; min-height: 5; max-height: 22;
    }
    """

    def compose_body(self) -> ComposeResult:
        yield Label("Error / log / traceback", classes="field")
        yield TextArea(
            id="explain-input",
            placeholder="Paste the error, log, or traceback here…",
        )
        with Horizontal(id="explain-actions"):
            yield Button("Diagnose", id="explain-run", variant="primary")
        yield LoadingIndicator(id="explain-spinner")
        with VerticalScroll(id="explain-scroll"):
            yield Markdown("", id="explain-answer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "explain-run":
            event.stop()
            self.run_primary()

    def run_primary(self) -> None:
        text = self.query_one("#explain-input", TextArea).text.strip()
        if not text:
            self.notify("Paste an error or log excerpt first", severity="warning")
            return
        self._diagnose(text)

    @work(exclusive=True, group="explain")
    async def _diagnose(self, text: str) -> None:
        answer = self.query_one("#explain-answer", Markdown)
        spinner = self.query_one("#explain-spinner", LoadingIndicator)
        model = self.model_for("default")
        spinner.add_class("busy")
        answer.update("_Diagnosing…_")
        sources = await asyncio.to_thread(traceback_sources, text)
        if sources:
            status = f"Reading source named in the traceback…\n\n{sources}"
            answer.update(f"_{status.splitlines()[0]}_")
        prompt = EXPLAIN_PROMPT.format(
            os=platform.platform(),
            machine=platform.machine() or "unknown",
            python=sys.version.split()[0],
            cwd=Path.cwd(),
            text=text,
            sources=(
                f"\nReal source from this machine:\n\n{sources}" if sources else ""
            ),
        )
        try:
            result = await self.api.chat(
                [{"role": "user", "content": prompt}], model=model
            )
        except Exception as error:
            answer.update(f"**Error:** {escape(str(error))}")
            self.record("error", str(error), model)
        else:
            answer.update(result or "_(empty response)_")
            detail = f"diagnosed {len(text)} characters"
            if sources:
                detail += " · grounded in local source"
            self.record("ok", detail, model)
        finally:
            spinner.remove_class("busy")


class GitPane(ToolPane):
    """Draft conventional commits and review real staged or unstaged diffs."""

    META = ToolMeta(
        key="git",
        title="Git",
        purpose="Reviews and commit messages grounded in your actual staged diff.",
        request_estimate="0 local · 1 for AI",
        group="Develop",
        role="power",
        action="Review diff",
        input_label="Staged diff",
        output_label="Commit + review",
        examples=(
            ("Feature commit", "feat(cli): add explicit command approval"),
            ("Bug-fix commit", "fix(git): preserve multiline commit bodies"),
            ("Documentation commit", "docs(guide): clarify destructive command risk"),
        ),
        avoid="treating a diff review as runtime verification; run the changed path separately",
    )
    EXAMPLE_SELECTOR = "#git-message"
    RESULT_SELECTOR = "#git-review-output, #git-result"

    DEFAULT_CSS = """
    GitPane Label.field { height: 1; color: $text-muted; }
    GitPane Horizontal.git-actions { height: 3; }
    GitPane Button { width: auto; }
    GitPane TextArea#git-message { height: 9; min-height: 4; }
    GitPane Static#git-result { height: auto; max-height: 6; color: $text-muted; }
    GitPane VerticalScroll#git-review-scroll {
        border: round $primary; height: auto; min-height: 6; max-height: 22;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._commit_warned = False
        self._run_id = 0
    def on_mount(self) -> None:
        self._fetch_repo_state()

    @work(thread=True, exclusive=True, group="git-repo-state")
    def _fetch_repo_state(self) -> None:
        state = self._repo_state()
        if state:
            self.app.call_from_thread(self._update_repo_state, state)

    def _update_repo_state(self, state: str) -> None:
        self.query_one("#git-repo-state", Static).update(state)

    @on(TextArea.Changed, "#git-message")
    def _on_message_changed(self) -> None:
        self._commit_warned = False

    def compose_body(self) -> ComposeResult:
        yield Static("", id="git-repo-state", classes="field")
        with TabbedContent(initial="git-review-tab"):
            with TabPane("Review", id="git-review-tab"):
                with Horizontal(classes="git-actions"):
                    yield Button("Review changes", id="git-review", variant="primary")
                yield Label("Uses staged diff; falls back to unstaged when staging is empty.", classes="field")
                with VerticalScroll(id="git-review-scroll"):
                    yield Markdown("", id="git-review-output")
            with TabPane("Commit", id="git-commit-tab"):
                with Horizontal(classes="git-actions"):
                    yield Button("Draft from staged", id="git-draft", variant="primary")
                    yield Button("Commit staged changes", id="git-commit", variant="success")
                yield Label("Review/edit this message. Commit occurs only when you click Commit staged changes.", classes="field")
                yield TextArea(
                    id="git-message",
                    placeholder="Commit message; generate a draft or write your own…",
                )
                yield Static("", id="git-result")

    def load_example(self, value: str) -> None:
        self.query_one(TabbedContent).active = "git-commit-tab"
        super().load_example(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "git-review":
            event.stop()
            self.run_primary()
        elif button_id == "git-draft":
            event.stop()
            self._load_staged_for_draft()
        elif button_id == "git-commit":
            event.stop()
            message = self.query_one("#git-message", TextArea).text.strip()
            if not message:
                self.notify("Enter or draft a commit message first", severity="warning")
                return
            problem = self._validate_commit_message(message)
            if problem and not self._commit_warned:
                self._commit_warned = True
                self.query_one("#git-result", Static).update(
                    f"{problem}  Click Commit again to use this message anyway."
                )
                self.notify(problem, severity="warning", timeout=8)
                return
            self._commit_warned = False
            self._commit(message)

    def run_primary(self) -> None:
        self._load_diff_for_review()

    def _repo_state(self) -> str:
        """Branch, divergence, and in-progress operation, when GitPython is present."""
        if not capabilities.has("gitpython"):
            return ""
        try:
            import git

            repo = git.Repo(Path.cwd(), search_parent_directories=True)
            branch = repo.active_branch.name if not repo.head.is_detached else "detached"
            bits = [f"branch {branch}"]
            tracking = (
                repo.active_branch.tracking_branch() if not repo.head.is_detached else None
            )
            if tracking is not None:
                ahead = sum(1 for _ in repo.iter_commits(f"{tracking.name}..HEAD"))
                behind = sum(1 for _ in repo.iter_commits(f"HEAD..{tracking.name}"))
                if ahead or behind:
                    bits.append(f"{ahead} ahead / {behind} behind {tracking.name}")
                else:
                    bits.append(f"in sync with {tracking.name}")
            git_dir = Path(repo.git_dir)
            if (git_dir / "MERGE_HEAD").exists():
                bits.append("MERGE in progress")
            if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
                bits.append("REBASE in progress")
            if repo.is_dirty(untracked_files=False):
                bits.append("uncommitted changes")
            return " · ".join(bits)
        except Exception:
            return ""

    def _validate_commit_message(self, message: str) -> str:
        """Return a problem description for a non-conventional message, else ''."""
        if not capabilities.has("commitizen"):
            return ""
        subject = next((line for line in message.strip().splitlines() if line.strip()), "")
        match = re.match(r"^(\w+)(\([^)]+\))?(!)?:\s+(.+)$", subject)
        if not match:
            return "Not a conventional commit — expected 'type(scope): summary'."
        kind, _, _, summary = match.groups()
        known = {
            "feat", "fix", "docs", "style", "refactor", "perf",
            "test", "build", "ci", "chore", "revert",
        }
        if kind not in known:
            return f"Unknown commit type '{kind}' — use one of: {', '.join(sorted(known))}."
        if len(subject) > 72:
            return f"Subject line is {len(subject)} characters; keep it under 72."
        if not summary.strip():
            return "Commit summary is empty."
        return ""

    def _git_diff(self, staged: bool) -> tuple[str | None, str]:
        command = ["git", "diff", "--staged"] if staged else ["git", "diff"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as error:
            return None, str(error)
        if result.returncode:
            return None, result.stderr.strip() or f"git exited {result.returncode}"
        return result.stdout, ""

    @work(thread=True, exclusive=True, group="git-draft-load")
    def _load_staged_for_draft(self) -> None:
        diff, error = self._git_diff(staged=True)
        if diff is None:
            self.app.call_from_thread(self._git_load_error, error)
        elif not diff.strip():
            self.app.call_from_thread(self._git_load_error, "nothing staged")
        else:
            self.app.call_from_thread(self._draft_from_diff, _truncate(diff))

    @work(thread=True, exclusive=True, group="git-review-load")
    def _load_diff_for_review(self) -> None:
        diff, error = self._git_diff(staged=True)
        source = "staged"
        if diff is None:
            self.app.call_from_thread(self._git_load_error, error)
            return
        if not diff.strip():
            diff, error = self._git_diff(staged=False)
            source = "unstaged"
        if diff is None:
            self.app.call_from_thread(self._git_load_error, error)
        elif not diff.strip():
            self.app.call_from_thread(self._git_load_error, "no staged or unstaged changes")
        else:
            self.app.call_from_thread(
                self._review_diff, _truncate(diff), source
            )

    def _git_load_error(self, detail: str) -> None:
        self.notify(detail, severity="warning" if "nothing" in detail or "no staged" in detail else "error")
        self.record("error", detail, self.model_for("power"))

    def _draft_from_diff(self, diff: str) -> None:
        self._draft_worker(diff)

    def _review_diff(self, diff: str, source: str) -> None:
        self._review_worker(diff, source)

    @work(exclusive=True, group="git-model")
    async def _draft_worker(self, diff: str) -> None:
        area = self.query_one("#git-message", TextArea)
        model = self.model_for("power")
        area.text = "Drafting…"
        try:
            message = await self.api.chat(
                [{"role": "user", "content": COMMIT_PROMPT.format(diff=diff)}],
                model=model,
            )
        except Exception as error:
            area.text = ""
            self.notify(f"Draft failed: {error}", severity="error")
            self.record("error", f"draft: {error}", model)
        else:
            area.text = _strip_fences(message)
            self.record("ok", "drafted conventional commit", model)

    @work(exclusive=True, group="git-model")
    async def _review_worker(self, diff: str, source: str) -> None:
        output = self.query_one("#git-review-output", Markdown)
        model = self.model_for("power")
        output.update(f"_Reviewing {source} changes…_")
        try:
            review = await self.api.chat(
                [
                    {
                        "role": "user",
                        "content": REVIEW_PROMPT.format(source=source, diff=diff),
                    }
                ],
                model=model,
            )
        except Exception as error:
            output.update(f"**Error:** {escape(str(error))}")
            self.record("error", f"review {source}: {error}", model)
        else:
            output.update(review or "_(empty response)_")
            self.record("ok", f"reviewed {source} diff", model)

    @work(thread=True, exclusive=True, group="git-commit")
    def _commit(self, message: str) -> None:
        try:
            result = subprocess.run(
                ["git", "commit", "-F", "-"],
                input=message,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + result.stderr).strip() or "(no output)"
            self.app.call_from_thread(
                self._commit_finished, result.returncode, output
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.app.call_from_thread(self._commit_finished, -1, str(error))

    def _commit_finished(self, returncode: int, output: str) -> None:
        self.query_one("#git-result", Static).update(output)
        if returncode == 0:
            self.notify("Committed staged changes")
            self.record("ok", "git commit completed")
        else:
            self.notify("Git commit failed", severity="error")
            self.record("error", output[:300])


class CommandPane(ToolPane):
    """Translate intent to one visible command; execute only after explicit Run."""

    META = ToolMeta(
        key="shell",
        title="Command",
        purpose="Describe the outcome; approve the exact command before anything runs.",
        request_estimate="1 draft · 0 run",
        group="Develop",
        role="default",
        action="Suggest command",
        input_label="Desired outcome",
        output_label="Command + log",
        examples=(
            ("Largest files", "Find the five largest files under the current directory."),
            ("Port owner", "Show which process is listening on port 8000."),
            ("Format JSON", "Pretty-print package.json without modifying it."),
        ),
        avoid="running generated commands without inspecting the exact command and risk",
    )
    EXAMPLE_SELECTOR = "#command-request"
    RESULT_SELECTOR = "#command-log, #command-preview"

    DEFAULT_CSS = """
    CommandPane Label.field { height: 1; color: $text-muted; }
    CommandPane Horizontal#command-suggest-row { height: 3; }
    CommandPane Input#command-request { width: 1fr; }
    CommandPane Button { width: auto; }
    CommandPane Static#command-preview {
        border: heavy $accent; height: auto; min-height: 3; padding: 0 1;
    }
    CommandPane Static#command-context, CommandPane Static#command-risk {
        height: auto; min-height: 1; color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._command = ""
        self._armed_command = ""
        self._running = False
        self._proc: subprocess.Popen | None = None
        self._run_id = 0
    def compose_body(self) -> ComposeResult:
        yield Label("Desired outcome", classes="field")
        with Horizontal(id="command-suggest-row"):
            yield Input(
                placeholder="Describe the outcome; Enter suggests but never runs",
                id="command-request",
            )
            yield Button("Suggest", id="command-suggest", variant="primary")
        yield Label("Exact command (read before Run)", classes="field")
        yield Static("No command suggested.", id="command-preview", markup=False)
        yield Static(
            f"Context: {platform.platform()} · {_shell_context()} · cwd={Path.cwd()}",
            id="command-context",
            markup=False,
        )
        yield Static("Risk: not assessed", id="command-risk", markup=False)
        with Horizontal(id="command-actions"):
            yield Button("Run", id="command-run", variant="error", disabled=True)
            yield Button("Copy", id="command-copy", disabled=True)
            yield Button("Cancel", id="command-cancel", disabled=True)
        yield RichLog(id="command-log", wrap=True, highlight=False, markup=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "command-request":
            event.stop()
            self.run_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "command-suggest":
            event.stop()
            self.run_primary()
        elif button_id == "command-run" and self._command:
            event.stop()
            if self._running:
                self.notify("A command is already running", severity="warning")
                return
            risk = _risk(self._command)
            if risk.startswith("HIGH") and self._armed_command != self._command:
                self._armed_command = self._command
                event.button.label = "Confirm HIGH risk"
                self.notify("High-risk command: click Confirm HIGH risk to execute", severity="warning", timeout=10)
                return
            self._armed_command = ""
            event.button.label = "Run"
            self._running = True
            event.button.disabled = True
            self._run_id += 1
            self._run_command(self._command, self._run_id)
        elif button_id == "command-copy" and self._command:
            event.stop()
            self.app.copy_to_clipboard(self._command)
            self.notify("Command copied")
        elif button_id == "command-cancel":
            event.stop()
            self._run_id += 1
            if self._running and self._proc:
                _kill_process_tree(self._proc)
                self._proc = None
            self._running = False
            self._set_command("")

    def run_primary(self) -> None:
        request = self.query_one("#command-request", Input).value.strip()
        if not request:
            self.notify("Describe the desired outcome first", severity="warning")
            return
        self._suggest(request)

    def _set_command(self, command: str) -> None:
        self._command = command
        self._armed_command = ""
        self.query_one("#command-run", Button).label = "Run"
        self.query_one("#command-preview", Static).update(
            command or "No command suggested."
        )
        self.query_one("#command-risk", Static).update(
            f"Risk: {_risk(command)}" if command else "Risk: not assessed"
        )
        for button_id in ("command-run", "command-copy", "command-cancel"):
            self.query_one(f"#{button_id}", Button).disabled = not command

    @work(exclusive=True, group="command-suggest")
    async def _suggest(self, request: str) -> None:
        model = self.model_for("default")
        self._set_command("")
        self.query_one("#command-preview", Static).update("Generating one command…")
        prompt = COMMAND_PROMPT.format(
            os=platform.platform(),
            shell=_shell_context(),
            cwd=Path.cwd(),
            request=request,
        )
        try:
            response = await self.api.chat(
                [{"role": "user", "content": prompt}], model=model
            )
            command = _single_command(response)
        except (ApiError, ValueError) as error:
            self._set_command("")
            self.notify(f"Suggestion failed: {error}", severity="error")
            self.record("error", f"suggest: {error}", model)
        else:
            self._set_command(command)
            self.record("ok", "command suggested; awaiting explicit Run", model)

    # Sanctioned shell=True: the user saw the exact command and explicitly
    # clicked Run. This is the only place in the app allowed to do this.
    @work(thread=True, exclusive=True, group="command-run")
    def _run_command(self, command: str, run_id: int) -> None:
        kwargs = {}
        if platform.system() != "Windows":
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                **kwargs,
            )
            self._proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=120)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                stdout, stderr = proc.communicate()
                returncode = -1
                stderr = ((stderr or "") + "\n[Command timed out after 120s]").strip()
        except (OSError, subprocess.SubprocessError) as error:
            returncode = -1
            stdout = ""
            stderr = str(error)
        finally:
            self._proc = None

        self.app.call_from_thread(
            self._command_finished,
            command,
            returncode,
            stdout or "",
            stderr or "",
            run_id,
        )

    def _command_finished(
        self, command: str, returncode: int, stdout: str, stderr: str, run_id: int
    ) -> None:
        if self._run_id != run_id:
            return
        self._running = False
        self.query_one("#command-run", Button).disabled = not bool(self._command)
        log = self.query_one("#command-log", RichLog)
        log.write(f"$ {command}")
        max_chars = 50_000
        if stdout:
            text = stdout.rstrip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... (stdout truncated)"
            log.write(text)
        if stderr:
            text = stderr.rstrip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... (stderr truncated)"
            log.write(text)
        status = "ok" if returncode == 0 else "error"
        self.record(status, f"command exit {returncode}")


class ActivityPane(ToolPane):
    """Inspect the shell's bounded local request and artifact history."""

    META = ToolMeta(
        key="activity",
        title="Activity",
        purpose="Replay every call this workbench made, and what it cost.",
        request_estimate="0",
        group="System",
        role="",
        action="Refresh",
        input_label="Session filter",
        output_label="Calls + cost",
        examples=(
            ("Failures", "error"),
            ("Images", ".png"),
            ("Git activity", "git"),
        ),
        avoid="using local session history as provider billing or a remote audit log",
    )
    EXAMPLE_SELECTOR = "#activity-filter"
    RESULT_SELECTOR = "#activity-log, #activity-table"

    DEFAULT_CSS = """
    ActivityPane Label.field { height: 1; color: $text-muted; }
    ActivityPane Horizontal#activity-filter-row { height: 3; }
    ActivityPane Input#activity-filter { width: 1fr; }
    ActivityPane Button { width: auto; }
    ActivityPane TabbedContent { height: 1fr; min-height: 10; }
    ActivityPane DataTable#activity-table,
    ActivityPane OptionList#activity-artifacts,
    ActivityPane RichLog#activity-log {
        border: round $primary; height: 1fr; min-height: 6;
    }
    ActivityPane Static#activity-empty { height: 1; color: $text-muted; }
    """

    def compose_body(self) -> ComposeResult:
        yield Label("Filter tool, status, model, detail, or artifact", classes="field")
        with Horizontal(id="activity-filter-row"):
            yield Input(placeholder="All local activity", id="activity-filter")
            yield Button("Refresh", id="activity-refresh", variant="primary")
        yield Static("", id="activity-empty")
        with TabbedContent(initial="activity-requests-tab"):
            with TabPane("Requests", id="activity-requests-tab"):
                yield DataTable(
                    id="activity-table", zebra_stripes=True, cursor_type="row"
                )
            with TabPane("Spend", id="activity-spend-tab"):
                yield Static("", id="activity-spend")
            with TabPane("Artifacts", id="activity-artifacts-tab"):
                yield OptionList(id="activity-artifacts")
            with TabPane("Session log", id="activity-log-tab"):
                yield RichLog(
                    id="activity-log", wrap=True, highlight=False, markup=False
                )

    def on_mount(self) -> None:
        table = self.query_one("#activity-table", DataTable)
        table.add_columns("Time", "Tool", "Model", "Credits", "Status", "Detail")
        self.refresh_activity()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "activity-filter":
            self.refresh_activity()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "activity-filter":
            event.stop()
            self.run_primary()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "activity-refresh":
            event.stop()
            self.run_primary()

    def run_primary(self) -> None:
        self.refresh_activity()
        self.record("ok", "refreshed local activity")

    def refresh_activity(self) -> None:
        try:
            table = self.query_one("#activity-table", DataTable)
            artifacts = self.query_one("#activity-artifacts", OptionList)
            log = self.query_one("#activity-log", RichLog)
            empty = self.query_one("#activity-empty", Static)
            filter_widget = self.query_one("#activity-filter", Input)
        except Exception:
            return

        query = filter_widget.value.strip().lower()
        records = list(getattr(self.app, "activity", ()))
        if not records:
            records = list(getattr(self.app, "activity_records", ()))
        visible = [
            record
            for record in records
            if not query
            or query
            in " ".join(
                str(record.get(field, ""))
                for field in ("time", "tool", "status", "detail", "model")
            ).lower()
        ]

        table.clear()
        artifacts.clear_options()
        log.clear()
        for record in visible:
            time_text = str(record.get("time", ""))
            tool = str(record.get("tool", ""))
            model = str(record.get("model", ""))
            status = str(record.get("status", ""))
            detail = str(record.get("detail", ""))
            cost = record.get("credits")
            table.add_row(
                time_text, tool, model, format_credits(cost) if cost is not None else "—",
                status, detail,
            )
            log.write(
                " ".join(
                    part
                    for part in (time_text, status.upper(), tool, model, detail)
                    if part
                )
            )
            if _ARTIFACT_RE.search(detail):
                artifacts.add_option(f"{time_text}  {tool}  {detail}")
        empty.update(
            "No matching local activity." if not visible else f"{len(visible)} local event(s)"
        )
        self._refresh_spend_tab()

    def _refresh_spend_tab(self) -> None:
        """Summarise today's credit use and the recent daily trend."""
        try:
            panel = self.query_one("#activity-spend", Static)
        except Exception:
            return
        totals = today_totals()
        if not totals.get("requests"):
            panel.update(
                "[dim]No API calls yet today.[/dim]\n\n"
                "[b]API calls[/b] counts how many times a tool hit the Atessa "
                "endpoint. Local work (extraction, transcripts, feeds) is free "
                "and never counted.\n"
                "[b]Quota credits[/b] is what those calls cost against your "
                "plan: each call bills one request times that model's quota "
                "weight, so an expensive model costs several credits per call. "
                "Import weights from Models → Credit costs."
            )
            return
        credits = totals.get("credits", 0)
        requests = totals.get("requests", 0)
        lines = [
            f"[b]Today: {requests} API {'call' if requests == 1 else 'calls'} "
            f"· {credits:g} quota {'credit' if credits == 1 else 'credits'}[/b]",
            "[dim]API calls = how many times a tool hit Atessa. Quota credits = "
            "what those calls cost (one request × the model's quota weight).[/dim]",
        ]
        if totals.get("unpriced"):
            lines.append(
                f"[dim]{totals['unpriced']} request(s) used models with no imported "
                "cost — import from Models → Credit costs.[/dim]"
            )
        busiest = top_tools()
        if busiest:
            lines.append("")
            lines.append("[b]Where it went[/b]")
            lines.extend(f"  {tool:10} {credits:g}" for tool, credits in busiest)
        history = recent_days(7)
        if len(history) > 1:
            lines.append("")
            lines.append("[b]Recent days[/b]")
            for day, data in history:
                lines.append(
                    f"  {day}  {data.get('credits', 0):g} credits "
                    f"· {data.get('requests', 0)} requests"
                )
        panel.update("\n".join(lines))
