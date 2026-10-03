"""Scoring rules: what a scorer does when its grader or sandbox fails.

The rules here read the code a scorer runs: every ``@scorer`` function, the
functions nested in it, and the package functions they call, followed to any
depth. A call is followed when its target can be read from the source: a
function defined in an enclosing scope or at the top of the file, or a
function imported from another module of the package, directly, as an
attribute of an imported module, or through a re-export. ``@tool`` functions
are not followed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.diagnostics import Diagnostic, Finding, Outcome, Severity
from inspect_evals_lint.registry import inspect_docs, rule
from inspect_evals_lint.rules._ast import (
    ParsedFile,
    column_of,
    get_call_name,
    get_decorator_name,
    parse_failures,
    parse_python_files,
)
from inspect_evals_lint.rules.host_code import FunctionNode, scope_nodes

Module = tuple[str, ...]
"""A module's path within the package: ``("core", "scorer")`` for ``core/scorer.py``, ``("core",)`` for ``core/__init__.py``."""

Binding = tuple[Literal["function", "module"], Module, str]
"""What an import binds a name to: a function, as its module and name, or a package module, with an empty name."""


def _is_decorated(fn: FunctionNode, name: str) -> bool:
    return any(get_decorator_name(d) == name for d in fn.decorator_list)


def _functions_in(body: list[ast.stmt]) -> dict[str, FunctionNode]:
    """The functions a body defines, by name, without entering nested bodies."""
    return {
        node.name: node
        for node in scope_nodes(body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_of(path: Path, package: Path) -> tuple[Module, bool]:
    """The module a file holds within the package, and whether it is a package's ``__init__.py``."""
    parts = path.relative_to(package).with_suffix("").parts
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


@dataclass
class PackageFunction:
    """A function in the package, with its file and the definitions enclosing it, outermost first."""

    node: FunctionNode
    file: ParsedFile
    module: Module
    enclosing: tuple[FunctionNode | ast.ClassDef, ...]

    @property
    def scopes(self) -> list[FunctionNode]:
        """The function and the functions enclosing it, innermost first."""
        chain = [d for d in (*self.enclosing, self.node) if not isinstance(d, ast.ClassDef)]
        return list(reversed(chain))


def _locals(fn: FunctionNode) -> frozenset[str]:
    """The names a function binds other than by ``def`` or import: its parameters and assignments."""
    args = fn.args
    bound = {
        a.arg
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if a is not None
    }
    declared: set[str] = set()
    for node in scope_nodes(fn.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
    return frozenset(bound - declared)


def _module_names(tree: ast.Module) -> frozenset[str]:
    """The names a module binds at its top level by assignment or import."""
    names: set[str] = set()
    for node in scope_nodes(tree.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return frozenset(names)


@dataclass
class ScorerCode:
    """The functions of a package that run when one of its scorers does."""

    root: Module
    """The package's import path, split: ``("inspect_evals", "agieval")``."""
    functions: dict[int, PackageFunction] = field(default_factory=dict)
    """Every function in the package, by ``id`` of its node."""
    reached: list[PackageFunction] = field(default_factory=list)
    """The functions a scorer runs, in the order they were found."""
    calls: dict[int, list[tuple[PackageFunction, ast.Call]]] = field(default_factory=dict)
    """For each reached function, by ``id`` of its node, the calls to it in reached code and their callers."""
    modules: dict[Module, ast.Module] = field(default_factory=dict)
    packages: set[Module] = field(default_factory=set)
    _defined: dict[int, dict[str, FunctionNode]] = field(default_factory=dict)
    _imported: dict[int, dict[str, Binding]] = field(default_factory=dict)
    _bound: dict[int, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, ctx: LintContext, files: list[ParsedFile]) -> ScorerCode:
        code = cls(tuple(ctx.config.module_name(ctx.name).split(".")))
        for parsed in files:
            if not isinstance(parsed.tree, ast.Module):
                continue
            module, is_package = _module_of(parsed.path, ctx.path)
            code.modules[module] = parsed.tree
            if is_package:
                code.packages.add(module)
            code._index(parsed, module, parsed.tree.body, ())
        pending = [f for f in code.functions.values() if _is_decorated(f.node, "scorer")]
        seen: set[int] = set()
        while pending:
            function = pending.pop()
            if id(function.node) in seen or _is_decorated(function.node, "tool"):
                continue
            seen.add(id(function.node))
            code.reached.append(function)
            for nested in code.defined(function.node.body).values():
                pending.append(code.functions[id(nested)])
            for node in scope_nodes(function.node.body):
                if isinstance(node, ast.Call):
                    callee = code.callee(node, function)
                    if callee is not None:
                        code.calls.setdefault(id(callee.node), []).append((function, node))
                        pending.append(callee)
        return code

    def _index(
        self,
        parsed: ParsedFile,
        module: Module,
        body: list[ast.stmt],
        enclosing: tuple[FunctionNode | ast.ClassDef, ...],
    ) -> None:
        for node in scope_nodes(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[id(node)] = PackageFunction(node, parsed, module, enclosing)
                self._index(parsed, module, node.body, (*enclosing, node))
            elif isinstance(node, ast.ClassDef):
                self._index(parsed, module, node.body, (*enclosing, node))

    def defined(self, body: list[ast.stmt]) -> dict[str, FunctionNode]:
        """The functions a body defines, by name, without entering nested bodies."""
        key = id(body)
        if key not in self._defined:
            self._defined[key] = _functions_in(body)
        return self._defined[key]

    def bound(self, fn: FunctionNode) -> frozenset[str]:
        """The names ``fn`` binds by parameter or assignment."""
        key = id(fn)
        if key not in self._bound:
            self._bound[key] = _locals(fn)
        return self._bound[key]

    def constant_names(self, function: PackageFunction) -> frozenset[str]:
        """The names that hold module-level or imported values inside ``function``."""
        local: set[str] = set()
        for fn in function.scopes:
            local |= self.bound(fn)
        return _module_names(self.modules[function.module]) - local

    def callee(self, call: ast.Call, caller: PackageFunction) -> PackageFunction | None:
        """The package function ``call`` runs, when it can be read from the source."""
        func = call.func
        if isinstance(func, ast.Name):
            binding = self._name_binding(func.id, caller)
            if isinstance(binding, ast.AST):
                return self.functions.get(id(binding))
            return self._resolve(binding, set()) if binding is not None else None
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return None
        binding = self._name_binding(func.value.id, caller)
        if isinstance(binding, tuple) and binding[0] == "module":
            return self._resolve(("function", binding[1], func.attr), set())
        return None

    def _name_binding(self, name: str, caller: PackageFunction) -> FunctionNode | Binding | None:
        """What ``name`` refers to in ``caller``: a function defined in a scope it sees, or a package import.

        A parameter or assignment in an enclosing function hides anything further out.
        """
        for fn in caller.scopes:
            found = self.defined(fn.body).get(name) or self._imports(fn.body, caller.module).get(
                name
            )
            if found is not None:
                return found
            if name in self.bound(fn):
                return None
        tree = self.modules[caller.module]
        return self.defined(tree.body).get(name) or self._imports(tree.body, caller.module).get(
            name
        )

    def _imports(self, body: list[ast.stmt], module: Module) -> dict[str, Binding]:
        """The package functions and modules a body's imports bind, by local name."""
        key = id(body)
        if key in self._imported:
            return self._imported[key]
        bound: dict[str, Binding] = {}
        for node in scope_nodes(body):
            if isinstance(node, ast.ImportFrom):
                base = self._import_base(node, module)
                if base is None:
                    continue
                for alias in node.names:
                    submodule = (*base, alias.name)
                    bound[alias.asname or alias.name] = (
                        ("module", submodule, "")
                        if submodule in self.modules
                        else ("function", base, alias.name)
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    inner = self._within_package(tuple(alias.name.split(".")))
                    if alias.asname and inner is not None and inner in self.modules:
                        bound[alias.asname] = ("module", inner, "")
        self._imported[key] = bound
        return bound

    def _import_base(self, node: ast.ImportFrom, module: Module) -> Module | None:
        """The package module a ``from ... import`` reads from; None when it is outside the package."""
        named = tuple(node.module.split(".")) if node.module else ()
        if node.level == 0:
            return self._within_package(named)
        package = module if module in self.packages else module[:-1]
        up = node.level - 1
        if up > len(package):
            return None
        return (*package[: len(package) - up], *named)

    def _within_package(self, parts: Module) -> Module | None:
        if parts[: len(self.root)] != self.root:
            return None
        return parts[len(self.root) :]

    def _resolve(self, binding: Binding, seen: set[Binding]) -> PackageFunction | None:
        """The function an import names, followed through re-exports in other package modules."""
        kind, module, name = binding
        tree = self.modules.get(module)
        if kind != "function" or tree is None or binding in seen:
            return None
        seen.add(binding)
        defined = self.defined(tree.body).get(name)
        if defined is not None:
            return self.functions.get(id(defined))
        imported = self._imports(tree.body, module).get(name)
        return self._resolve(imported, seen) if imported is not None else None


# scorer_failure_scored

Arm = Literal["grader", "sandbox"]
"""What the ``try`` guards: a grader model call, or a sandbox ``exec()`` or ``read_file()``."""

_SANDBOX_METHODS = frozenset({"exec", "read_file"})
_GRADER_WORDS = ("judge", "grader")
_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException"})


def _direct_arm(call: ast.Call) -> Arm | None:
    """The arm an awaited call belongs to by its name, for a call the package does not define."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "generate":
        return "grader"
    if isinstance(func, ast.Attribute) and func.attr in _SANDBOX_METHODS:
        return "sandbox"
    name = (get_call_name(call) or "").lower()
    if any(word in name for word in _GRADER_WORDS):
        return "grader"
    return None


def _swallows(statement: ast.Try | ast.TryStar) -> bool:
    """Whether a ``try`` has a broad handler that does not raise, so nothing its body raises gets out."""
    return any(_is_broad(h) and not _has_raise(h) for h in statement.handlers)


def _awaited_calls(body: list[ast.stmt]) -> Iterable[ast.Call]:
    """The calls a body awaits whose errors can get out of it: those not inside a ``try`` that swallows them."""
    caught = frozenset(
        id(statement)
        for node in scope_nodes(body)
        if isinstance(node, (ast.Try, ast.TryStar)) and _swallows(node)
        for statement in node.body
    )
    for node in scope_nodes(body, caught):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            yield node.value


def _worse(a: Arm | None, b: Arm | None) -> Arm | None:
    return "grader" if "grader" in (a, b) else a or b


class _Arms:
    """What each ``try`` body and package function awaits, following package calls to any depth."""

    def __init__(self, code: ScorerCode) -> None:
        self.code = code
        self._steps: dict[int, tuple[Arm | None, list[PackageFunction]]] = {}
        self._functions: dict[int, Arm | None] = {}

    def of_body(self, body: list[ast.stmt], caller: PackageFunction) -> Arm | None:
        arm: Arm | None = None
        for call in _awaited_calls(body):
            callee = self.code.callee(call, caller)
            arm = _worse(arm, self.of_function(callee) if callee else _direct_arm(call))
            if arm == "grader":
                break
        return arm

    def _step(self, function: PackageFunction) -> tuple[Arm | None, list[PackageFunction]]:
        """What a function awaits itself, and the package functions it awaits."""
        key = id(function.node)
        if key not in self._steps:
            arm: Arm | None = None
            callees: list[PackageFunction] = []
            for call in _awaited_calls(function.node.body):
                callee = self.code.callee(call, function)
                if callee is None:
                    arm = _worse(arm, _direct_arm(call))
                else:
                    callees.append(callee)
            self._steps[key] = (arm, callees)
        return self._steps[key]

    def of_function(self, function: PackageFunction) -> Arm | None:
        """The worst arm among everything ``function`` awaits, walked without recursion."""
        key = id(function.node)
        if key in self._functions:
            return self._functions[key]
        arm: Arm | None = None
        seen = {key}
        pending = [function]
        while pending and arm != "grader":
            current = pending.pop()
            if id(current.node) in self._functions:
                arm = _worse(arm, self._functions[id(current.node)])
                continue
            direct, callees = self._step(current)
            arm = _worse(arm, direct)
            for callee in callees:
                if id(callee.node) not in seen:
                    seen.add(id(callee.node))
                    pending.append(callee)
        self._functions[key] = arm
        return arm


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """``except:``, ``except Exception`` or ``except BaseException``, alone or in a tuple."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        (isinstance(t, ast.Name) and t.id in _BROAD_EXCEPTIONS)
        or (isinstance(t, ast.Attribute) and t.attr in _BROAD_EXCEPTIONS)
        for t in types
    )


def _is_constant(node: ast.expr, names: frozenset[str]) -> bool:
    """A value fixed in the source: a number, a bool, ``None``, ``""``, or an upper-case module-level or imported name such as ``INCORRECT``.

    ``NAME.copy()`` of such a name counts too. ``names`` holds the module-level and imported names visible here.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    if isinstance(node, ast.Constant):
        return node.value is None or node.value == "" or isinstance(node.value, (bool, int, float))
    if (
        isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "copy"
    ):
        node = node.func.value
    return isinstance(node, ast.Name) and node.id.isupper() and node.id in names


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _holds_verdict(node: ast.expr) -> bool:
    """A dict literal with a bool or number among its values: ``{"criteria_met": False, ...}``."""
    return isinstance(node, ast.Dict) and any(
        isinstance(v, ast.Constant) and isinstance(v.value, (bool, int, float)) for v in node.values
    )


def _score_value(node: ast.expr) -> ast.expr | None:
    """The value a ``Score(...)`` call is given, when ``node`` is one."""
    if not (isinstance(node, ast.Call) and get_call_name(node) == "Score"):
        return None
    if node.args:
        return node.args[0]
    return next((k.value for k in node.keywords if k.arg == "value"), None)


def _constant_text(node: ast.expr, names: frozenset[str]) -> str | None:
    """How a message shows a constant result: ``0.0``, ``(False, ...)``, ``Score(value=INCORRECT)``; None for anything else."""
    if isinstance(node, ast.Tuple):
        if node.elts and _is_constant(node.elts[0], names):
            return f"({ast.unparse(node.elts[0])}, ...)"
        return None
    value = _score_value(node)
    if value is not None:
        return f"Score(value={ast.unparse(value)})" if _is_constant(value, names) else None
    return ast.unparse(node) if _is_constant(node, names) else None


def _returns_name(node: ast.expr, name: str) -> bool:
    """Whether a returned value is ``name``, a tuple holding it, or ``Score(value=name)``."""
    elements = node.elts if isinstance(node, ast.Tuple) else [_score_value(node) or node]
    return any(isinstance(e, ast.Name) and e.id == name for e in elements)


def _assigned(target: ast.expr, value: ast.expr) -> Iterable[tuple[str, ast.expr]]:
    """``(name, value)`` for each name an assignment binds to a value written beside it."""
    if isinstance(target, ast.Name):
        yield target.id, value
    elif (
        isinstance(target, ast.Tuple)
        and isinstance(value, ast.Tuple)
        and len(target.elts) == len(value.elts)
    ):
        for t, v in zip(target.elts, value.elts, strict=True):
            yield from _assigned(t, v)


def _checks_none(fn: FunctionNode, name: str) -> bool:
    """Whether ``fn`` tests ``name`` as a sentinel: ``name is None``, ``name is not None`` or ``not name``."""
    for node in scope_nodes(fn.body):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == name
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot))
            and _is_none(node.comparators[0])
        ):
            return True
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.Not)
            and isinstance(node.operand, ast.Name)
            and node.operand.id == name
        ):
            return True
    return False


def _callers_check_none(function: PackageFunction, code: ScorerCode) -> bool:
    """Whether every call to ``function`` in scorer code assigns the result to a name its caller tests for ``None``."""
    calls = code.calls.get(id(function.node), [])
    if not calls:
        return False
    for caller, call in calls:
        names = [
            target.id
            for node in scope_nodes(caller.node.body)
            if isinstance(node, ast.Assign)
            and (
                node.value is call
                or (isinstance(node.value, ast.Await) and node.value.value is call)
            )
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        if not any(_checks_none(caller.node, name) for name in names):
            return False
    return True


def _is_score_function(function: PackageFunction) -> bool:
    """Whether the function is defined directly in a ``@scorer``: its score function, where ``None`` records no score."""
    parent = function.enclosing[-1] if function.enclosing else None
    return (
        parent is not None
        and not isinstance(parent, ast.ClassDef)
        and _is_decorated(parent, "scorer")
    )


def _leaves_otherwise(handler: ast.ExceptHandler, names: frozenset[str]) -> bool:
    """Whether the handler leaves by ``continue`` or a ``return`` of something other than a constant."""
    return any(
        isinstance(node, ast.Continue)
        or (
            isinstance(node, ast.Return)
            and (node.value is None or _constant_text(node.value, names) is None)
        )
        for node in scope_nodes(handler.body)
    )


def _constant_result(
    handler: ast.ExceptHandler,
    statement: ast.Try | ast.TryStar,
    function: PackageFunction,
    code: ScorerCode,
) -> str | None:
    """How ``handler`` turns the failure into a result, for the message; None when it does not.

    It returns a constant; or it assigns one to a name the function returns
    after it; or it rebinds a name the ``try`` body bound, to a constant or a
    dict literal holding a bool or number, and the function reads the name
    after the ``try``. A ``None`` the function, or every caller of it, tests
    for is a sentinel and does not count, nor does one a scorer's score
    function returns, which records no score.
    """
    names = code.constant_names(function)
    for node in scope_nodes(handler.body):
        if isinstance(node, ast.Return) and node.value is not None:
            text = _constant_text(node.value, names)
            if text is not None and not (
                _is_none(node.value)
                and (_is_score_function(function) or _callers_check_none(function, code))
            ):
                return f"returns {text}"
    if _leaves_otherwise(handler, names):
        return None
    after = (statement.end_lineno or statement.lineno, statement.end_col_offset or 0)
    later = [
        node.value
        for node in scope_nodes(function.node.body)
        if isinstance(node, ast.Return) and node.value is not None and node.lineno > handler.lineno
    ]
    read_after = {
        node.id
        for node in scope_nodes(function.node.body)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and (node.lineno, node.col_offset) > after
    }
    bound_in_try = {
        node.id
        for node in scope_nodes(statement.body)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    for node in scope_nodes(handler.body):
        assignments: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.Assign):
            assignments = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments = [(node.target, node.value)]
        for target, value in assignments:
            for name, part in _assigned(target, value):
                if _is_none(part) and _checks_none(function.node, name):
                    continue
                if _is_constant(part, names) and any(_returns_name(r, name) for r in later):
                    return f"sets {name} = {ast.unparse(part)}"
                if (
                    name in bound_in_try
                    and name in read_after
                    and (_is_constant(part, names) or _holds_verdict(part))
                ):
                    shown = "{...}" if isinstance(part, ast.Dict) else ast.unparse(part)
                    return f"sets {name} = {shown}, which is read after the try"
    return None


def _has_raise(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for node in scope_nodes(handler.body))


_GUARDED = {"grader": "a grader call", "sandbox": "a sandbox exec()/read_file()"}

_FAILURE_HINT = {
    "grader": (
        "let the grader call's API errors propagate, so the sample errors and can be retried; "
        "when the grader replies without a usable verdict, retry the grader alone a bounded "
        "number of times (not from the cache), then return "
        'Score.unscored(reason="grader_failed"), or set NaN on the one key if the grader '
        "feeds a single key of a dict score"
    ),
    "sandbox": (
        "let an exception from exec()/read_file() propagate: it means the sandbox broke, so the "
        "sample errors and can be retried; score what a result shows instead: a non-zero exit "
        "is the model's code failing, and a missing file the agent was told to write is a "
        "verdict (reason no_response); don't turn a sandbox failure into a score or "
        "Score.unscored()"
    ),
}


@rule(
    code="IEBP012",
    name="scorer_failure_scored",
    category="best_practices",
    scopes=("eval", "helper"),
    summary="A scorer does not turn a grader or sandbox failure into a score",
    references=(
        inspect_docs("scoring-policy", "Scoring Policy: Malfunctions", "malfunctions"),
        inspect_docs("custom-scorers", "Custom Scorers: Unscored Samples", "unscored-samples"),
    ),
)
def scorer_failure_scored(ctx: LintContext) -> Iterable[Finding]:
    """A scorer does not turn a grader or sandbox failure into a score.

    ## What it does
    Flags a broad handler (``except:``, ``except Exception`` or ``except
    BaseException``, alone or in a tuple) with no ``raise`` in its body, in code
    a scorer runs, when both hold:

    - its ``try`` body awaits a grader call or a sandbox call. A grader call is
      a method call ending in ``.generate(``, or a call to something outside
      the package whose name contains ``judge`` or ``grader``, such as a
      ``model_graded_qa()`` scorer bound to ``grader``. A sandbox call is a
      method call ending in ``.exec(`` or ``.read_file(``. A package function
      counts by what it does, not its name: awaiting one counts when it awaits
      either kind of call, at any depth, outside a ``try`` of its own that
      swallows the failure;
    - the handler turns the failure into a result. It returns a constant; or
      it assigns one to a name the function returns after it; or it rebinds
      a name the ``try`` body binds, to a constant or a dict literal holding
      a bool or number (``{"criteria_met": False}``), and the function reads
      that name after the ``try``. A constant is a number, a bool, ``None``,
      ``""``, an upper-case name bound at module level or imported (such as
      ``INCORRECT``, or its ``.copy()``), a tuple starting with one, or
      ``Score(value=<constant>)``. A name is returned when the ``return``
      gives the name itself, a tuple holding it, or ``Score(value=<name>)``.

    These do not count as a result:

    - an assignment in a handler that then leaves by ``continue``, or by a
      ``return`` of anything but a constant, such as
      ``return Score.unscored(...)``;
    - a ``None`` that is tested as a sentinel: assigned in a function that
      tests ``name is None``, ``name is not None`` or ``not name``, or
      returned by a helper whose every caller in scorer code assigns the
      result to a name it tests that way;
    - a ``None`` returned by a scorer's own score function, which records
      no score.

    Code a scorer runs is every ``@scorer`` function, the functions nested in
    it, and the package functions they call, to any depth. A call is followed
    when its target is a function defined in an enclosing scope or at the top
    of the file, or a function imported from another module of the package
    (directly, as an attribute of an imported module, or through a
    re-export). A parameter or assignment of the same name in an enclosing
    function hides the definition. ``@tool`` functions are not followed: a
    tool returning an error string to the model is correct. Code no scorer
    calls, such as a solver or a health check, is not read.

    It is an error when the ``try`` guards a grader call. It is a warning
    when it guards only a sandbox call, whatever the handler returns, since
    some of those failures are the model's own: the agent never wrote the
    file it was asked to, or its code ran into the timeout. A handler
    narrowed to the error the model causes, such as
    ``except FileNotFoundError``, is not broad and is not reported. Mark a
    reviewed site with
    ``# inspect-evals-lint: ignore[scorer_failure_scored] -- <reason>`` on the
    ``except`` line.

    **Known limits:** a constant that reaches the result only through a
    derived value (``refused = "yes" in response`` with ``response`` not set
    in the ``try``) is not traced, nor is one bound through an alias, a
    walrus or an attribute. Method calls are not followed, so a judge
    class's ``check()`` called on an instance or a parameter
    (``refusal_judge.check_refusal(...)``) is not read; following those is a
    later step. Calls wrapped in ``asyncio.gather()`` or ``wait_for()`` are
    not seen as awaited. Nothing outside the package is followed.

    ## Why is this bad?
    A judge API error or a sandbox outage then reads as a real verdict: "not
    refused", "wrong answer", 0.0. The run looks healthy and the metric moves
    with the outage rate. Letting the error propagate fails the sample, which
    is visible and can be retried.

    What to do instead depends on what failed:

    - **The grader call raised** (an API error): let it propagate.
    - **The grader replied but gave no usable verdict**: retry the grader
      alone a bounded number of times, holding the model's output fixed (a
      retry with the same prompt and ``cache=True`` never reaches the
      grader), then return ``Score.unscored(reason="grader_failed")``. This
      holds when the grader is the model under test too, as in a self-judging
      defence. If the grader feeds one key of a dict score and the other keys
      are valid results, set that key to NaN instead of unscoring the sample.
    - **A sandbox call raised**: let it propagate; the sandbox broke. Score
      what a result shows instead: a non-zero exit is the model's code
      failing, and a missing file the agent was told to write is a verdict
      (``reason="no_response"``). A deterministic failure never becomes
      ``Score.unscored()``.

    ## Example
    ```python
    try:
        result = await grader.generate(prompt)
        return Score(value=parse_verdict(result.completion))
    except Exception:
        return Score(value=INCORRECT)
    ```
    Use instead:
    ```python
    result = await grader.generate(prompt)
    try:
        return Score(value=parse_verdict(result.completion))
    except ValueError:
        return Score.unscored(reason="grader_failed", explanation=result.completion)
    ```
    Or, where the grader feeds one key of a dict score:
    ```python
    try:
        refused = parse_verdict(result.completion)
    except ValueError:
        refused = float("nan")  # this key is ungraded; the other keys stand
    return Score(value={"score": task_score, "refused": refused})
    ```
    """
    parsed_files = parse_python_files(ctx)
    yield from parse_failures(parsed_files)

    code = ScorerCode.build(ctx, parsed_files.parsed)
    arms = _Arms(code)
    found: list[Diagnostic] = []
    for function in code.reached:
        for node in scope_nodes(function.node.body):
            if not isinstance(node, (ast.Try, ast.TryStar)):
                continue
            arm = arms.of_body(node.body, function)
            if arm is None:
                continue
            for handler in node.handlers:
                if not _is_broad(handler) or _has_raise(handler):
                    continue
                result = _constant_result(handler, node, function, code)
                if result is None:
                    continue
                severity: Severity = "error" if arm == "grader" else "warning"
                found.append(
                    Diagnostic(
                        f"broad except around {_GUARDED[arm]} in {function.node.name}() "
                        f"{result}, so the failure is scored instead of raised",
                        file=function.file.path,
                        line=handler.lineno,
                        column=column_of(handler),
                        severity=severity,
                        hint=_FAILURE_HINT[arm],
                    )
                )
    yield from sorted(found, key=lambda d: (str(d.file), d.line or 0))

    if found:
        return
    if not code.reached:
        yield Outcome("skip", "No @scorer functions found")
    else:
        yield Outcome(
            "pass",
            f"No broad except turns a grader or sandbox failure into a score in "
            f"{len(code.reached)} function(s) scorers run",
        )
