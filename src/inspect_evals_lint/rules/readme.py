"""README rules: the commands a README shows agree with the package they document."""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import parse_python_files
from inspect_evals_lint.rules.best_practices import TaskDefaultsVisitor
from inspect_evals_lint.rules.dependencies import normalize_name
from inspect_evals_lint.rules.file_structure import readme_path

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
SEPARATOR_CHARS = frozenset(";&|")
"""A word made only of these is a separator such as ``&&`` or ``;``, written with or without spaces."""
MAX_QUOTED_LINES = 20
"""How many lines an open quote may carry a command over before the line is given up on, so a stray apostrophe in prose stays cheap."""


@dataclass(frozen=True)
class Word:
    """One shell word of a README command, with the line it is on."""

    text: str
    line: int


Command = tuple[Word, ...]
"""A simple command: the words between separators such as ``&&``."""


def _split(text: str) -> list[str] | None:
    """Shell words, with ``;``, ``&&`` and ``|`` split off even when unspaced; None for an open quote."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _words(lines: list[tuple[int, str]]) -> list[Word] | None:
    """The words of one logical line, each on its own physical line where quoting allows."""
    per_line = [(number, _split(text)) for number, text in lines]
    if all(words is not None for _, words in per_line):
        return [Word(word, number) for number, words in per_line for word in words or ()]
    joined = _split("\n".join(text for _, text in lines))
    if joined is None:
        return None
    return [Word(word, lines[0][0]) for word in joined]


def _logical_lines(block: list[tuple[int, str]]) -> Iterator[list[tuple[int, str]]]:
    """Group a code block's lines into commands: a trailing backslash continues one, as does an open quote."""
    i = 0
    while i < len(block):
        end = i
        while True:
            pieces = block[i : end + 1]
            continued = pieces[-1][1].rstrip().endswith("\\")
            if end + 1 < len(block) and (
                continued
                or (
                    end - i < MAX_QUOTED_LINES
                    and _split("\n".join(text for _, text in pieces)) is None
                )
            ):
                end += 1
                continue
            break
        pieces = [
            (number, text.rstrip()[:-1] if text.rstrip().endswith("\\") else text)
            for number, text in block[i : end + 1]
        ]
        if _split("\n".join(text for _, text in pieces)) is None:
            # An unbalanced quote that never closes: give up on this line only.
            pieces, end = pieces[:1], i
        yield pieces
        i = end + 1


def _commands(block: list[tuple[int, str]]) -> Iterator[Command]:
    for lines in _logical_lines(block):
        words = _words(lines)
        if not words:
            continue
        command: list[Word] = []
        for word in words:
            if word.text and set(word.text) <= SEPARATOR_CHARS:
                if command:
                    yield tuple(command)
                command = []
            else:
                command.append(word)
        if command:
            yield tuple(command)


def readme_blocks(readme: Path) -> list[list[Command]]:
    """The shell commands in each of the README's fenced code blocks and inline code spans, in order.

    Raises:
        OSError: the README cannot be read.
        UnicodeDecodeError: the README is not UTF-8.
    """
    blocks: list[list[Command]] = []
    block: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), start=1):
        opening = FENCE.match(line)
        if fence is None:
            if opening:
                fence = opening.group(1)
                block = []
                continue
            for span in INLINE_CODE.finditer(line):
                blocks.append(list(_commands([(number, span.group(1))])))
        elif line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
            blocks.append(list(_commands(block)))
            fence = None
        else:
            block.append((number, line))
    if fence is not None:
        blocks.append(list(_commands(block)))
    return blocks


def readme_commands(readme: Path) -> list[Command]:
    """Every shell command in the README's fenced code blocks and inline code spans.

    Raises:
        OSError: the README cannot be read.
        UnicodeDecodeError: the README is not UTF-8.
    """
    return [command for block in readme_blocks(readme) for command in block]


@dataclass(frozen=True)
class TaskSignature:
    """The keyword parameters of one ``@task`` function."""

    file: Path
    parameters: frozenset[str]
    accepts_any: bool
    """It takes ``**kwargs``, so any ``-T`` name reaches it."""


def package_tasks(ctx: LintContext) -> dict[str, list[TaskSignature]]:
    """Every ``@task`` function in the package by name; a name defined twice keeps both."""
    tasks: dict[str, list[TaskSignature]] = {}
    for parsed in parse_python_files(ctx).parsed:
        visitor = TaskDefaultsVisitor()
        visitor.visit(parsed.tree)
        for (name, _, parameters), node in zip(visitor.tasks, visitor.nodes, strict=True):
            tasks.setdefault(name, []).append(
                TaskSignature(parsed.path, frozenset(parameters), node.args.kwarg is not None)
            )
    return tasks


TASK_RUNNERS = ("eval", "eval-set")


def _task_runs(command: Command) -> Iterator[tuple[list[Word], list[Word]]]:
    """``(tasks, task arguments)`` for an ``inspect eval`` or ``inspect eval-set`` in the command.

    Tasks are the positional words straight after the subcommand; task
    arguments are the values of every ``-T`` (``-T name=value`` or
    ``-Tname=value``).
    """
    for i, word in enumerate(command[:-1]):
        if Path(word.text).name != "inspect" or command[i + 1].text not in TASK_RUNNERS:
            continue
        rest = command[i + 2 :]
        tasks: list[Word] = []
        for candidate in rest:
            if candidate.text.startswith("-"):
                break
            tasks.append(candidate)
        arguments: list[Word] = []
        for j, option in enumerate(rest):
            if option.text == "-T" and j + 1 < len(rest):
                arguments.append(rest[j + 1])
            elif option.text.startswith("-T") and len(option.text) > 2:
                arguments.append(Word(option.text[2:], option.line))
        yield tasks, arguments
        return


def _resolve(
    ctx: LintContext, spec: str, readme: Path, tasks: dict[str, list[TaskSignature]]
) -> list[TaskSignature]:
    """The package's task functions ``spec`` names; empty when it names none of them.

    ``<namespace>/<task>`` (the namespace being the package's top-level module,
    as Inspect registers it), a bare ``<task>``, and ``path/to/file.py@task``
    with the path taken from the repository root, the README's directory or
    the package.
    """
    if "@" in spec:
        file, _, name = spec.rpartition("@")
        if not file.endswith(".py"):
            return []
        candidates = {
            (base / file).resolve() for base in (ctx.root, readme.parent, ctx.path) if file
        }
        return [s for s in tasks.get(name, []) if s.file.resolve() in candidates]
    if spec.endswith(".py"):
        return []
    if "/" in spec:
        namespace, _, name = spec.partition("/")
        if namespace != ctx.config.module_name(ctx.name).split(".")[0]:
            return []
        return tasks.get(name, [])
    return tasks.get(spec, [])


def _parameter_hint(task: str, signatures: list[TaskSignature]) -> str:
    parameters = sorted({p for s in signatures for p in s.parameters})
    if not parameters:
        return f"{task}() takes no parameters; remove the -T argument"
    return f"use one of {task}()'s parameters: {', '.join(parameters)}"


@rule(
    code="IEFS007",
    name="readme_task_args",
    category="file_structure",
    summary="Every -T argument in a README command names a parameter of the task it runs",
    references=(inspect_docs("tasks", "Tasks: Parameters", "parameters"),),
)
def readme_task_args(ctx: LintContext) -> Iterable[Finding]:
    """Every ``-T`` argument in a README command names a parameter of the task it runs.

    ## What it does
    Reads the ``inspect eval`` and ``inspect eval-set`` commands in the README's
    fenced code blocks and inline code, with or without a ``uv run`` prefix and
    across line continuations. Each task the command names is resolved
    against the package's ``@task`` functions: ``<namespace>/<task>``, a bare
    ``<task>``, or ``path/to/file.py@task``. Every ``-T name=value`` must then
    name a parameter of every resolved task. Fails once per argument and task.

    Tasks that resolve to nothing in the package, such as another package's
    tasks or a placeholder like ``my_eval_XX``, are not checked. Neither is a task
    that takes ``**kwargs``.

    ## Why is this bad?
    Inspect drops a ``-T`` argument the task does not take and only logs a
    warning. Readers copy README commands, so one that shows
    ``-T max_messages=75`` for a task whose parameter is ``message_limit`` runs
    with the default. Nothing else checks hand-written examples: a renamed
    parameter leaves them behind, even in a repository whose README generator
    keeps a parameter list up to date.

    ## Example
    ```python
    @task
    def my_eval(message_limit: int = 50) -> Task: ...
    ```
    ```bash
    inspect eval my_eval/my_eval -T max_messages=75
    ```
    Use instead:
    ```bash
    inspect eval my_eval/my_eval -T message_limit=75
    ```
    """
    readme = readme_path(ctx)
    if not readme.exists():
        yield Outcome("skip", "No README.md to read")
        return
    try:
        commands = readme_commands(readme)
    except (OSError, UnicodeDecodeError) as e:
        yield Outcome("skip", f"Could not read {readme.name}: {e}")
        return
    tasks = package_tasks(ctx)
    checked = 0
    reported: set[tuple[int, str, str]] = set()
    for command in commands:
        for task_words, arguments in _task_runs(command):
            for task_word in task_words:
                signatures = _resolve(ctx, task_word.text, readme, tasks)
                if not signatures or any(s.accepts_any for s in signatures):
                    continue
                task = task_word.text.rpartition("@")[2].rpartition("/")[2]
                for argument in arguments:
                    name = argument.text.partition("=")[0].strip()
                    if "=" not in argument.text or not name:
                        continue
                    checked += 1
                    if any(name in s.parameters for s in signatures):
                        continue
                    key = (argument.line, name, task)
                    if key in reported:
                        continue
                    reported.add(key)
                    yield Diagnostic(
                        f"README passes -T {name}=... to {task}(), which has no parameter {name!r}",
                        file=readme,
                        line=argument.line,
                        hint=_parameter_hint(task, signatures),
                    )
    if reported:
        return
    if checked:
        yield Outcome("pass", f"All {checked} -T argument(s) in README commands name a parameter")
    else:
        yield Outcome("skip", "No -T arguments to tasks in this package in the README")


DependencyKind = Literal["extra", "group"]

UV_DEPENDENCY_OPTIONS: dict[str, DependencyKind] = {"--extra": "extra", "--group": "group"}
UV_PROJECT_OPTIONS = ("--project", "--directory", "--package")
"""Options that point uv at another project or workspace member, whose pyproject this rule does not read."""
CHANGE_DIRECTORY = frozenset({"cd", "pushd"})
PIP = re.compile(r"^pip(\d+(\.\d+)?)?$")
REQUIREMENT_EXTRAS = re.compile(
    r"^(?P<project>\.|[A-Za-z0-9][A-Za-z0-9._-]*)\[(?P<extras>[^\]]*)\]"
)
DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Project:
    """The extras and dependency groups a ``pyproject.toml`` defines, by normalised name."""

    pyproject: Path
    name: str
    extras: frozenset[str]
    groups: frozenset[str]

    def defines(self, kind: DependencyKind, name: str) -> bool:
        return normalize_name(name) in (self.extras if kind == "extra" else self.groups)


def _project(pyproject: Path) -> Project | None:
    """What ``pyproject`` defines; None when it is missing or not valid TOML."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project", {})
    return Project(
        pyproject=pyproject,
        name=normalize_name(str(project.get("name", ""))),
        extras=frozenset(normalize_name(str(n)) for n in project.get("optional-dependencies", {})),
        groups=frozenset(normalize_name(str(n)) for n in data.get("dependency-groups", {})),
    )


def governing_pyproject(ctx: LintContext) -> tuple[Path, bool]:
    """The ``pyproject.toml`` that declares the package's dependencies, and whether it is an isolated package's.

    ``<isolated-packages-dir>/<name>/pyproject.toml`` when that exists, else the
    repository's own.
    """
    if ctx.config.isolated_packages_dir:
        isolated = ctx.root / ctx.config.isolated_packages_dir / ctx.name / "pyproject.toml"
        if isolated.is_file():
            return isolated, True
    return ctx.root / "pyproject.toml", False


@dataclass(frozen=True)
class DependencyReference:
    """An extra or group a README command asks for, and how it was written."""

    word: Word
    kind: DependencyKind
    name: str
    written: str
    """``--extra x``, ``--group x`` or ``inspect_evals[x]``."""
    project: Project


def _uv_references(command: Command, governing: Project) -> Iterator[DependencyReference]:
    if not any(Path(w.text).name == "uv" for w in command):
        return
    if any(w.text.partition("=")[0] in UV_PROJECT_OPTIONS for w in command):
        return
    for j, word in enumerate(command):
        option, equals, value = word.text.partition("=")
        kind = UV_DEPENDENCY_OPTIONS.get(option)
        if kind is None:
            continue
        if not equals:
            if j + 1 >= len(command):
                continue
            value = command[j + 1].text
        yield DependencyReference(word, kind, value, f"{option} {value}", governing)


def _installed_from(command: Command) -> int:
    """Where the requirements of a ``pip install`` or ``uv add`` start; past the end for any other command."""
    for i, word in enumerate(command[1:], start=1):
        installer = Path(command[i - 1].text).name
        if (word.text == "install" and PIP.match(installer)) or (
            word.text == "add" and installer == "uv"
        ):
            return i + 1
    return len(command)


def _requirement_references(
    command: Command, governing: Project, root: Project | None
) -> Iterator[DependencyReference]:
    for word in command[_installed_from(command) :]:
        match = REQUIREMENT_EXTRAS.match(word.text)
        if match is None:
            continue
        requirement = match.group("project")
        if requirement == "." or normalize_name(requirement) == governing.name:
            project = governing
        elif root is not None and normalize_name(requirement) == root.name:
            project = root
        else:
            continue
        for extra in match.group("extras").split(","):
            name = extra.strip()
            yield DependencyReference(word, "extra", name, f"{requirement}[{name}]", project)


def _dependency_hint(reference: DependencyReference, isolated_dir: str | None) -> str:
    """What to do instead; ``isolated_dir`` is set when the reference is to an isolated package's pyproject."""
    other: DependencyKind = "group" if reference.kind == "extra" else "extra"
    if reference.project.defines(other, reference.name):
        if reference.written.startswith("--"):
            return f"{reference.name!r} is a dependency {other}: use --{other} {reference.name}"
        return f"{reference.name!r} is a dependency group: use uv sync --group {reference.name}"
    if isolated_dir is not None:
        return (
            f"this evaluation is an isolated package: install it with `uv sync` in {isolated_dir}/ "
            f"or `pip install {isolated_dir}/`, then run it from that environment"
        )
    section = (
        "[project.optional-dependencies]" if reference.kind == "extra" else "[dependency-groups]"
    )
    return f"name one under {section}, or drop it if the evaluation needs nothing extra"


def _commands_in_place(blocks: list[list[Command]]) -> Iterator[Command]:
    """The commands of each block up to its first ``cd``, after which the directory is unknown."""
    for block in blocks:
        for command in block:
            if any(word.text in CHANGE_DIRECTORY for word in command[:2]):
                break
            yield command


@rule(
    code="IEFS008",
    name="readme_dependency_groups",
    category="file_structure",
    summary="Every extra and dependency group a README command installs exists in the governing pyproject.toml",
)
def readme_dependency_groups(ctx: LintContext) -> Iterable[Finding]:
    """Every extra and dependency group a README command installs exists in the governing ``pyproject.toml``.

    ## What it does
    Reads the commands in the README's fenced code blocks and inline code. In
    a ``uv`` command, every ``--extra X`` must name an extra under
    ``[project.optional-dependencies]`` and every ``--group X`` a group under
    ``[dependency-groups]``. In a ``pip install`` (also ``pip3`` and ``uv pip``)
    or ``uv add`` command, every ``project[X]`` must name an extra of that
    project, when the project is this repository's or ``.``. Fails once per
    missing name.

    The governing ``pyproject.toml`` is the repository's, or, with
    ``isolated-packages-dir`` set, ``<isolated-packages-dir>/<name>/pyproject.toml``
    when the evaluation has one. A ``uv`` command with ``--project``,
    ``--directory`` or ``--package`` points elsewhere and is not checked, and
    neither is any command after a ``cd`` in the same code block. A
    ``pyproject.toml`` that is not valid TOML skips the rule.

    ## Why is this bad?
    ``uv sync --extra X`` fails outright when there is no extra ``X``, and the
    reader is left to guess what to install. An evaluation that moves to its
    own package, or loses its extra, keeps the old install line, because a
    README generator that rewrites its own sections does not read the
    hand-written instructions around them.

    ## Example
    ```bash
    uv sync --extra my_eval  # pyproject.toml has no my_eval extra
    ```
    Use instead:
    ```bash
    uv sync
    ```

    ## Options
    - `isolated-packages-dir`
    """
    readme = readme_path(ctx)
    if not readme.exists():
        yield Outcome("skip", "No README.md to read")
        return
    pyproject, isolated = governing_pyproject(ctx)
    if not pyproject.is_file():
        yield Outcome("skip", "No pyproject.toml declares this evaluation's dependencies")
        return
    governing = _project(pyproject)
    if governing is None:
        yield Outcome(
            "skip", f"Could not parse {pyproject.relative_to(ctx.root).as_posix()} as TOML"
        )
        return
    try:
        blocks = readme_blocks(readme)
    except (OSError, UnicodeDecodeError) as e:
        yield Outcome("skip", f"Could not read {readme.name}: {e}")
        return
    root = _project(ctx.root / "pyproject.toml")
    isolated_dir = pyproject.parent.relative_to(ctx.root).as_posix() if isolated else None
    checked = 0
    failed = False
    for command in _commands_in_place(blocks):
        references = [
            *_uv_references(command, governing),
            *_requirement_references(command, governing, root),
        ]
        for reference in references:
            if not DEPENDENCY_NAME.match(reference.name):
                continue
            checked += 1
            if reference.project.defines(reference.kind, reference.name):
                continue
            failed = True
            where = reference.project.pyproject.relative_to(ctx.root).as_posix()
            isolated_reference = isolated_dir if reference.project is governing else None
            if isolated_reference is not None:
                where += ", which governs this evaluation,"
            yield Diagnostic(
                f"README asks for {reference.written}, but {where} defines no "
                f"{reference.kind} {reference.name!r}",
                file=readme,
                line=reference.word.line,
                hint=_dependency_hint(reference, isolated_reference),
            )
    if failed:
        return
    if checked:
        yield Outcome("pass", f"All {checked} extra(s) and group(s) in README commands exist")
    else:
        yield Outcome("skip", "No extras or dependency groups in README commands")
