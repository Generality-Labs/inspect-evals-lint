"""README rules: the commands a README shows agree with the package they document."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import parse_python_files
from inspect_evals_lint.rules.best_practices import TaskDefaultsVisitor
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


def readme_commands(readme: Path) -> list[Command]:
    """Every shell command in the README's fenced code blocks and inline code spans.

    Raises:
        OSError: the README cannot be read.
        UnicodeDecodeError: the README is not UTF-8.
    """
    commands: list[Command] = []
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
                commands.extend(_commands([(number, span.group(1))]))
        elif line.strip().startswith(fence[0] * len(fence)) and not line.strip().strip(fence[0]):
            commands.extend(_commands(block))
            fence = None
        else:
            block.append((number, line))
    if fence is not None:
        commands.extend(_commands(block))
    return commands


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
    warning. A README that shows ``-T max_messages=75`` for a task whose
    parameter is ``message_limit`` runs with the default, and readers copy these
    commands. A
    README generator that rewrites a generated parameter list does not read the
    hand-written examples around it, so a renamed parameter leaves them behind.

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
