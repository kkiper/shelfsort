"""Wrappers around external command-line tools."""

from __future__ import annotations

import re
import subprocess


class ToolError(Exception):
    pass


class ToolTimeout(ToolError):
    pass


def run_tool(args: list[str], timeout: float = 60) -> str:
    """Run a command and return its stdout (UTF-8, errors replaced)."""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:  # run() kills the child before raising
        raise ToolTimeout(f"{args[0]} timed out after {timeout}s") from e
    except OSError as e:
        raise ToolError(f"cannot run {args[0]}: {e}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"{args[0]} exited with {proc.returncode}: {stderr}")
    return proc.stdout.decode("utf-8", errors="replace")


_KEY_RE = re.compile(r"^(\S[^:]*?)\s*:\s?(.*)$")


def ebook_meta_read(path: str) -> dict[str, str]:
    """Parse `ebook-meta <path>` output into a dict with lowercased keys."""
    out = run_tool(["ebook-meta", path])
    result: dict[str, str] = {}
    last: str | None = None
    for line in out.splitlines():
        if line[:1].isspace() and last is not None:
            # continuation of a multi-line value (e.g. comments)
            if line.strip():
                result[last] += "\n" + line.strip()
            continue
        m = _KEY_RE.match(line)
        if not m:
            continue
        last = m.group(1).strip().lower()
        result[last] = m.group(2).strip()
    return result
