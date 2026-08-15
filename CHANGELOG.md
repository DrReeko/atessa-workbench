# Changelog

All notable changes to the Atessa Toolbelt project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Live theme switching in the TUI:** `Ctrl+T` (or the `Theme` binding) now
  cycles through five built-in workbench palettes (Midnight, Nord, Dracula,
  Tokyo Night, Aurora) and re-colors the entire UI instantly. Palettes are
  defined in `atessa_tui/themes.py`.
- **Popup model picker:** Council, Benchmark, and Arena choose models through
  a filterable popup — type to filter, Space toggles, Ctrl+Enter confirms.
  Arena gains an explicit two-model pair with a "Random pair" option. The
  invisible inline checkbox grid is gone.
- **Council judge picker:** the judge model is chosen through the same popup
  (single-select, defaults to the power route).
- **Ctrl+C now copies:** `ctrl+y` was replaced with `ctrl+c` for copying the
  active result; selected text in inputs is copied instead. Ctrl+C no longer
  quits — the notification reminds you that `ctrl+q` quits.

### Removed
- **Voice feature removed entirely:** `atessa-voice`, the TUI Voice pane,
  the `audio` model role, local audio playback (`atessa_tui/audio.py`), and
  the `atessa-transcribe` placeholder are gone. No working speech model
  exists on the proxy.

### Fixed
- **Themes now actually switch:** the palette tokens were hard-coded as CSS
  variables in `app.tcss`, which shadowed the active Textual theme and made
  switching a no-op. Those values now come from the theme, so changing it
  re-resolves every color live.
- **Models pane no longer crashes on startup:** the catalog loader queried DOM
  nodes before the pane's body was mounted in the content switcher, raising
  `NoMatches` in a background worker. The loader now caches the catalog and
  renders when the pane is first displayed.
- **Static results are copyable:** `_widget_text` never handled `Static`
  widgets, so Ctrl+C returned nothing for panes whose result lives in a
  Static (e.g. the Command pane preview). Static content is now extracted
  correctly.

### Model catalog
- The Models pane and `atessa-models` now read the complete live `/v1/models` catalog on each reload; Claude entries are shown whenever the endpoint publishes them.
- Existing configured role routes remain unchanged; live catalog refresh no longer hides or hard-codes model IDs.

## [0.3.0b1] - 2026-08-01

### Added
- **Native Python CLI suite:** Replaced all legacy bash and `.cmd` launchers with native Python entry points (`atessa-chat`, `atessa-search`, `atessa-read`, `atessa-image`, `atessa-view`, `atessa-shot`, `atessa-council`, `atessa-bench`, `atessa-arena`, `atessa-explain`, `atessa-git`, `atessa-shell`, `atessa-models`, `atessa-activity`, `atessa-ghsearch`, `websearch`).
- **14-Pane Textual TUI Workbench (`atessa`):** Integrated full keyboard and mouse workbench shell featuring Create, Compare, Develop, and System tool groups.
- **Direct GitHub Search:** Built native Python REST discovery module (`github_search`) replacing WSL forwarding.
- **Cross-Platform Installer Automation:** PyInstaller specification (`atessa.spec`) and Inno Setup script (`installer.iss`) for standalone Windows and macOS console packaging.
- **Automated CI/CD:** GitHub Actions release workflow (`.github/workflows/beta.yml`) for multi-platform building, testing, and asset packaging.
- **Comprehensive Test Suite:** Modular tests under `tests/` covering CLI contract logic, rate-limiting, source sanitization, and TUI interaction/responsive layouts.

### Changed
- Refactored model routing plumbing to support `default`, `vision`, `ocr`, `power`, and `image` model roles persisted in `~/.atessa/config`.
- Hardened web reader and HTTP client components with private IP address filtering and response size bounding.
- Standardized execution safety across system capture and Git utilities to use argument arrays rather than shell execution strings.

### Removed
- Legacy bash launcher scripts (`bin/*.sh`, `windows-bin/*.cmd`, `lib.sh`).
- Obsolete design prototypes and standalone mockup modules.
