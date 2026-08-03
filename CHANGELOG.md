# Changelog

All notable changes to the Atessa Toolbelt project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
