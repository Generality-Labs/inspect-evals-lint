"""Finding and reading Compose files, shared by the rules that check them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic


def is_compose_file(path: Path) -> bool:
    """``compose.yaml``, ``docker-compose.override.yml`` and the like."""
    return path.is_file() and bool(re.fullmatch(r"(docker-)?compose.*\.ya?ml", path.name))


def iter_compose_files(ctx: LintContext) -> list[Path]:
    """Compose files under the package, in stable order, including ``exclude``d directories.

    ``exclude`` is for code shipped into a sandbox. Compose files are read on the
    host to build that sandbox, often from an excluded challenge directory.
    """
    return sorted(path for path in ctx.path.rglob("*.y*ml") if is_compose_file(path))


class _ComposeLoader(yaml.SafeLoader):
    """Safe loading that accepts Compose's own tags."""


def _construct_tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    # Compose's merge tags (!reset, !override) change how override files combine
    # with a base file; within one file the setting is the tagged value itself.
    tag: str
    if isinstance(node, yaml.ScalarNode):
        tag = cast(str, loader.resolve(yaml.ScalarNode, node.value, (True, False)))  # pyright: ignore[reportUnknownMemberType]
    elif isinstance(node, yaml.SequenceNode):
        tag = "tag:yaml.org,2002:seq"
    else:
        tag = "tag:yaml.org,2002:map"
    untagged = type(node)(tag, node.value, node.start_mark, node.end_mark)
    return loader.construct_object(untagged)  # pyright: ignore[reportUnknownMemberType]


_ComposeLoader.add_multi_constructor("!", _construct_tagged)  # pyright: ignore[reportUnknownMemberType]


def load_compose(path: Path) -> tuple[Any, Diagnostic | None]:
    """The parsed file (``{}`` when empty), or a warning saying why it could not be read."""
    try:
        compose: Any = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, Diagnostic(f"Could not parse compose file: {e}", file=path, severity="warning")
    return ({} if compose is None else compose), None
