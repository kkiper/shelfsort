"""Source mount health checks."""

from __future__ import annotations

import os
from dataclasses import dataclass

_MARKER = ".shelfsort"


@dataclass
class SourceStatus:
    mounted: bool
    writable: bool
    detail: str


def check_source(root: str) -> SourceStatus:
    """Mounted = root is a directory that is non-empty or holds `.shelfsort`.

    The marker folder is created on the first successful check. Writable =
    a probe file can be created and removed inside it.
    """
    if not os.path.exists(root):
        return SourceStatus(False, False, f"{root} does not exist")
    if not os.path.isdir(root):
        return SourceStatus(False, False, f"{root} is not a directory")
    marker = os.path.join(root, _MARKER)
    try:
        if not os.path.isdir(marker) and not os.listdir(root):
            return SourceStatus(False, False, f"{root} is empty (not mounted?)")
    except OSError as exc:
        return SourceStatus(False, False, f"cannot read {root}: {exc}")
    probe = os.path.join(marker, "write-test")
    try:
        os.makedirs(marker, exist_ok=True)
        with open(probe, "wb"):
            pass
        os.remove(probe)
    except OSError as exc:
        return SourceStatus(True, False, f"mounted but not writable: {exc}")
    return SourceStatus(True, True, "ok")
