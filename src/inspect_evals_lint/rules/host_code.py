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
from typing import Literal, cast

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
call (or ``pty.spawn``) that is a shell sink with ``shell=`` and a program
sink without; ``command`` is a shell sink when its payload is written as a
string and an argv otherwise; ``program`` and ``import`` report only when
tainted.
"""


@dataclass(frozen=True)
class _Sink:
    """A call that runs its argument on the host, and where in the call that argument is."""

    name: str
    """How findings and allowlist keys name it: ``eval``, ``subprocess.run``."""
    kind: SinkKind
    keywords: tuple[str, ...] = ()
    """Keyword spellings of the positional argument that carries the payload."""
    position: int = 0
    """Where the payload sits among the positional arguments: 1 for ``os.spawnv(mode, path, args)``."""


def _sinks(
    kind: SinkKind, names: Iterable[str], *keywords: str, position: int = 0
) -> dict[str, _Sink]:
    return {name: _Sink(name, kind, keywords, position) for name in names}


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
            "marshal.loads",
            "marshal.load",
            "dill.loads",
            "dill.load",
            "cloudpickle.loads",
            "cloudpickle.load",
            "joblib.load",
            "pandas.read_pickle",
            "numpy.load",
        ),
        "data",
        "file",
        "stream",
        "f",
        "bytes",
        "str",
        "filename",
        "filepath_or_buffer",
    ),
    **_sinks("code", ("runpy.run_path", "runpy.run_module"), "path_name", "mod_name"),
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
    **_sinks("argv", ("pty.spawn",), "argv"),
    **_sinks("program", ("asyncio.create_subprocess_exec",), "program"),
    **_sinks(
        "program",
        (
            *(f"os.exec{suffix}" for suffix in ("v", "ve", "vp", "vpe", "l", "le", "lp", "lpe")),
            "os.posix_spawn",
            "os.posix_spawnp",
        ),
        "path",
    ),
    **_sinks(
        "program",
        (f"os.spawn{suffix}" for suffix in ("v", "ve", "vp", "vpe", "l", "le", "lp", "lpe")),
        position=1,
    ),
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
    f"site reviewed with `# inspect-evals-lint: ignore[{RULE_NAME}] -- <why it is safe>`"
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


Names = dict[str, str | None]
"""Local name to what an import in force binds it to (``sp`` to ``subprocess``); None where a local binding hides the import."""


def _imports_in(nodes: Iterable[ast.AST]) -> dict[str, str]:
    bound: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(_import_bindings(node))
    return bound


def _try_blocks(statement: ast.Try | ast.TryStar) -> list[list[ast.stmt]]:
    return [
        statement.body,
        *(handler.body for handler in statement.handlers),
        statement.orelse,
        statement.finalbody,
    ]


def _unconditional_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run whenever the body does: the body itself and its ``try`` blocks."""
    for statement in body:
        yield statement
        if isinstance(statement, (ast.Try, ast.TryStar)):
            for block in _try_blocks(statement):
                yield from _unconditional_statements(block)


BlockStatement = ast.If | ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith | ast.Match


def _blocks(body: list[ast.stmt]) -> Iterator[BlockStatement]:
    """The compound statements in a module-level body, looking through ``try``, whose imports hold only inside them."""
    for statement in body:
        if isinstance(statement, (ast.Try, ast.TryStar)):
            for block in _try_blocks(statement):
                yield from _blocks(block)
        elif isinstance(
            statement,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match),
        ):
            yield statement


def _block_body(statement: BlockStatement) -> list[ast.stmt]:
    if isinstance(statement, ast.Match):
        return [s for case in statement.cases for s in case.body]
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return statement.body
    return [*statement.body, *statement.orelse]


def module_names(tree: ast.Module) -> Names:
    """What names resolve to across a file.

    The module's unconditional imports (the top level and top-level ``try``)
    apply everywhere. A module-level block's import of anything but a builtin
    sink name applies too where nothing unconditional binds the name, since the
    module global it sets is visible once the block has run. A block's
    rebinding of a builtin applies only inside the block.
    """
    conditional = {
        local: qualified
        for local, qualified in _imports_in(scope_nodes(tree.body)).items()
        if local not in _BUILTIN_SINKS
    }
    return {**conditional, **_imports_in(_unconditional_statements(tree.body))}


def _locally_bound(body: list[ast.stmt], parameters: Iterable[str]) -> set[str]:
    """Names a function or class body binds other than by import, less its ``global`` and ``nonlocal`` names."""
    bound = set(parameters)
    declared: set[str] = set()
    for node in scope_nodes(body):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif (
            isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.ExceptHandler,
                    ast.MatchAs,
                    ast.MatchStar,
                ),
            )
            and node.name
        ):
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound - declared - _BUILTIN_SINKS


def local_names(body: list[ast.stmt], outer: Names, parameters: Iterable[str] = ()) -> Names:
    """What names resolve to in a function or class body.

    The enclosing scope's names, hidden by anything the body binds other than
    by import (a parameter or ``yaml = YAML()``), then the body's own imports
    wherever they sit in it. A builtin sink name is only ever rebound by an
    import.
    """
    imports = _imports_in(scope_nodes(body))
    hidden = dict.fromkeys(_locally_bound(body, parameters) - imports.keys())
    return {**outer, **hidden, **imports}


def qualified_name(func: ast.expr, names: Names) -> str | None:
    """``subprocess.run`` for ``sp.run`` after ``import subprocess as sp``; None when no import in force binds the name.

    A bare builtin sink name resolves to ``builtins.<name>`` unless an import in
    force rebinds it, so ``from inspect_ai import eval`` exempts ``eval`` and
    nothing else.
    """
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    if not parts and node.id in _BUILTIN_SINKS:
        return names.get(node.id) or f"builtins.{node.id}"
    base = names.get(node.id)
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


def scope_nodes(body: list[ast.stmt], skip: frozenset[int] = frozenset()) -> Iterator[ast.AST]:
    """Every node a function, class or module body runs, without entering nested bodies or the statements in ``skip``.

    A nested definition's decorators, defaults and class bases run here, so they are included.
    """
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        if id(node) in skip:
            continue
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(reversed(_evaluated_in_enclosing_scope(node)))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _nested_definitions(
    body: list[ast.stmt], skip: frozenset[int] = frozenset()
) -> list[FunctionNode | ast.ClassDef]:
    return [
        node
        for node in scope_nodes(body, skip)
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


ScopeKind = Literal["module", "block", "class", "function"]


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
    names: Names
    """What names resolve to here; see :func:`module_names` and :func:`local_names`."""
    kind: ScopeKind = "function"
    """A ``block`` is a module-level compound statement holding an import: it shares the module's taint and sandbox bindings, and has its own names."""
    via: str = ""
    sandboxes: set[str] = field(default_factory=set)
    """Dotted names bound to a sandbox environment."""
    enclosing: Scope | None = None
    """For a class body, the scope its methods close over; class-level names are not visible in methods."""

    @property
    def visible(self) -> Scope:
        """The scope a definition nested here closes over."""
        return self.enclosing or self


CalleeKey = tuple[int, frozenset[tuple[str, str]], int, int]


class FileAnalysis:
    """The taint analysis of one parsed file; ``hits`` holds each sink site at its worst."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.safe_loaders = safe_yaml_loaders(tree)
        self.hits: dict[tuple[int, int], Hit] = {}
        self.module = Scope(
            tree.body,
            {},
            {
                node.name: node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            },
            {},
            depth=0,
            names=module_names(tree),
            kind="module",
        )
        self._defining: dict[int, Scope] = {}
        """The scope each definition closes over, by ``id`` of its node."""
        self._names: dict[int, Names] = {}
        self._returns: dict[tuple[int, int, int], str | None] = {}
        self._followed: set[CalleeKey] = set()

    def run(self) -> list[Hit]:
        self._analyse_body(self.module)
        return sorted(self.hits.values(), key=lambda hit: hit.site)

    # Scopes

    def _analyse_body(self, scope: Scope, *, tool_factory: bool = False) -> Scope:
        """Taint every name in ``scope``, record its sinks, then descend into what it defines."""
        blocks = self._import_blocks(scope)
        skip = frozenset(id(block) for block in blocks)
        nested = _nested_definitions(scope.body, skip)
        scope.functions = {
            **scope.functions,
            **{
                node.name: node
                for node in _nested_definitions(scope.body)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            },
        }
        if scope.depth == 0:
            for node in nested:
                self._defining[id(node)] = scope.visible
        if scope.kind != "block":
            self._propagate(scope)
        self._check_calls(scope, skip)
        if scope.depth > 0:
            return scope
        for block in blocks:
            imports = _imports_in(_unconditional_statements(_block_body(block)))
            self._analyse_body(
                Scope(
                    [block],
                    scope.taint,
                    scope.functions,
                    scope.methods,
                    depth=0,
                    names={**scope.names, **imports},
                    kind="block",
                    sandboxes=scope.sandboxes,
                )
            )
        self._analyse_nested(nested, scope, tool_factory=tool_factory)
        return scope

    def _import_blocks(self, scope: Scope) -> list[BlockStatement]:
        """The module-level blocks directly in ``scope`` that hold an import, each analysed as its own block scope."""
        if scope.kind == "module":
            candidates = _blocks(scope.body)
        elif scope.kind == "block":
            (statement,) = scope.body
            candidates = _blocks(_block_body(cast(BlockStatement, statement)))
        else:
            return []
        return [
            block
            for block in candidates
            if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in scope_nodes([block]))
        ]

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
        methods = {
            item.name: item
            for item in _nested_definitions(node.body)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        scope = Scope(
            node.body,
            dict(visible.taint),
            visible.functions,
            methods,
            depth=0,
            names=local_names(node.body, visible.names),
            kind="class",
            sandboxes=set(visible.sandboxes),
            enclosing=visible,
        )
        self._analyse_body(scope)

    def _analyse_function(
        self, fn: FunctionNode, outer: Scope, *, tool_argument: bool = False
    ) -> None:
        scope = self._function_scope(fn, outer.visible, outer.methods, depth=0)
        if tool_argument:
            scope.taint.update(
                {p.arg: f"tool argument {p.arg!r}" for p in _parameters(fn) if p.arg != "self"}
            )
        is_tool = any(get_decorator_name(d) == "tool" for d in fn.decorator_list)
        self._analyse_body(scope, tool_factory=is_tool)

    def _function_scope(
        self,
        fn: FunctionNode,
        defining: Scope,
        methods: dict[str, FunctionNode],
        *,
        depth: int,
        via: str = "",
    ) -> Scope:
        """A scope for ``fn``'s body that closes over ``defining``: its taint, sandbox bindings and names."""
        parameters = {param.arg for param in _parameters(fn)}

        def closes_over(name: str) -> bool:
            return name.split(".")[0] not in parameters

        names = self._names.get(id(fn))
        if names is None:
            names = self._names[id(fn)] = local_names(fn.body, defining.names, parameters)
        scope = Scope(
            fn.body,
            {k: v for k, v in defining.taint.items() if closes_over(k)},
            defining.functions,
            methods,
            depth=depth,
            names=names,
            via=via,
            sandboxes={s for s in defining.sandboxes if closes_over(s)},
        )
        self._seed_parameters(fn, scope)
        for param, default in _defaults(fn.args):
            reason = self.taint_of(default, defining)
            if reason is not None:
                scope.taint.setdefault(param.arg, reason)
        return scope

    def _seed_parameters(self, fn: FunctionNode, scope: Scope) -> None:
        for param in _parameters(fn):
            names = annotation_names(param.annotation)
            if "ModelOutput" in names:
                scope.taint.setdefault(param.arg, f"model output (parameter {param.arg!r})")
            if "SandboxEnvironment" in names:
                scope.sandboxes.add(param.arg)

    def _defining_scope(self, fn: FunctionNode) -> Scope:
        return self._defining.get(id(fn), self.module)

    def _enter_callee(self, fn: FunctionNode, seeds: dict[str, str], via: str) -> Scope:
        """``fn``'s body entered through a call, in the context of the scope that defines it, with ``seeds`` tainting its parameters."""
        defining = self._defining_scope(fn)
        callee = self._function_scope(fn, defining, defining.methods, depth=1, via=via)
        callee.taint.update(seeds)
        return callee

    # Propagation

    def _is_sandbox_call(self, node: ast.expr, scope: Scope) -> bool:
        node = _unwrap_await(node)
        if not isinstance(node, ast.Call):
            return False
        if get_call_name(node) == "sandbox":
            return True
        qualified = qualified_name(node.func, scope.names)
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
        return self._return_taint(fn, name)

    def _return_taint(self, fn: FunctionNode, name: str) -> str | None:
        """What a call to ``fn`` returns that is model-controlled; recomputed as its defining scope gains taint."""
        defining = self._defining_scope(fn)
        key = (id(fn), len(defining.taint), len(defining.sandboxes))
        if key not in self._returns:
            self._returns[key] = None
            callee = self._enter_callee(fn, {}, "")
            self._propagate(callee)
            for node in scope_nodes(fn.body):
                if isinstance(node, ast.Return) and node.value is not None:
                    reason = self.taint_of(node.value, callee)
                    if reason is not None:
                        self._returns[key] = f"{reason}, returned by {name}()"
                        break
        return self._returns[key]

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

    def _check_calls(self, scope: Scope, skip: frozenset[int] = frozenset()) -> None:
        for node in scope_nodes(scope.body, skip):
            if not isinstance(node, ast.Call):
                continue
            qualified = qualified_name(node.func, scope.names)
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
        """Analyse a same-file callee with the call's tainted arguments, once per distinct set of them."""
        resolved = self._callee(call, scope)
        if resolved is None:
            return
        name, fn, bound = resolved
        seeds = self._argument_seeds(fn, call, scope, bound=bound)
        if not seeds:
            return
        defining = self._defining_scope(fn)
        key = (id(fn), frozenset(seeds.items()), len(defining.taint), len(defining.sandboxes))
        if key in self._followed:
            return
        self._followed.add(key)
        self._analyse_body(self._enter_callee(fn, seeds, f", via {name}() from line {call.lineno}"))

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
        payload = _argument(call, sink.position, sink.keywords)
        if sink.name in _YAML_LOADS_WITH_LOADER and _has_safe_loader(call, self.safe_loaders):
            return None
        if sink.name == "torch.load" and _is_true(_keyword(call, "weights_only")):
            return None
        if sink.name == "numpy.load":
            allow_pickle = _keyword(call, "allow_pickle")
            if allow_pickle is None or _is_false(allow_pickle.value):
                return None
        if sink.kind == "argv":
            shell = _keyword(call, "shell")
            if shell is not None and not _is_false(shell.value):
                return self._always(call, sink, _shell_command(payload), scope, kind="shell")
            executable = _keyword(call, "executable")
            if executable is not None:
                reason = self.taint_of(executable.value, scope)
                if reason is not None:
                    return self._fail(call, sink, reason, kind="program")
            return self._program(call, sink, _program_of(payload), scope)
        if sink.kind == "command":
            if _is_string(payload):
                return self._always(call, sink, payload, scope, kind="shell")
            return self._program(call, sink, _program_of(payload), scope)
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
            argv = _argument(call, sink.position, sink.keywords)
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
    elif isinstance(node, ast.Match):
        for case in node.cases:
            for name in _captures(case.pattern):
                yield ast.Name(id=name, ctx=ast.Store()), node.subject
    elif isinstance(node, ast.Lambda):
        for param, default in _defaults(node.args):
            yield ast.Name(id=param.arg, ctx=ast.Store()), default


def _defaults(args: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    """Each parameter that has a default, with its default."""
    positional = [*args.posonlyargs, *args.args]
    pairs = zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
    keyword_only = zip(args.kwonlyargs, args.kw_defaults, strict=True)
    return [*pairs, *((param, d) for param, d in keyword_only if d is not None)]


def _captures(pattern: ast.pattern) -> Iterator[str]:
    """The names a ``match`` pattern binds; each takes part of the subject."""
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest


def _unwrap_await(node: ast.expr) -> ast.expr:
    return node.value if isinstance(node, ast.Await) else node


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _argument(call: ast.Call, index: int, keywords: tuple[str, ...]) -> ast.expr | None:
    """The argument at ``index``, or an unpacked ``*args`` at or before it that may supply it."""
    for position, arg in enumerate(call.args):
        if position == index or isinstance(arg, ast.Starred):
            return arg
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


def _is_string(node: ast.expr | None) -> bool:
    """Whether ``node`` is written as a string: a literal, an f-string, concatenation or ``%`` with one, or ``.format`` on one."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp):
        return _is_string(node.left) or _is_string(node.right)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_string(node.func.value)
    )


def _shell_command(payload: ast.expr | None) -> ast.expr | None:
    """What a shell runs: a list's first element (the rest are the shell's arguments), otherwise the payload."""
    if isinstance(payload, (ast.List, ast.Tuple)):
        if not payload.elts:
            return None
        first = payload.elts[0]
        return first if not isinstance(first, ast.Starred) else first.value
    return payload


def _program_of(argv: ast.expr | None) -> ast.expr | None | Literal[False]:
    """The program an argv runs: a string or a list's first element, None when constant or absent, False when it cannot be told."""
    if argv is None:
        return None
    if isinstance(argv, ast.Constant):
        return None
    if _is_string(argv):
        return argv
    if isinstance(argv, (ast.List, ast.Tuple)):
        if not argv.elts:
            return None
        first = argv.elts[0]
        return False if isinstance(first, ast.Starred) else first
    return False


def _program_argument(program: ast.expr | None) -> ast.expr | None | Literal[False]:
    """A program argument (``create_subprocess_exec``, ``os.execv``): None when constant or absent, False when unpacked from an argv."""
    if program is None or isinstance(program, ast.Constant):
        return None
    return False if isinstance(program, ast.Starred) else program


@dataclass(frozen=True)
class SafeLoaders:
    """Which ``yaml.load`` loaders are safe in one file."""

    local: frozenset[str]
    """Classes the file defines, which a bare name refers to instead of PyYAML's."""
    safe_local: frozenset[str]
    """The local classes that subclass a safe loader, however deep."""

    def is_safe(self, loader: ast.expr | None) -> bool:
        """``SafeLoader``, ``yaml.CSafeLoader``, a safe local subclass, or ``getattr(yaml, "CSafeLoader", <safe>)``."""
        if isinstance(loader, ast.Name):
            if loader.id in self.local:
                return loader.id in self.safe_local
            return loader.id in _SAFE_YAML_LOADERS
        if isinstance(loader, ast.Attribute):
            return loader.attr in _SAFE_YAML_LOADERS or loader.attr in self.safe_local
        if (
            isinstance(loader, ast.Call)
            and get_call_name(loader) == "getattr"
            and len(loader.args) in (2, 3)
            and not loader.keywords
        ):
            name = loader.args[1]
            return (
                isinstance(name, ast.Constant)
                and name.value in _SAFE_YAML_LOADERS | self.safe_local
                and (len(loader.args) == 2 or self.is_safe(loader.args[2]))
            )
        return False


def safe_yaml_loaders(tree: ast.AST) -> SafeLoaders:
    """PyYAML's safe loaders plus the classes in this file that subclass one; a local class is judged by its bases, not its name."""
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    loaders = SafeLoaders(frozenset(node.name for node in classes), frozenset())
    changed = True
    while changed:
        changed = False
        for node in classes:
            if node.name not in loaders.safe_local and any(
                loaders.is_safe(base) for base in node.bases
            ):
                loaders = SafeLoaders(loaders.local, loaders.safe_local | {node.name})
                changed = True
    return loaders


def _has_safe_loader(call: ast.Call, safe_loaders: SafeLoaders) -> bool:
    return safe_loaders.is_safe(_argument(call, 1, ("Loader",)))


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

    - code: the builtins ``exec``, ``eval``, ``compile`` and ``__import__``,
      called bare or through ``builtins``, and ``runpy.run_path`` and
      ``runpy.run_module``. An import such as ``from inspect_ai import eval``
      rebinds only the name it imports, and only where it is in force: the
      whole file when it sits at the top level (or in a top-level ``try``);
      a module-level block such as an ``if __name__ == "__main__":`` guard
      and the functions defined in it; or a function and the functions
      nested in it. ``exec``, ``compile`` and ``__import__`` stay checked;
    - data: ``pickle``, ``marshal``, ``dill`` and ``cloudpickle`` ``load``
      and ``loads``; ``joblib.load``; ``pandas.read_pickle``; ``numpy.load``
      with ``allow_pickle=`` anything but a false literal;
      ``yaml.unsafe_load``, ``yaml.full_load`` and their ``_all`` forms;
      ``yaml.load`` and ``yaml.load_all`` without a safe loader; and
      ``torch.load`` without ``weights_only=True``. A safe loader is
      ``SafeLoader``, ``CSafeLoader`` or ``BaseLoader``,
      ``getattr(yaml, "CSafeLoader", yaml.SafeLoader)`` where the name and
      the default are both safe, or a class in the same file that subclasses
      one. A same-file class is judged by its bases, not its name;
    - shell commands: ``os.system``, ``os.popen``, ``subprocess.getoutput``,
      ``subprocess.getstatusoutput``, ``asyncio.create_subprocess_shell``,
      ``inspect_ai.util.subprocess`` with a payload written as a string (a
      literal, f-string, concatenation, ``%`` or ``.format``), and
      ``subprocess.run``, ``Popen``, ``call``, ``check_call`` and
      ``check_output`` with ``shell=`` anything but a false literal. With a
      shell and a list, only the first element is the command;
    - programs, reported only when the program or ``executable=`` is
      tainted: the same ``subprocess`` calls without a shell, ``pty.spawn``
      and ``inspect_ai.util.subprocess`` with a list or tuple literal, whose
      program is the first element (or the whole argv when it is written as
      a string); ``asyncio.create_subprocess_exec``, ``os.exec*`` and
      ``os.posix_spawn``/``posix_spawnp``, whose program is the first
      argument; and ``os.spawn*``, whose program follows the mode. A tainted
      argument to a constant program is not reported;
    - ``importlib.import_module``, only when the module name is tainted.

    Only the argument that is run counts: the code of ``exec``, not the
    namespace passed beside it. Model input handed to constant code as data is
    not traced.

    A module sink is recognised only through a name an import in force
    binds. Imports at the top level or in a top-level ``try`` apply to the
    whole file. A function's imports apply in that function and the
    functions nested in it. A module-level block's imports apply inside it,
    and elsewhere only to names no top-level import binds. A parameter or
    other local binding hides an imported module in its function: with
    ``import os`` at the top, ``def f(os): os.system(x)`` is not the module,
    and neither is ``yaml.load`` after ``yaml = YAML()`` in a function.

    **Propagation** is within one file. In a function, a name (or an attribute
    such as ``self.code``) is tainted if any assignment, loop target, ``with``
    target, walrus, ``match`` capture, default argument or
    ``append``/``update``-style call puts a tainted value into it, wherever
    the sink sits. An expression is tainted if anything in it is, which
    covers f-strings, concatenation, ``.format`` and calls such as ``str(x)``.
    Nested functions see their enclosing function's taint and sandbox
    bindings. Calls to functions and ``self`` or ``cls`` methods (static
    methods included) in the same file are followed one level: tainted
    arguments, including unpacked ``*args`` and ``**kwargs``, taint the
    callee's parameters, and a callee returning a source taints the call. A
    callee is analysed in the scope that defines it, with that scope's
    taint, sandbox bindings and imports. Nothing crosses files.

    **Statuses:**

    - error when a source reaches a sink. The allowlist key is
      ``<path within the package>:<sink>``, for example
      ``common/tools.py:eval`` or ``solver.py:subprocess.run``;
    - warning for every other shell, code or deserialisation sink in host
      code, so a reviewer sees each one. Mark a reviewed site with
      ``# inspect-evals-lint: ignore[host_code_execution] -- <why it is safe>``
      on any line of the call;
    - warning when a process runs from an argv that is not a literal and
      carries model-controlled input, since the program cannot be told. This
      includes ``inspect_ai.util.subprocess`` given a variable, which may
      hold a string or a list.

    **Known limits:**

    - an interpreter given code on its command line, such as
      ``["bash", "-c", tool_argument]``, is a constant program with a
      tainted argument and is not reported;
    - a same-file ``def eval`` or a relative import of ``eval`` is still
      taken for the builtin, and aliasing a builtin by assignment
      (``ev = eval; ev(x)``) is not followed;
    - a chained ``__import__("os").system(x)`` is not recognised as
      ``os.system``; the ``__import__`` call itself is still checked;
    - an import in a module-level ``with`` block, such as
      ``with suppress(ImportError):``, is treated like one in an ``if``: a
      builtin rebinding there applies only inside the block;
    - ``global`` and ``nonlocal`` writes are not traced from one function to
      another;
    - a static method called through its class name (``H.run(x)``) is not
      followed, only one called through ``self`` or ``cls``;
    - a sandbox bound in one method, such as ``self.sb = sandbox()`` in
      ``__init__``, is not seen in another.

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
