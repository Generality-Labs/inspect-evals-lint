"""mkdocs hooks: publish each page's Markdown source next to its HTML.

Agents and tools prefer Markdown to rendered HTML. After the build, every
``docs/<path>.md`` is copied to ``site/<path>.md`` and, for pages served as a
directory, to ``site/<path>/index.md`` as well, so ``rules/IEBP002.md`` and
``rules/IEBP002/index.md`` both return the source of ``rules/IEBP002/``.
``llms.txt`` lists the ``.md`` form of every page.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def on_post_build(config: Any, **_: Any) -> None:
    docs_dir = Path(config["docs_dir"])
    site_dir = Path(config["site_dir"])
    for source in docs_dir.rglob("*.md"):
        relative = source.relative_to(docs_dir)
        flat = site_dir / relative
        flat.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, flat)
        if relative.name != "index.md":
            nested = site_dir / relative.with_suffix("") / "index.md"
            nested.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, nested)
