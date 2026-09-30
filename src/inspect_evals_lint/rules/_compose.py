"""Finding and reading Compose files, shared by the rules that check them."""

from __future__ import annotations

import re
from collections.abc import Iterator
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


class ComposeMapping(dict[Any, Any]):
    """A loaded mapping that remembers the line each key is on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: dict[Any, int] = {}


class ComposeSequence(list[Any]):
    """A loaded sequence that remembers the line each item is on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[int] = []


def line_of(container: Any, key: Any) -> int | None:
    """The 1-based line of a mapping key or sequence index in a loaded compose file."""
    if isinstance(container, ComposeMapping):
        return container.lines.get(key)
    if (
        isinstance(container, ComposeSequence)
        and isinstance(key, int)
        and key < len(container.lines)
    ):
        return container.lines[key]
    return None


class _ComposeLoader(yaml.SafeLoader):
    """Safe loading that keeps line numbers and accepts Compose's own tags."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> Iterator[ComposeMapping]:
    data = ComposeMapping()
    yield data
    data.update(loader.construct_mapping(node))  # pyright: ignore[reportUnknownMemberType]
    # construct_mapping has merged any << keys into node.value, the file's own keys last.
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)  # pyright: ignore[reportUnknownMemberType]
        data.lines[key] = key_node.start_mark.line + 1


def _construct_sequence(
    loader: yaml.SafeLoader, node: yaml.SequenceNode
) -> Iterator[ComposeSequence]:
    data = ComposeSequence()
    yield data
    data.extend(loader.construct_sequence(node))  # pyright: ignore[reportUnknownMemberType]
    data.lines = [item.start_mark.line + 1 for item in node.value]


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


_ComposeLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)  # pyright: ignore[reportUnknownMemberType]
_ComposeLoader.add_constructor("tag:yaml.org,2002:seq", _construct_sequence)  # pyright: ignore[reportUnknownMemberType]
_ComposeLoader.add_multi_constructor("!", _construct_tagged)  # pyright: ignore[reportUnknownMemberType]


def load_compose(path: Path) -> tuple[Any, Diagnostic | None]:
    """The parsed file (``{}`` when empty), or a warning saying why it could not be read."""
    try:
        compose: Any = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, Diagnostic(f"Could not parse compose file: {e}", file=path, severity="warning")
    return ({} if compose is None else compose), None
