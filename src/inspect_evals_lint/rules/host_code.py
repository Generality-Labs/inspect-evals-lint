"""Host-side execution of model-controlled input: exec, eval, subprocess, unsafe deserialisation.

The analysis is a taint check over one file's AST. Sources are the values a
model controls by construction; sinks are the calls that run or deserialise
their argument on the host. Within a function a name is tainted if any
assignment to it is, and an expression is tainted if anything inside it is.
Calls to functions in the same file are followed one level, into their
parameters and out through their return values; nothing crosses files.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import (
    column_of,
    end_line_of,
    get_call_name,
    get_decorator_name,
    parse_failures,
    parse_python_files,
)

RULE_NAME = "host_code_execution"

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

SinkKind = Literal["code", "data", "shell", "argv", "command", "program", "import"]
"""How a sink's payload is judged.

``code``, ``data`` and ``shell`` always report; ``argv`` is a ``subprocess``
call that is a shell sink with ``shell=`` and a program sink without;
``command`` takes a string (run by a shell) or a list; ``program`` and
``import`` report only when tainted.
"""


@dataclass(frozen=True)
class _Sink:
    """A call that runs its argument on the host, and where in the call that argument is."""

    name: str
    """How findings and allowlist keys name it: ``eval``, ``subprocess.run``."""
    kind: SinkKind
    keywords: tuple[str, ...] = ()
    """Keyword spellings of the positional argument that carries the payload."""


def _sinks(kind: SinkKind, names: Iterable[str], *keywords: str) -> dict[str, _Sink]:
    return {name: _Sink(name, kind, keywords) for name in names}


_BUILTIN_SINKS = frozenset({"exec", "eval", "compile", "__import__"})

_YAML_LOADS_WITH_LOADER = frozenset({"yaml.load", "yaml.load_all"})

SINKS: dict[str, _Sink] = {
    **{f"builtins.{name}": _Sink(name, "code", ("source", "name")) for name in _BUILTIN_SINKS},
    **_sinks(
        "data",
        (
            "pickle.loads",
            "pickle.load",
            *_YAML_LOADS_WITH_LOADER,
            "yaml.unsafe_load",
            "yaml.unsafe_load_all",
            "yaml.full_load",
            "yaml.full_load_all",
            "torch.load",
        ),
        "data",
        "file",
        "stream",
        "f",
    ),
    **_sinks(
        "shell",
        ("os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput"),
        "command",
        "cmd",
    ),
    **_sinks("shell", ("asyncio.create_subprocess_shell",), "cmd"),
    **_sinks(
        "argv",
        (
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
        ),
        "args",
    ),
    **_sinks("command", ("inspect_ai.util.subprocess",), "args"),
    **_sinks("program", ("asyncio.create_subprocess_exec",), "program"),
    **_sinks("import", ("importlib.import_module",), "name"),
}
"""Sinks by qualified name. Bare builtins resolve to ``builtins.<name>`` unless an import in force rebinds the name."""

_SAFE_YAML_LOADERS = frozenset({"SafeLoader", "CSafeLoader", "BaseLoader"})

_FAIL_TEXT: dict[str, str] = {
    "code": "executes model-controlled code on the host",
    "data": "deserialises model-controlled data on the host",
    "shell": "runs a model-controlled shell command on the host",
    "program": "runs a model-controlled program on the host",
    "import": "imports a model-controlled module on the host",
}

_WARN_TEXT: dict[str, str] = {
    "code": "executes code on the host",
    "data": "deserialises data on the host",
    "shell": "runs a shell command on the host",
}

FAIL_HINT = (
    "run it inside the sandbox with sandbox().exec(), or parse the input without executing it "
    "(ast.literal_eval, json.loads, yaml.safe_load)"
)
WARN_HINT = (
    "check that no model output, tool argument or sandbox content can reach it, then mark the "
    f"site reviewed with `# inspect-evals-lint: ignore[{RULE_NAME}]`"
)
ARGV_HINT = (
    "build the argv as a list literal with a constant program, so only its arguments can come "
    "from the model; otherwise review the site as for a shell command"
)

_MUTATORS = frozenset({"append", "extend", "insert", "update", "add", "setdefault"})
"""Methods that store their arguments in the object they are called on."""


@dataclass
class Hit:
    """A sink call and what reached it."""

    node: ast.Call
    sink: _Sink
    severity: Severity
    message: str
    hint: str
    traced: bool = True
    """Whether a source was traced to the sink; an untraced site is reported where it is analysed on its own."""

    @property
    def site(self) -> tuple[int, int]:
        return (self.node.lineno, self.node.col_offset)


def _import_bindings(node: ast.Import | ast.ImportFrom) -> Iterator[tuple[str, str]]:
    """``(local name, qualified name)`` for each name an import statement binds."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                yield alias.asname, alias.name
            else:
                top = alias.name.split(".")[0]
                yield top, top
    elif node.module and node.level == 0:
        for alias in node.names:
            yield alias.asname or alias.name, f"{node.module}.{alias.name}"


def import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name to the module or object an import binds it to: ``sp`` to ``subprocess``, ``run`` to ``subprocess.run``.

    Taken from every import in the file, wherever it sits. Imports that rebind
    a builtin sink name are left to :func:`builtin_rebindings`, which applies
    them only where they are in force.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for local, qualified in _import_bindings(node):
                if local not in _BUILTIN_SINKS:
                    aliases[local] = qualified
    return aliases


def builtin_rebindings(statements: Iterable[ast.AST]) -> dict[str, str]:
    """Builtin sink names the given import statements rebind: ``{"eval": "inspect_ai.eval"}``."""
    rebound: dict[str, str] = {}
    for node in statements:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for local, qualified in _import_bindings(node):
                if local in _BUILTIN_SINKS:
                    rebound[local] = qualified
    return rebound


def _unconditional_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run whenever the module is imported: the top level and its ``try`` blocks."""
    for statement in body:
        yield statement
        if isinstance(statement, (ast.Try, ast.TryStar)):
            for block in (
                statement.body,
                *(handler.body for handler in statement.handlers),
                statement.orelse,
                statement.finalbody,
            ):
                yield from _unconditional_statements(block)


def qualified_name(func: ast.expr, aliases: dict[str, str], rebound: dict[str, str]) -> str | None:
    """``subprocess.run`` for ``sp.run`` after ``import subprocess as sp``; None when no import binds the name.

    A bare builtin sink name resolves to ``builtins.<name>`` unless ``rebound``
    (the rebinding imports in force at the call) says otherwise, so
    ``from inspect_ai import eval`` exempts ``eval`` and nothing else.
    """
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    if not parts and node.id in _BUILTIN_SINKS:
        return rebound.get(node.id, f"builtins.{node.id}")
    base = aliases.get(node.id)
    if base is None:
        return None
    return ".".join([base, *reversed(parts)])


def dotted(node: ast.expr) -> str | None:
    """``self.code`` for an attribute chain ending in a name; None otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _evaluated_in_enclosing_scope(node: FunctionNode | ast.ClassDef) -> list[ast.expr]:
    """The parts of a definition that run where it is defined: decorators, defaults, class bases."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
    defaults = [d for d in node.args.kw_defaults if d is not None]
    return [*node.decorator_list, *node.args.defaults, *defaults]


def scope_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node a function, class or module body runs, without entering nested bodies.

    A nested definition's decorators, defaults and class bases run here, so they are included.
    """
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(reversed(_evaluated_in_enclosing_scope(node)))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _nested_definitions(body: list[ast.stmt]) -> list[FunctionNode | ast.ClassDef]:
    return [
        node
        for node in scope_nodes(body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _parameters(fn: FunctionNode) -> list[ast.arg]:
    args = fn.args
    extra = [a for a in (args.vararg, args.kwarg) if a is not None]
    return [*args.posonlyargs, *args.args, *args.kwonlyargs, *extra]


def annotation_names(annotation: ast.expr | None) -> set[str]:
    """The type names an annotation mentions: ``ModelOutput | None`` and ``Optional[ModelOutput]`` both give ``ModelOutput``."""
    if annotation is None:
        return set()
    if isinstance(annotation, ast.Name):
        return {annotation.id}
    if isinstance(annotation, ast.Attribute):
        return {annotation.attr}
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return annotation_names(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return set()
    if isinstance(annotation, ast.BinOp):
        return annotation_names(annotation.left) | annotation_names(annotation.right)
    if isinstance(annotation, ast.Subscript):
        inner = annotation.slice
        elements = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        names: set[str] = set()
        for element in elements:
            names |= annotation_names(element)
        return names
    return set()


def _is_staticmethod(fn: FunctionNode) -> bool:
    return any(get_decorator_name(d) == "staticmethod" for d in fn.decorator_list)


def _attribute_source(node: ast.Attribute) -> str | None:
    """What model-controlled value an attribute access reads, if it reads one."""
    if node.attr == "completion":
        return "model output (.completion)"
    if node.attr in ("tool_calls", "messages"):
        return f"model output (.{node.attr})"
    if node.attr == "arguments":
        return "model output (tool call arguments)"
    if (
        node.attr in ("message", "choices")
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "output"
    ):
        return f"model output (.output.{node.attr})"
    if node.attr == "output" and isinstance(node.value, ast.Name) and node.value.id == "state":
        return "model output (state.output)"
    return None


@dataclass
class Scope:
    """One function, class or module body being analysed, and what it can see."""

    body: list[ast.stmt]
    taint: dict[str, str]
    """Dotted names known to hold model-controlled values, with where each came from."""
    functions: dict[str, FunctionNode]
    """Same-file functions callable by bare name from here."""
    methods: dict[str, FunctionNode]
    """Methods of the enclosing class, callable as ``self.<name>`` or ``cls.<name>``."""
    depth: int
    """0 when the body is analysed on its own; 1 when entered through a call, which is not followed further."""
    rebound: dict[str, str]
    """Builtin sink names rebound by imports in force here."""
    inherited_rebound: dict[str, str]
    """What a function defined here starts from: a module's unconditional imports, a function's own."""
    via: str = ""
    sandboxes: set[str] = field(default_factory=set)
    """Dotted names bound to a sandbox environment."""
    enclosing: Scope | None = None
    """For a class body, the scope its methods close over; class-level names are not visible in methods."""

    @property
    def visible(self) -> Scope:
        """The scope a definition nested here closes over."""
        return self.enclosing or self


class FileAnalysis:
    """The taint analysis of one parsed file; ``hits`` holds each sink site at its worst."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.aliases = import_aliases(tree)
        self.global_rebound = builtin_rebindings(_unconditional_statements(tree.body))
        self.safe_loaders = safe_yaml_loaders(tree)
        self.hits: dict[tuple[int, int], Hit] = {}
        self._returns: dict[int, str | None] = {}

    def run(self) -> list[Hit]:
        module_functions = {
            node.name: node
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        module = Scope(
            self.tree.body,
            {},
            module_functions,
            {},
            depth=0,
            rebound={**self.global_rebound, **builtin_rebindings(scope_nodes(self.tree.body))},
            inherited_rebound=self.global_rebound,
        )
        self._analyse_body(module)
        return sorted(self.hits.values(), key=lambda hit: hit.site)

    # Scopes

    def _analyse_body(self, scope: Scope, *, tool_factory: bool = False) -> Scope:
        """Taint every name in ``scope``, record its sinks, then descend into what it defines."""
        nested = _nested_definitions(scope.body)
        local_functions = {
            node.name: node
            for node in nested
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        scope.functions = {**scope.functions, **local_functions}
        self._propagate(scope)
        self._check_calls(scope)
        if scope.depth > 0:
            return scope
        self._analyse_nested(nested, scope, tool_factory=tool_factory)
        return scope

    def _analyse_nested(
        self, nested: list[FunctionNode | ast.ClassDef], scope: Scope, *, tool_factory: bool
    ) -> None:
        for node in nested:
            if isinstance(node, ast.ClassDef):
                self._analyse_class(node, scope)
            else:
                self._analyse_function(node, scope, tool_argument=tool_factory)

    def _analyse_class(self, node: ast.ClassDef, outer: Scope) -> None:
        visible = outer.visible
        nested = _nested_definitions(node.body)
        methods = {
            item.name: item
            for item in nested
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        scope = Scope(
            node.body,
            dict(visible.taint),
            visible.functions,
            methods,
            depth=0,
            rebound={**visible.inherited_rebound, **builtin_rebindings(scope_nodes(node.body))},
            inherited_rebound=visible.inherited_rebound,
            sandboxes=set(visible.sandboxes),
            enclosing=visible,
        )
        self._propagate(scope)
        self._check_calls(scope)
        self._analyse_nested(nested, scope, tool_factory=False)

    def _analyse_function(
        self, fn: FunctionNode, outer: Scope, *, tool_argument: bool = False
    ) -> None:
        visible = outer.visible
        names = {param.arg for param in _parameters(fn)}
        taint = {k: v for k, v in visible.taint.items() if k.split(".")[0] not in names}
        if tool_argument:
            taint.update({n: f"tool argument {n!r}" for n in names if n != "self"})
        rebound = {**visible.inherited_rebound, **builtin_rebindings(scope_nodes(fn.body))}
        scope = Scope(
            fn.body,
            taint,
            visible.functions,
            outer.methods,
            depth=0,
            rebound=rebound,
            inherited_rebound=rebound,
            sandboxes={s for s in visible.sandboxes if s.split(".")[0] not in names},
        )
        self._seed_parameters(fn, scope)
        is_tool = any(get_decorator_name(d) == "tool" for d in fn.decorator_list)
        self._analyse_body(scope, tool_factory=is_tool)

    def _seed_parameters(self, fn: FunctionNode, scope: Scope) -> None:
        for param in _parameters(fn):
            names = annotation_names(param.annotation)
            if "ModelOutput" in names:
                scope.taint.setdefault(param.arg, f"model output (parameter {param.arg!r})")
            if "SandboxEnvironment" in names:
                scope.sandboxes.add(param.arg)

    def _enter_callee(
        self, fn: FunctionNode, seeds: dict[str, str], scope: Scope, via: str, *, record: bool
    ) -> Scope:
        rebound = {**self.global_rebound, **builtin_rebindings(scope_nodes(fn.body))}
        callee = Scope(
            fn.body,
            dict(seeds),
            scope.visible.functions,
            scope.methods,
            depth=1,
            rebound=rebound,
            inherited_rebound=rebound,
            via=via,
        )
        self._seed_parameters(fn, callee)
        if not record:
            self._propagate(callee)
            return callee
        return self._analyse_body(callee)

    # Propagation

    def _is_sandbox_call(self, node: ast.expr, scope: Scope) -> bool:
        node = _unwrap_await(node)
        if not isinstance(node, ast.Call):
            return False
        if get_call_name(node) == "sandbox":
            return True
        qualified = qualified_name(node.func, self.aliases, scope.rebound)
        return qualified is not None and qualified.endswith(".sandbox")

    def _propagate(self, scope: Scope) -> None:
        """Taint assignment targets until nothing changes; flow-insensitive within the body."""
        nodes = list(scope_nodes(scope.body))
        for node in nodes:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign) and self._is_sandbox_call(node.value, scope):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and (
                "SandboxEnvironment" in annotation_names(node.annotation)
                or (node.value is not None and self._is_sandbox_call(node.value, scope))
            ):
                targets = [node.target]
            scope.sandboxes.update(name for t in targets if (name := dotted(t)) is not None)
        changed = True
        while changed:
            changed = False
            for node in nodes:
                for target, value in _flows(node):
                    reason = self.taint_of(value, scope)
                    if reason is not None and self._bind(target, reason, scope):
                        changed = True

    def _bind(self, target: ast.expr, reason: str, scope: Scope) -> bool:
        if isinstance(target, (ast.Tuple, ast.List)):
            results = [self._bind(element, reason, scope) for element in target.elts]
            return any(results)
        if isinstance(target, ast.Starred):
            return self._bind(target.value, reason, scope)
        if isinstance(target, ast.Subscript):
            return self._bind(target.value, reason, scope)
        name = dotted(target)
        if name is None or name in scope.taint:
            return False
        scope.taint[name] = reason
        return True

    def taint_of(self, expr: ast.expr, scope: Scope) -> str | None:
        """Where the model-controlled part of ``expr`` came from, or None if nothing in it is."""
        for node in ast.walk(expr):
            if isinstance(node, (ast.Name, ast.Attribute)):
                name = dotted(node)
                if name is not None and name in scope.taint:
                    return scope.taint[name]
                if isinstance(node, ast.Attribute):
                    source = _attribute_source(node)
                    if source is not None:
                        return source
            elif isinstance(node, ast.Call):
                reason = self._call_source(node, scope)
                if reason is not None:
                    return reason
        return None

    def _call_source(self, call: ast.Call, scope: Scope) -> str | None:
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr == "generate":
                return "model output (generate())"
            if func.attr in ("read_file", "exec") and (
                self._is_sandbox_call(func.value, scope) or dotted(func.value) in scope.sandboxes
            ):
                return (
                    "sandbox read_file()" if func.attr == "read_file" else "sandbox exec() output"
                )
        if scope.depth > 0:
            return None
        resolved = self._callee(call, scope)
        if resolved is None:
            return None
        name, fn, _bound = resolved
        return self._return_taint(fn, name, scope)

    def _return_taint(self, fn: FunctionNode, name: str, scope: Scope) -> str | None:
        if id(fn) not in self._returns:
            self._returns[id(fn)] = None
            callee = self._enter_callee(fn, {}, scope, "", record=False)
            for node in scope_nodes(fn.body):
                if isinstance(node, ast.Return) and node.value is not None:
                    reason = self.taint_of(node.value, callee)
                    if reason is not None:
                        self._returns[id(fn)] = f"{reason}, returned by {name}()"
                        break
        return self._returns[id(fn)]

    # Sinks

    def _callee(self, call: ast.Call, scope: Scope) -> tuple[str, FunctionNode, bool] | None:
        """The same-file function a call runs, its name, and whether its first parameter is bound."""
        func = call.func
        if isinstance(func, ast.Name) and func.id in scope.functions:
            return func.id, scope.functions[func.id], False
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
            and func.attr in scope.methods
        ):
            method = scope.methods[func.attr]
            return func.attr, method, not _is_staticmethod(method)
        return None

    def _check_calls(self, scope: Scope) -> None:
        for node in scope_nodes(scope.body):
            if not isinstance(node, ast.Call):
                continue
            qualified = qualified_name(node.func, self.aliases, scope.rebound)
            sink = SINKS.get(qualified) if qualified else None
            if sink is not None:
                hit = self._sink_hit(node, sink, scope)
                if hit is not None and hit.traced:
                    hit.message += scope.via
                    self._record(hit)
                elif hit is not None and scope.depth == 0:
                    self._record(hit)
            elif scope.depth == 0:
                self._follow_call(node, scope)

    def _follow_call(self, call: ast.Call, scope: Scope) -> None:
        resolved = self._callee(call, scope)
        if resolved is None:
            return
        name, fn, bound = resolved
        seeds = self._argument_seeds(fn, call, scope, bound=bound)
        if seeds:
            self._enter_callee(
                fn, seeds, scope, f", via {name}() from line {call.lineno}", record=True
            )

    def _argument_seeds(
        self, fn: FunctionNode, call: ast.Call, scope: Scope, *, bound: bool
    ) -> dict[str, str]:
        args = fn.args
        positional = [*args.posonlyargs, *args.args][1 if bound else 0 :]
        named = {param.arg for param in [*positional, *args.kwonlyargs]}
        everything = [param.arg for param in _parameters(fn)][1 if bound else 0 :]
        seeds: dict[str, str] = {}
        for index, arg in enumerate(call.args):
            value = arg.value if isinstance(arg, ast.Starred) else arg
            reason = self.taint_of(value, scope)
            if reason is None:
                continue
            if isinstance(arg, ast.Starred):
                seeds.update(dict.fromkeys(everything, reason))
            elif index < len(positional):
                seeds[positional[index].arg] = reason
            elif args.vararg is not None:
                seeds[args.vararg.arg] = reason
        for keyword in call.keywords:
            reason = self.taint_of(keyword.value, scope)
            if reason is None:
                continue
            if keyword.arg is None:
                seeds.update(dict.fromkeys(everything, reason))
            elif keyword.arg in named:
                seeds[keyword.arg] = reason
            elif args.kwarg is not None:
                seeds[args.kwarg.arg] = reason
        return seeds

    def _sink_hit(self, call: ast.Call, sink: _Sink, scope: Scope) -> Hit | None:
        payload = _argument(call, 0, sink.keywords)
        if sink.name in _YAML_LOADS_WITH_LOADER and _has_safe_loader(call, self.safe_loaders):
            return None
        if sink.name == "torch.load" and _is_true(_keyword(call, "weights_only")):
            return None
        if sink.kind == "argv":
            shell = _keyword(call, "shell")
            if shell is not None and not _is_false(shell.value):
                return self._always(call, sink, payload, scope, kind="shell")
            executable = _keyword(call, "executable")
            if executable is not None:
                reason = self.taint_of(executable.value, scope)
                if reason is not None:
                    return self._fail(call, sink, reason, kind="program")
            return self._program(call, sink, _program_of(payload), scope)
        if sink.kind == "command":
            if isinstance(payload, (ast.List, ast.Tuple)):
                return self._program(call, sink, _program_of(payload), scope)
            return self._always(call, sink, payload, scope, kind="shell")
        if sink.kind == "program":
            return self._program(call, sink, _program_argument(payload), scope)
        if sink.kind == "import":
            reason = self.taint_of(payload, scope) if payload is not None else None
            return self._fail(call, sink, reason) if reason is not None else None
        return self._always(call, sink, payload, scope, kind=sink.kind)

    def _always(
        self, call: ast.Call, sink: _Sink, payload: ast.expr | None, scope: Scope, *, kind: SinkKind
    ) -> Hit:
        reason = self.taint_of(payload, scope) if payload is not None else None
        if reason is not None:
            return self._fail(call, sink, reason, kind=kind)
        return Hit(
            call,
            sink,
            "warning",
            f"{sink.name}() {_WARN_TEXT[kind]}; no model-controlled input was traced to it",
            WARN_HINT,
            traced=False,
        )

    def _program(
        self, call: ast.Call, sink: _Sink, program: ast.expr | None | Literal[False], scope: Scope
    ) -> Hit | None:
        """A process sink without a shell: fails when the program is tainted, warns when the argv is opaque and tainted."""
        if program is None:
            return None
        if program is False:
            argv = _argument(call, 0, sink.keywords)
            if isinstance(argv, ast.Starred):
                argv = argv.value
            reason = self.taint_of(argv, scope) if argv is not None else None
            if reason is None:
                return None
            return Hit(
                call,
                sink,
                "warning",
                f"{sink.name}() runs a program chosen at runtime from an argv that carries "
                f"model-controlled input: {reason}",
                ARGV_HINT,
            )
        reason = self.taint_of(program, scope)
        return self._fail(call, sink, reason, kind="program") if reason is not None else None

    def _fail(
        self, call: ast.Call, sink: _Sink, reason: str, *, kind: SinkKind | None = None
    ) -> Hit:
        text = _FAIL_TEXT[kind or sink.kind]
        return Hit(call, sink, "error", f"{sink.name}() {text}: {reason}", FAIL_HINT)

    def _record(self, hit: Hit) -> None:
        existing = self.hits.get(hit.site)
        if existing is None or (existing.severity == "warning" and hit.severity == "error"):
            self.hits[hit.site] = hit


def _flows(node: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    """``(target, value)`` pairs where ``node`` moves a value into a name."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield target, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        yield node.target, node.value
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        yield node.target, node.iter
    elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
        yield node.target, node.value
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                yield item.optional_vars, item.context_expr
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
    ):
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            yield node.func.value, arg


def _unwrap_await(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Await) else node


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _argument(call: ast.Call, index: int, keywords: tuple[str, ...]) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]
    for keyword in call.keywords:
        if keyword.arg in keywords:
            return keyword.value
    return None


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def _is_true(keyword: ast.keyword | None) -> bool:
    return (
        keyword is not None
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
    )


def _program_of(argv: ast.expr | None) -> ast.expr | None | Literal[False]:
    """The program an argv runs: its first element, None when constant or absent, False when it cannot be told."""
    if argv is None:
        return None
    if isinstance(argv, ast.Constant):
        return None
    if isinstance(argv, (ast.List, ast.Tuple)):
        if not argv.elts:
            return None
        first = argv.elts[0]
        return False if isinstance(first, ast.Starred) else first
    return False


def _program_argument(program: ast.expr | None) -> ast.expr | None | Literal[False]:
    """``create_subprocess_exec``'s program: None when constant or absent, False when unpacked from an argv."""
    if program is None or isinstance(program, ast.Constant):
        return None
    return False if isinstance(program, ast.Starred) else program


def _class_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def safe_yaml_loaders(tree: ast.AST) -> frozenset[str]:
    """PyYAML's safe loaders plus the classes in this file that subclass one, however deep."""
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    safe = set(_SAFE_YAML_LOADERS)
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name not in safe and any(_class_name(b) in safe for b in node.bases):
                safe.add(node.name)
                changed = True
    return frozenset(safe)


def _has_safe_loader(call: ast.Call, safe_loaders: frozenset[str]) -> bool:
    loader = _argument(call, 1, ("Loader",))
    return loader is not None and _class_name(loader) in safe_loaders


@rule(
    code="IESC002",
    name=RULE_NAME,
    category="security",
    scopes=("eval", "helper"),
    allowlist=True,
    summary="Model output, tool arguments and sandbox content are not executed or deserialised on the host",
    references=(inspect_docs("sandboxing", "Sandboxing"),),
)
def host_code_execution(ctx: LintContext) -> Iterable[Finding]:
    """Model output, tool arguments and sandbox content are not executed or deserialised on the host.

    ## What it does

    Traces values the model under evaluation controls to calls that run or
    deserialise them on the machine running the evaluation rather than in the
    sandbox.

    Host code is every Python file under the package that ``exclude`` does not
    rule out. Code shipped into a sandbox (challenge sources, container code,
    solution scripts) belongs in ``exclude``; this check and the other AST
    rules then never read it. Within a host file, everything that runs is
    read: module, function and class bodies, decorators, default arguments and
    class bases.

    **Sources**, the values a model controls by construction:

    - the parameters of a function defined inside a ``@tool`` function (the
      tool's ``execute``); the ``@tool`` function's own parameters are task
      configuration and are not sources;
    - ``.completion``, ``.messages``, ``.tool_calls`` and ``.arguments``
      anywhere, ``.output.message`` and ``.output.choices``, ``state.output``,
      the result of any ``.generate(...)`` method call such as
      ``get_model().generate(...)``, and parameters annotated ``ModelOutput``
      (including ``ModelOutput | None`` and ``Optional[ModelOutput]``). A
      solver's bare ``generate(state)`` returns the task state, which is not a
      source as a whole;
    - ``read_file()`` and ``exec()`` results from ``sandbox(...)`` (however it
      is imported), from a name or attribute bound to one, or from a parameter
      or variable annotated ``SandboxEnvironment``.

    **Sinks:**

    - the builtins ``exec``, ``eval``, ``compile`` and ``__import__``, called
      bare or through ``builtins``. An import such as
      ``from inspect_ai import eval`` rebinds only the name it imports, and
      only where it is in force: everywhere in the file when it sits at the
      top level (or in a top-level ``try``), otherwise only in the function or
      block that holds it. ``exec``, ``compile`` and ``__import__`` stay
      checked;
    - ``pickle.load`` and ``pickle.loads``; ``yaml.unsafe_load``,
      ``yaml.full_load`` and their ``_all`` forms; ``yaml.load`` and
      ``yaml.load_all`` without ``SafeLoader``, ``CSafeLoader``, ``BaseLoader``
      or a class in the same file that subclasses one; ``torch.load`` without
      ``weights_only=True``;
    - shell commands: ``os.system``, ``os.popen``, ``subprocess.getoutput``,
      ``subprocess.getstatusoutput``, ``asyncio.create_subprocess_shell``,
      ``inspect_ai.util.subprocess`` with anything but a list literal (a
      string runs through a shell), and ``subprocess.run``, ``Popen``,
      ``call``, ``check_call`` and ``check_output`` with ``shell=`` anything
      but a false literal;
    - the same ``subprocess`` calls without a shell, ``inspect_ai.util.subprocess``
      with a list literal, and ``asyncio.create_subprocess_exec``, only when the
      program (the first argv element) or ``executable=`` is tainted. A tainted
      argument to a constant program is not reported;
    - ``importlib.import_module``, only when the module name is tainted.

    Only the argument that is run counts: the code of ``exec``, not the
    namespace passed beside it. Model input handed to constant code as data is
    not traced. A module sink is recognised only through a name an import
    binds, so a local variable called ``yaml`` or a parameter called ``os`` is
    not mistaken for the module.

    **Propagation** is within one file. In a function, a name (or an attribute
    such as ``self.code``) is tainted if any assignment, loop target, ``with``
    target, walrus or ``append``/``update``-style call puts a tainted value
    into it, wherever the sink sits. An expression is tainted if anything in
    it is, which covers f-strings, concatenation, ``.format`` and calls such as
    ``str(x)``. Nested functions see their enclosing function's taint and
    sandbox bindings. Calls to functions and ``self`` or ``cls`` methods
    (static methods included) in the same file are followed one level:
    tainted arguments, including unpacked ``*args`` and ``**kwargs``, taint the
    callee's parameters, and a callee returning a source taints the call.
    Nothing crosses files.

    **Statuses:**

    - error when a source reaches a sink. The allowlist key is
      ``<path within the package>:<sink>``, for example
      ``common/tools.py:eval`` or ``solver.py:subprocess.run``;
    - warning for every other shell, code or deserialisation sink in host
      code, so a reviewer sees each one. Mark a reviewed site with
      ``# inspect-evals-lint: ignore[host_code_execution]`` on any line of the
      call;
    - warning when a process runs from an argv that is not a list literal and
      carries model-controlled input, since the program cannot be told.

    **Known limits.** An interpreter given code on its command line, such as
    ``["bash", "-c", tool_argument]``, is a constant program with a tainted
    argument and is not reported. A same-file ``def eval`` or a relative
    import of ``eval`` is still taken for the builtin.

    ## Why is this bad?

    The sandbox is what stands between the model and the machine running the
    evaluation. A tool that ``eval``s its argument, or a scorer that ``exec``s
    a file the agent wrote, runs model output with the host's permissions,
    credentials and network: a model can read secrets, alter logs or scores,
    or hang the run (``9**9**9`` gets past a digits-and-operators allowlist).
    Unpickling, ``yaml.load`` or ``torch.load`` of model-controlled bytes is
    the same thing.

    The analysis is deliberately shallow. It misses taint that crosses files
    or passes through more than one call, so an error is strong evidence and
    the absence of one is not proof. A warning is a sink the check could not
    connect to a source, not a verdict that the site is safe.

    ## Example

    ```python
    @tool
    def calculate():
        async def execute(expression: str) -> str:
            return str(eval(expression))

        return execute
    ```
    Use instead:
    ```python
    @tool
    def calculate():
        async def execute(expression: str) -> str:
            result = await sandbox().exec(["python3", "-c", f"print({expression})"])
            return result.stdout

        return execute
    ```

    ## Options

    - `allowlists.host_code_execution`: `{ package = ["path/within/package.py:sink"] }` entries reported as warnings while an existing surface is burned down.
    """
    parsed_files = parse_python_files(ctx)
    failures = parse_failures(parsed_files)
    yield from failures

    reported = bool(failures)
    for parsed in parsed_files.parsed:
        if not isinstance(parsed.tree, ast.Module):
            continue
        relative = _package_relative(parsed.path, ctx.path)
        for hit in FileAnalysis(parsed.tree).run():
            reported = True
            key = f"{relative}:{hit.sink.name}" if hit.severity == "error" else None
            yield Diagnostic(
                hit.message,
                file=parsed.path,
                line=hit.node.lineno,
                column=column_of(hit.node),
                end_line=end_line_of(hit.node),
                severity=hit.severity,
                hint=hit.hint
                if key is None
                else f"{hit.hint}; or review it and document it as '{key}' in allowlists.{RULE_NAME}",
                key=key,
            )

    if reported:
        return
    count = len(parsed_files.parsed)
    if count == 0:
        yield Outcome("skip", "No host Python files to check")
    else:
        yield Outcome("pass", f"No reportable code execution sinks in {count} host Python file(s)")


def _package_relative(path: Path, package: Path) -> str:
    return path.relative_to(package).as_posix() if path.is_relative_to(package) else path.name
