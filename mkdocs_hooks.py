"""mkdocs hooks: build the pages that list every rule, and publish each page's Markdown source next to its HTML.

The rule index (``CHECKS.md``), the front page (``index.md``), ``llms.txt``,
``llms-full.txt`` and ``rules.json`` come from
:func:`inspect_evals_lint.docs.site_files` at build time rather than from
``docs/``. Every new rule changes them, so committed copies would make any two
pull requests that add rules conflict. The committed ``docs/CHECKS.md`` only
points at the site, for the links older releases print, and the generated
index replaces it.

Agents and tools prefer Markdown to rendered HTML. Every page's Markdown is
written to ``site/<path>.md`` and, for pages served as a directory, to
``site/<path>/index.md`` as well, so ``rules/IEBP002.md`` and
``rules/IEBP002/index.md`` both return the source of ``rules/IEBP002/``.
``llms.txt`` lists the ``.md`` form of every page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspect_evals_lint.docs import site_files


def on_files(files: Any, config: Any) -> Any:
    from mkdocs.structure.files import File

    root = Path(config["config_file_path"]).parent
    for path, content in site_files(root).items():
        committed = files.get_file_from_path(path)
        if committed is not None:
            files.remove(committed)
        files.append(File.generated(config, path, content=content))
    return files


def on_post_page(output: str, page: Any, config: Any) -> str:
    relative = Path(page.file.src_uri)
    site_dir = Path(config["site_dir"])
    targets = [site_dir / relative]
    if relative.name != "index.md":
        targets.append(site_dir / relative.with_suffix("") / "index.md")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.file.content_string, encoding="utf-8")
    return output
