# Security Policy

Atessa Toolbelt is committed to maintaining user privacy, local data safety, and secure model interactions.

---

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `0.3.x` (Beta) | :white_check_mark: |
| `< 0.3.0` | :x: |

---

## Security Architecture & Design Principles

1. **Subprocess Isolation:** System integration (Git diffs, screen capture utilities) executes using explicit argument arrays (`argv` list form) to prevent shell injection vulnerabilities.
2. **Command Suggestion Boundaries:** The `atessa-shell` CLI and TUI `Command` pane generate single-line commands but **never** execute them automatically. User review is required, and high-risk operations trigger secondary confirmation warnings.
3. **Network Destination Safeguards:** URL reader routines reject requests pointing to loopback addresses (`127.0.0.1`, `localhost`), link-local IPs, private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and non-standard schemes.
4. **Local Data Privacy:** API keys, role configurations, request metering ledgers (`spend.json`), and ELO ratings (`arena.json`) remain local to `~/.atessa/` and are never transmitted to third-party telemetry services.

---

## Reporting a Vulnerability

If you discover a security vulnerability:
1. Please **do not** report security vulnerabilities through public GitHub issues.
2. Email the maintainers directly or send a private security advisory through GitHub.
3. Include detailed steps to reproduce the issue, along with affected versions and operating systems.
4. We will acknowledge receipt of your vulnerability report within 48 hours and work with you on a resolution timeline.
