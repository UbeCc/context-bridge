#!/usr/bin/env python3
"""Public-repository safety scan.

Fails when tracked files look like they contain credentials, internal infrastructure details or
live data. Run in CI and before every push. This is deliberately conservative: false positives
are cheap to fix, a leaked credential is not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# (name, regex) — case-insensitive unless stated. Keep patterns narrow enough to be actionable.
FORBIDDEN = [
    ("corporate domain", r"glm\.ai|bigmodel\.cn|deepseek\.com/(?!en/harness)"),
    # Loopback (127.0.0.1 / localhost) is public-safe and allowed. Only real RFC1918 hosts matter.
    ("private IP", r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"),
    ("internal hostname", r"\b(?:dev-\d{3,4}|haoran-[a-z0-9-]+|euler-\d+)\b"),
    ("feishu id", r"\b(?:oc|om|ou|omt)_[0-9a-f]{8,}"),
    ("credential shape", r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_|AKIA[0-9A-Z]{16}|xox[baprs]-|-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("bearer token", r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    ("unix home path", r"/root/|/home/[a-z0-9_-]+/"),
    ("absolute scratch path", r"/workspace/|/srv/(?!context-hub)"),
]
ALLOW_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2", ".gz", ".zst"}
ALLOW_LINE_MARKERS = ("# nosec", "public-safety: allow")


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [Path(p) for p in out.splitlines() if p]


def main() -> int:
    import re

    problems: list[str] = []
    patterns = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in FORBIDDEN]
    for path in tracked_files():
        if path.as_posix() == "scripts/check_public_safety.py":
            continue  # the blocklist itself
        if path.suffix.lower() in ALLOW_SUFFIX or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, 1):
            if any(marker in line for marker in ALLOW_LINE_MARKERS):
                continue
            for name, rx in patterns:
                if rx.search(line):
                    problems.append(f"{path}:{lineno}: {name}: {line.strip()[:160]}")
    if problems:
        print("public-safety scan FAILED:\n" + "\n".join(problems), file=sys.stderr)
        return 1
    print(f"public-safety scan OK ({len(tracked_files())} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
