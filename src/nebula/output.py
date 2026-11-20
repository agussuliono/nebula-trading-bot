"""CLI result output with best-effort clipboard copying.

Copying affects only the displayed command result, never source files or inputs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _enabled() -> bool:
    value = os.getenv("NEBULA_AUTO_COPY_RESULT", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def copy_result_to_clipboard(text: str) -> bool:
    """Copy result text to the desktop clipboard when a supported tool exists."""
    if not text or not _enabled():
        return False

    candidates: list[list[str]] = []
    if sys.platform.startswith("linux"):
        if os.getenv("WAYLAND_DISPLAY"):
            candidates.append(["wl-copy"])
        if os.getenv("DISPLAY"):
            candidates.extend([
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ])
        # Try available tools even when session variables are absent (e.g. some WSL setups).
        candidates.extend([["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]])
    elif sys.platform == "darwin":
        candidates.append(["pbcopy"])
    elif os.name == "nt":
        candidates.append(["clip.exe"])

    seen: set[tuple[str, ...]] = set()
    for command in candidates:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        executable = shutil.which(command[0])
        if not executable:
            continue
        try:
            completed = subprocess.run(
                [executable, *command[1:]],
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
            )
            if completed.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def emit_result(text: str) -> None:
    """Print the result and copy the exact same text, best-effort, to clipboard."""
    print(text)
    copy_result_to_clipboard(text)
