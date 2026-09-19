"""Path presentation shared by the renderers."""

from __future__ import annotations

from pathlib import Path


def relative_to_root(path: Path, root: Path | None) -> str:
    """``path`` relative to ``root`` with forward slashes when it falls under it; otherwise as given.

    Rules record absolute paths, which would tie any output to the machine that produced it.
    """
    if root is not None and path.is_absolute() and path.is_relative_to(root):
        return path.relative_to(root).as_posix()
    return path.as_posix()
