# Contributing to Atessa Toolbelt

Thank you for your interest in contributing to Atessa Toolbelt! We welcome bug reports, feature suggestions, documentation improvements, and code contributions.

---

## Code of Conduct

This project adheres to a standard Code of Conduct (see `CODE_OF_CONDUCT.md`). By participating, you are expected to uphold this code.

---

## How to Contribute

### 1. Reporting Bugs
If you find a bug or unexpected behavior:
- Check existing issues to make sure it has not already been reported.
- Open a new issue with a clear title, steps to reproduce, expected vs. actual behavior, and details about your OS and Python version.

### 2. Feature Requests
If you have an idea for a new CLI command or TUI pane:
- Open a feature request issue describing the use case and proposed behavior.
- Discuss the design before opening a large pull request.

### 3. Submitting Pull Requests
- Fork the repository and create a topic branch (`git checkout -b feature/my-feature`).
- Ensure all code conforms to standard Python formatting (PEP 8) and passes linting (`ruff` / `flake8`).
- Run the test suite before submitting:
  ```bash
  python tests/test_cli.py
  python tests/test_tui.py
  ```
- Keep pull requests focused on a single change or bug fix.
- Write clear commit messages.

---

## Security Guidelines

- **Never** commit API keys, personal credentials, or local spend ledgers (`config`, `spend.json`, `weights.json`).
- Ensure all subprocess calls use list arguments (`argv`) rather than `shell=True` unless explicitly intended for user-approved command execution.
- Maintain destination URL safety checks in any web-fetching or reader components.
