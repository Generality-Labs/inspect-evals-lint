"""Dockerfile rule: sandbox builds consume locked inputs.

A digest-pinned published image does not make its *build* reproducible: the
Dockerfile can still install whatever a registry or index serves on the day.
The rule reads each Dockerfile statically, never executing anything, and
reports build inputs that are not locked. Findings are warnings in this
release so consumers can migrate before any of them fail.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast, get_args

import yaml

from inspect_evals_lint.config import ConfigError
from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import iter_dockerfiles

RULE_NAME = "dockerfile_locking"

HostLockCoupling = Literal["warn", "allow"]
HOST_LOCK_COUPLING_MODES: tuple[str, ...] = get_args(HostLockCoupling)
DEFAULT_HOST_LOCK_COUPLING: HostLockCoupling = "allow"
OPTION_TABLE = f"[tool.inspect-evals-lint.{RULE_NAME}]"

_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIRECTIVE = re.compile(r"^#\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_HEREDOC = re.compile(r"<<-?['\"]?(\w+)['\"]?")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_SHELLS = frozenset({"sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/usr/bin/env"})
_OS_PACKAGE_MANAGERS = frozenset({"apt-get", "apt", "apk", "yum", "dnf", "microdnf", "zypper"})
_UNSUPPORTED_PACKAGE_MANAGERS = frozenset(
    {"npm", "yarn", "pnpm", "cargo", "gem", "go", "conda", "mamba", "poetry", "pipenv", "pdm"}
)
_BOOTSTRAP_PACKAGES = frozenset({"pip", "uv", "setuptools", "wheel", "poetry", "pipenv"})
_PIP_VALUE_OPTIONS = frozenset(
    {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--target",
        "-t",
        "--prefix",
        "--root",
        "--platform",
        "--python-version",
        "--implementation",
        "--abi",
        "--progress-bar",
        "--timeout",
        "--retries",
        "--proxy",
        "--cache-dir",
        "--log",
        "--config-settings",
        "-C",
    }
)

LOCK_HINT = (
    "install a fully resolved, hashed snapshot (pip install --require-hashes -r ...) or "
    "consume a committed uv.lock with uv sync --locked"
)
TOOL_IMAGE_HINT = (
    "COPY the tool from a digest-pinned image instead "
    "(COPY --from=ghcr.io/astral-sh/uv:<tag>@sha256:<digest> /uv /usr/local/bin/uv)"
)


def host_lock_coupling(options: Mapping[str, object]) -> HostLockCoupling:
    """The ``host-lock-coupling`` option from the rule's table, validated.

    Raises:
        ConfigError: the table has an unknown key or the value is not ``warn`` or ``allow``.
    """
    unknown = sorted(set(options) - {"host_lock_coupling"})
    if unknown:
        raise ConfigError(
            f"{OPTION_TABLE}: unknown option(s) {unknown}; the only option is 'host-lock-coupling'"
        )
    value = options.get("host_lock_coupling", DEFAULT_HOST_LOCK_COUPLING)
    if not isinstance(value, str) or value not in HOST_LOCK_COUPLING_MODES:
        raise ConfigError(
            f"{OPTION_TABLE}: 'host-lock-coupling' must be one of "
            f"{list(HOST_LOCK_COUPLING_MODES)}, got {value!r}"
        )
    return cast(HostLockCoupling, value)


@dataclass
class Instruction:
    """One Dockerfile instruction with its leading ``--flag=value`` options split out."""

    keyword: str
    args: str
    line: int
    flags: dict[str, str] = field(default_factory=dict)
    mounts: list[str] = field(default_factory=list)


def parse_dockerfile(path: Path) -> tuple[list[Instruction], dict[str, str]]:
    """Parse ``path`` into instructions plus the ``# KEY=value`` directives above the first one.

    Line continuations are joined and comment lines inside them dropped; each
    instruction keeps the line number it starts on so findings and inline
    suppressions can point at it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    instructions: list[Instruction] = []
    directives: dict[str, str] = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            if not instructions:
                match = _DIRECTIVE.match(stripped)
                if match:
                    directives[match.group(1)] = match.group(2)
            i += 1
            continue
        start = i + 1
        pieces: list[str] = []
        while i < len(lines):
            piece = lines[i].rstrip()
            i += 1
            if pieces and piece.strip().startswith("#"):
                continue
            if piece.endswith("\\"):
                pieces.append(piece[:-1].strip())
                continue
            pieces.append(piece.strip())
            break
        text = " ".join(p for p in pieces if p)
        keyword, _, rest = text.partition(" ")
        instruction = Instruction(keyword=keyword.upper(), args="", line=start)
        rest = rest.strip()
        while rest.startswith("--"):
            token, _, remainder = rest.partition(" ")
            key, _, value = token[2:].partition("=")
            if key == "mount":
                instruction.mounts.append(value)
            instruction.flags[key] = value
            rest = remainder.strip()
        instruction.args = rest
        instructions.append(instruction)
        # BuildKit heredocs: the body lines up to the terminator belong to this
        # instruction and are not Dockerfile instructions themselves.
        for marker in _HEREDOC.findall(text):
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            i += 1
    return instructions, directives


def _compose_build_contexts(package_path: Path) -> dict[Path, Path]:
    """Map each Dockerfile a compose file builds to the ``build.context`` it declares.

    Compose resolves ``build.dockerfile`` relative to ``build.context``, and
    ``build.context`` relative to the compose file. Interpolated values are skipped.
    """
    mapping: dict[Path, Path] = {}
    for compose_file in sorted(package_path.rglob("compose*.y*ml")):
        try:
            data: object = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        services: object = cast(dict[str, object], data).get("services")
        if not isinstance(services, dict):
            continue
        for raw_service in cast(dict[str, object], services).values():
            if not isinstance(raw_service, dict):
                continue
            build: object = cast(dict[str, object], raw_service).get("build")
            if isinstance(build, str):
                context_value, dockerfile_value = build, "Dockerfile"
            elif isinstance(build, dict):
                build_table = cast(dict[str, object], build)
                context_value = str(build_table.get("context", "."))
                dockerfile_value = str(build_table.get("dockerfile", "Dockerfile"))
            else:
                continue
            if "$" in context_value or "$" in dockerfile_value:
                continue
            context_dir = (compose_file.parent / context_value).resolve()
            mapping.setdefault((context_dir / dockerfile_value).resolve(), context_dir)
    return mapping


def split_shell_commands(args: str) -> list[list[str]]:
    """Split a RUN payload (shell or exec form) into simple commands as token lists.

    ``&&``, ``||``, ``;``, ``|`` and ``&`` separate commands; the pipe boundary is
    not preserved, so a caller detects ``curl ... | sh`` as adjacent commands.
    Returns ``[]`` when the payload cannot be tokenised.
    """
    text = args.strip()
    if text.startswith("["):
        try:
            items: object = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        raw_items: list[object] = cast(list[object], items)
        words: list[str] = [x for x in raw_items if isinstance(x, str)]
        if len(words) != len(raw_items):
            return []
        if len(words) >= 3 and words[0] in _SHELLS and words[1] == "-c":
            text = words[2]
        else:
            return [words]
    lexer = shlex.shlex(text, posix=True, punctuation_chars="&|;()")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    commands: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_OPERATORS or (token and set(token) <= set("&|;()")):
            if current:
                commands.append(current)
            current = []
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands


def _normalise(command: list[str]) -> list[str]:
    """Drop env assignments, ``sudo`` and ``set -e`` noise; rewrite ``python -m pip`` to ``pip``."""
    tokens = list(command)
    while tokens and (_ENV_ASSIGNMENT.match(tokens[0]) or tokens[0] in ("sudo", "exec", "env")):
        tokens.pop(0)
    if tokens and tokens[0] == "set":
        return []
    if len(tokens) >= 3 and tokens[0].startswith("python") and tokens[1] == "-m":
        tokens = tokens[2:]
    if tokens and tokens[0] in ("pip3", "pip"):
        tokens[0] = "pip"
    return tokens


@dataclass
class _Finding:
    line: int
    message: str
    hint: str | None = None
    limit: bool = False
    """True for inputs this rule cannot lock (OS packages); named in the pass message, not warned about."""


@dataclass
class _BuildState:
    """What the build has made available so far, tracked instruction by instruction."""

    context: Path
    repo_root: Path
    stages: set[str] = field(default_factory=set)
    lock_available: bool = False
    lock_unverified: bool = False
    host_coupling_lines: list[int] = field(default_factory=list)
    findings: list[_Finding] = field(default_factory=list)

    def warn(self, line: int, message: str, hint: str | None = None) -> None:
        self.findings.append(_Finding(line, message, hint))

    def limit(self, line: int, message: str) -> None:
        self.findings.append(_Finding(line, message, limit=True))


def _image_problem(reference: str, state: _BuildState) -> tuple[str, str] | None:
    """Message and hint for an image reference that is not a fixed build input."""
    if reference == "scratch" or reference in state.stages:
        return None
    if "$" in reference:
        return (
            f"image reference '{reference}' is dynamic and cannot be verified statically",
            "use a literal reference carrying an @sha256 digest",
        )
    if "@sha256:" not in reference:
        return (
            f"image '{reference}' is not digest-pinned, and a tag alone can be re-pushed",
            "append @sha256:<digest> to the reference to fix the build input",
        )
    return None


def _register_source(source: str, line: int, state: _BuildState, via: str) -> None:
    """Resolve a COPY/ADD/bind-mount source against the build context and record what it provides."""
    if "$" in source or source.startswith(("http://", "https://")):
        state.lock_unverified = True
        state.warn(
            line,
            f"{via} source '{source}' is dynamic or remote and cannot be verified statically",
            "copy a file committed in the build context instead",
        )
        return
    try:
        resolved = (state.context / source).resolve()
        repo = state.repo_root.resolve()
    except OSError:
        state.lock_unverified = True
        return
    if not resolved.is_relative_to(repo):
        state.warn(
            line,
            f"{via} source '{source}' resolves outside the repository and cannot be verified",
            "keep build inputs inside the repository",
        )
        state.lock_unverified = True
        return
    if not resolved.exists():
        context_rel = (
            state.context.resolve().relative_to(repo)
            if state.context.resolve() != repo
            else Path(".")
        )
        state.warn(
            line,
            f"{via} source '{source}' does not exist in the build context '{context_rel}', "
            "so the build would fail or fall back to an unlocked install",
            "fix the path, or the BUILD_CONTEXT directive it is relative to",
        )
        return
    if resolved.name == "uv.lock" or (resolved.is_dir() and (resolved / "uv.lock").exists()):
        state.lock_available = True
    host_files = {(repo / "uv.lock"), (repo / "pyproject.toml")}
    if resolved in host_files or (resolved == repo and (repo / "uv.lock").exists()):
        state.host_coupling_lines.append(line)


def _pip_install(args: list[str], line: int, state: _BuildState) -> None:
    require_hashes = "--require-hashes" in args
    no_deps = "--no-deps" in args
    requirement_files: list[str] = []
    targets: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in ("-r", "--requirement"):
            skip_next = True
            index = args.index(token)
            if index + 1 < len(args):
                requirement_files.append(args[index + 1])
            continue
        if token.startswith(("-r=", "--requirement=")):
            requirement_files.append(token.split("=", 1)[1])
            continue
        if token in _PIP_VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        targets.append(token)

    git_targets = [t for t in targets if "git+" in t or t.endswith(".git")]
    for target in git_targets:
        ref = target.rsplit("@", 1)[1] if "@" in target else ""
        ref = ref.split("#", 1)[0]
        if not _FULL_COMMIT.match(ref):
            state.warn(
                line,
                f"git dependency '{target}' is not pinned to a full commit, and a branch or tag can move",
                "pin the commit, or consume it through a lock such as uv.lock "
                "(pip hash-checking cannot cover VCS installs)",
            )
    local_targets = [t for t in targets if t == "." or t.startswith(("./", "/")) or t == "-e"]
    packages = [t for t in targets if t not in git_targets and t not in local_targets]
    dynamic = [p for p in packages if "$" in p or "{" in p]
    if dynamic:
        state.warn(
            line,
            f"package install target(s) {', '.join(dynamic)} are templated or dynamic and "
            "cannot be verified statically",
            LOCK_HINT,
        )
        packages = [p for p in packages if p not in dynamic]
        if not packages and not requirement_files and not local_targets:
            return

    if require_hashes and requirement_files and not packages and not local_targets:
        return
    if no_deps and local_targets and not packages and not requirement_files:
        return
    if requirement_files and not require_hashes:
        state.warn(
            line,
            f"requirements file(s) {', '.join(requirement_files)} installed without "
            "--require-hashes, so transitive dependencies are not locked",
            LOCK_HINT,
        )
    if packages:
        bootstrap = [
            p
            for p in packages
            if re.split(r"[=<>!~\[]", p, maxsplit=1)[0].lower() in _BOOTSTRAP_PACKAGES
        ]
        if bootstrap:
            state.warn(
                line,
                f"unlocked package-manager bootstrap: {', '.join(packages)}; without a hashed "
                "snapshot or a consumed lock the resolved versions change between builds",
                TOOL_IMAGE_HINT,
            )
        else:
            state.warn(
                line,
                f"unlocked package install: {', '.join(packages)}; without a hashed snapshot "
                "or a consumed lock the resolved versions change between builds",
                LOCK_HINT,
            )
    if local_targets and not no_deps:
        state.warn(
            line,
            f"installs the project ({', '.join(local_targets)}) with dependency resolution",
            "install locked dependencies first, then the project with --no-deps",
        )


def _uv_command(command: list[str], line: int, state: _BuildState) -> None:
    sub = command[1] if len(command) > 1 else ""
    if sub == "lock":
        state.warn(
            line,
            "generates or updates the lock during the build (uv lock)",
            "commit uv.lock and consume it with uv sync --locked instead",
        )
        return
    if sub == "sync":
        if "--locked" not in command and "--frozen" not in command:
            state.warn(
                line,
                "uv sync without --locked may update uv.lock during the build",
                "use uv sync --locked",
            )
        elif "--frozen" in command:
            state.warn(
                line,
                "uv sync --frozen skips lock freshness validation",
                "use --locked, or check freshness separately with uv lock --check",
            )
        if not state.lock_available:
            if state.lock_unverified:
                state.warn(
                    line,
                    "cannot verify that uv.lock is available to uv sync (a dynamic COPY or mount source)",
                    "copy the lock from a literal path in the build context",
                )
            else:
                state.warn(
                    line,
                    "uv.lock is not copied or bind-mounted into the build before uv sync, so "
                    "no committed lock is consumed",
                    "COPY uv.lock alongside pyproject.toml, or bind-mount it, before uv sync",
                )
        return
    if sub == "pip" and len(command) > 2 and command[2] == "install":
        _pip_install(command[3:], line, state)
        return
    if sub in ("add", "tool") or (sub == "python" and len(command) > 2 and command[2] == "install"):
        state.warn(
            line,
            f"uv {sub} resolves dependencies at build time",
            "declare them in the sandbox project and consume its uv.lock with uv sync --locked",
        )


def _classify(
    command: list[str], previous: list[str] | None, line: int, state: _BuildState
) -> None:
    command = _normalise(command)
    if not command:
        return
    name = command[0]
    if (
        name in _SHELLS
        and previous
        and _normalise(previous)[:1]
        and _normalise(previous)[0] in ("curl", "wget")
    ):
        state.warn(
            line,
            "installer script downloaded and piped to a shell without integrity verification",
            "COPY the tool from a digest-pinned image or verify a checksum before running it",
        )
        return
    if name == "uv":
        _uv_command(command, line, state)
    elif name == "pip" and len(command) > 1 and command[1] == "install":
        _pip_install(command[2:], line, state)
    elif name in _OS_PACKAGE_MANAGERS and any(t in ("install", "add") for t in command[1:]):
        state.limit(line, f"OS package installation ({name}) at line {line}")
    elif name in _UNSUPPORTED_PACKAGE_MANAGERS:
        state.warn(
            line,
            f"dependency installation with {name} cannot be verified by this rule",
            "only pip and uv installs are understood; lock these inputs by other means",
        )
    elif (
        name == "git"
        and len(command) > 1
        and command[1] == "clone"
        and not any(_FULL_COMMIT.match(t) for t in command)
    ):
        state.warn(
            line,
            "git clone at build time without a full commit checkout, and a branch or tag can move",
            "check out a full 40-character commit",
        )


def _analyse(
    dockerfile: Path,
    repo_root: Path,
    coupling: HostLockCoupling,
    compose_contexts: dict[Path, Path],
) -> list[_Finding]:
    instructions, directives = parse_dockerfile(dockerfile)
    if "BUILD_CONTEXT" in directives:
        context = repo_root / directives["BUILD_CONTEXT"]
    else:
        context = compose_contexts.get(dockerfile.resolve(), dockerfile.parent)
    state = _BuildState(context=context, repo_root=repo_root)
    if not context.resolve().is_relative_to(repo_root.resolve()):
        state.warn(
            1,
            f"BUILD_CONTEXT '{directives['BUILD_CONTEXT']}' points outside the repository, "
            "so nothing can be verified",
            "point it at a directory inside the repository",
        )
        return state.findings

    for instruction in instructions:
        keyword = instruction.keyword
        if keyword == "FROM":
            tokens = instruction.args.split()
            if not tokens:
                continue
            image = tokens[0]
            problem = _image_problem(image, state)
            if problem:
                state.warn(instruction.line, f"FROM: {problem[0]}", problem[1])
            if len(tokens) >= 3 and tokens[1].upper() == "AS":
                state.stages.add(tokens[2])
        elif keyword in ("COPY", "ADD"):
            source_image = instruction.flags.get("from")
            if source_image is not None:
                problem = _image_problem(source_image, state)
                if problem:
                    state.warn(instruction.line, f"{keyword} --from: {problem[0]}", problem[1])
                continue
            try:
                parts = (
                    json.loads(instruction.args)
                    if instruction.args.startswith("[")
                    else shlex.split(instruction.args)
                )
            except (ValueError, json.JSONDecodeError):
                parts = instruction.args.split()
            if any(str(part).startswith("<<") for part in parts):
                continue
            for source in parts[:-1]:
                _register_source(str(source), instruction.line, state, keyword)
        elif keyword == "RUN":
            for mount in instruction.mounts:
                options = dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
                if options.get("type") == "bind" and options.get("source"):
                    _register_source(options["source"], instruction.line, state, "bind mount")
            previous: list[str] | None = None
            for command in split_shell_commands(instruction.args):
                _classify(command, previous, instruction.line, state)
                previous = command

    if coupling == "warn":
        for line in sorted(set(state.host_coupling_lines)):
            state.warn(
                line,
                "the build consumes the host project's pyproject.toml/uv.lock, so unrelated "
                "host dependency updates change this image",
                "give the sandbox its own project and lock, or set host-lock-coupling = "
                f'"allow" in {OPTION_TABLE} for a standalone repository',
            )
    # A RUN with two apt-get installs is one finding, not two identical ones.
    unique: list[_Finding] = []
    seen: set[tuple[int, str]] = set()
    for finding in state.findings:
        key = (finding.line, finding.message)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


@rule(
    code="IEBP007",
    name=RULE_NAME,
    category="best_practices",
    summary="Dockerfile builds consume locked inputs: committed locks, digest-pinned images, fixed sources",
    references=(
        inspect_docs("sandboxing", "Sandboxing: Task Configuration", "task-configuration"),
        inspect_docs("sandboxing", "Sandboxing: Prebuilt Images", "prebuilt-images"),
    ),
)
def dockerfile_locking(ctx: LintContext) -> Iterable[Finding]:
    """Dockerfile builds consume locked inputs: committed locks, digest-pinned images, fixed sources.

    ## What it does
    Reads every ``Dockerfile*`` under the evaluation statically (nothing is executed)
    and reports one warning per instruction with a build input that is not locked,
    pointing at the instruction's first line:

    - Dependency installs must consume a committed lock or hashed snapshot:
      ``uv sync --locked`` with ``uv.lock`` copied or bind-mounted in beforehand, or
      ``pip install --require-hashes -r <snapshot>``. ``uv sync`` alone (may update
      the lock), ``uv sync --frozen`` (skips freshness validation), ``uv lock`` during
      the build, ``pip install <packages>``, requirements files without
      ``--require-hashes``, and installing the project with dependency resolution all
      warn; ``pip install . --no-deps`` after locked dependencies is accepted.
      Manifests are not inspected: ranges there are fine when the consumed lock
      resolves them.
    - Image and source inputs must be immutable: ``FROM`` and ``COPY --from``
      references need an ``@sha256`` digest (``scratch`` and earlier build stages are
      exempt), Git dependencies on a pip command line need a full commit, and an
      installer script piped from ``curl`` or ``wget`` into a shell warns because
      nothing verifies it. ``COPY --from`` a digest-pinned tool image is the accepted
      way to bring in ``uv``.
    - Dynamic references (``${VAR}`` images, remote or interpolated ``COPY`` sources,
      templated package names) and unsupported package managers (``npm``, ``cargo``,
      ``conda``, ...) are reported as unverified, never as passing.
    - ``COPY`` sources resolve against the build context: the
      ``# BUILD_CONTEXT=<path relative to the repository root>`` directive inspect_evals
      uses, else the ``build.context`` of a compose service that builds the Dockerfile
      (compose resolves ``dockerfile`` relative to it), else the Dockerfile's
      directory. Missing sources and sources outside the repository warn. BuildKit
      heredocs are skipped and ``.dockerignore`` is not consulted.

    Limits, kept visible: OS package installs (``apt-get install`` and the like)
    cannot be locked by this rule, so a Dockerfile whose other inputs are locked
    still passes, with each such step named in the pass message. Build-isolation
    dependencies of source builds and the interpreter version against the sandbox
    project's ``requires-python`` are not checked. Passing means the supported
    installs consume locked inputs, not that a rebuild is byte-identical. Every
    finding is a warning in this release. Dockerfiles matched by ``exclude`` are not
    read; an evaluation without a Dockerfile skips.

    ## Why is this bad?
    A digest-pinned published image fixes what runs today, but not what a rebuild
    produces: a Dockerfile that installs whatever the index or registry serves on
    the day gives a different sandbox each time the image is rebuilt, so results
    stop being comparable without anything in the repository changing, and a
    broken upstream release can turn into errored samples. Consuming a committed
    lock makes the rebuild a function of the repository alone.

    ## Example
    ```dockerfile
    FROM python:3.12-slim
    COPY pyproject.toml ./
    RUN curl -LsSf https://astral.sh/uv/install.sh | sh && uv sync
    ```
    Use instead:
    ```dockerfile
    # BUILD_CONTEXT=.
    FROM python:3.12-slim@sha256:<digest>
    COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:<digest> /uv /usr/local/bin/uv
    COPY src/inspect_evals/my_eval/sandbox/pyproject.toml src/inspect_evals/my_eval/sandbox/uv.lock ./
    RUN uv sync --locked --no-dev --no-install-project
    ```

    ## Options
    - `dockerfile_locking.host-lock-coupling`: `"warn"` reports a build that copies the repository root's `pyproject.toml` or `uv.lock`, because unrelated host dependency updates would then change the sandbox image; `"allow"` accepts it, for a standalone repository whose root project is the sandbox. `warn` in the `monorepo` preset, `allow` elsewhere.
    """
    coupling = host_lock_coupling(ctx.config.rule_options.get(RULE_NAME, {}))
    dockerfiles = iter_dockerfiles(ctx)
    if not dockerfiles:
        yield Outcome("skip", "No Dockerfile found")
        return

    compose_contexts = _compose_build_contexts(ctx.path)
    warned = 0
    limits: list[str] = []
    for dockerfile in dockerfiles:
        for finding in _analyse(dockerfile, ctx.root, coupling, compose_contexts):
            if finding.limit:
                limits.append(f"{dockerfile.name}: {finding.message}")
                continue
            warned += 1
            yield Diagnostic(
                finding.message,
                file=dockerfile,
                line=finding.line,
                severity="warning",
                hint=finding.hint,
            )
    if warned == 0:
        message = (
            f"{len(dockerfiles)} Dockerfile(s): dependency installs consume locked "
            "inputs and image references are digest-pinned"
        )
        if limits:
            message += (
                "; not lockable by this rule, so the base digest does not freeze them: "
                + "; ".join(limits)
            )
        yield Outcome("pass", message)
