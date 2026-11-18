"""Classify model lookups by when their statically resolved callers execute."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Literal

from inspect_evals_lint.context import LintContext
from inspect_evals_lint.rules._ast import ParsedFile, get_call_name, get_decorator_name
from inspect_evals_lint.rules.scoring import PackageFunction, ScorerCode

Phase = Literal["construction", "factory", "runtime"]
_COMPONENTS = frozenset({"solver", "scorer", "agent"})
_LOOKUPS = frozenset({"get_model", "model_roles"})


@dataclass
class ModelLookup:
    """A lookup and the execution phases reached through known calls."""

    file: ParsedFile
    node: ast.Call
    phases: set[Phase] = field(default_factory=set)

    @property
    def runtime_only(self) -> bool:
        return self.phases == {"runtime"}


def model_lookups(ctx: LintContext, files: list[ParsedFile]) -> list[ModelLookup]:
    """Find lookups, following package helpers and returned component callbacks.

    The package resolver is shared with scoring rules. Its reachability list
    includes factory bodies, so timing is computed separately here.
    """
    return _LookupTiming(ScorerCode.build(ctx, files), files).analyse()


class _LookupTiming:
    def __init__(self, code: ScorerCode, files: list[ParsedFile]) -> None:
        self.code = code
        self.files = files
        self.lookups: dict[int, ModelLookup] = {}
        self.pending: list[tuple[PackageFunction, Phase]] = []
        self.visited: set[tuple[int, Phase]] = set()

    def analyse(self) -> list[ModelLookup]:
        for parsed in self.files:
            if not isinstance(parsed.tree, ast.Module):
                continue
            module = next(m for m, tree in self.code.modules.items() if tree is parsed.tree)
            dummy = ast.parse("def _module_scope(): pass").body[0]
            assert isinstance(dummy, ast.FunctionDef)
            caller = PackageFunction(dummy, parsed, module, ())
            self._collect(parsed.tree, caller)
            for statement in parsed.tree.body:
                self._walk(statement, caller, "construction")

        while self.pending:
            function, phase = self.pending.pop()
            key = (id(function.node), phase)
            if key in self.visited:
                continue
            self.visited.add(key)
            for statement in function.node.body:
                self._walk(statement, function, phase)

        return sorted(self.lookups.values(), key=lambda v: (str(v.file.path), v.node.lineno))

    def _collect(self, node: ast.AST, caller: PackageFunction) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for header in _function_header(node):
                self._collect(header, caller)
            nested = self.code.functions[id(node)]
            for statement in node.body:
                self._collect(statement, nested)
            return
        if (
            isinstance(node, ast.Call)
            and get_call_name(node) in _LOOKUPS
            and self.code.callee(node, caller) is None
        ):
            self.lookups[id(node)] = ModelLookup(caller.file, node)
        for child in ast.iter_child_nodes(node):
            self._collect(child, caller)

    def _walk(self, node: ast.AST, caller: PackageFunction, phase: Phase) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for header in _function_header(node):
                self._walk(header, caller, phase)
            decorators = {get_decorator_name(d) for d in node.decorator_list}
            function = self.code.functions[id(node)]
            if decorators & _COMPONENTS:
                self.pending.append((function, "factory"))
            elif "task" in decorators:
                self.pending.append((function, "construction"))
            return
        if isinstance(node, ast.Lambda):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self._walk(default, caller, phase)
            return
        if isinstance(node, ast.Call):
            if lookup := self.lookups.get(id(node)):
                lookup.phases.add(phase)
            callee = self.code.callee(node, caller)
            if callee is not None:
                self.pending.append((callee, phase))
            elif isinstance(node.func, ast.Lambda):
                self._walk(node.func.body, caller, phase)
        if isinstance(node, ast.Return) and phase in ("factory", "runtime"):
            self._returned_callback(node.value, caller)
        for child in ast.iter_child_nodes(node):
            self._walk(child, caller, phase)

    def _returned_callback(self, value: ast.expr | None, caller: PackageFunction) -> None:
        if isinstance(value, (ast.Name, ast.Attribute)):
            call = ast.Call(func=value, args=[], keywords=[])
            callee = self.code.callee(call, caller)
            if callee is not None:
                self.pending.append((callee, "runtime"))
        elif isinstance(value, ast.IfExp):
            self._returned_callback(value.body, caller)
            self._returned_callback(value.orelse, caller)
        elif isinstance(value, ast.Lambda):
            self._walk(value.body, caller, "runtime")


def _function_header(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Expressions evaluated by a definition, before its body is called."""
    args = fn.args
    annotations = [
        arg.annotation
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if arg is not None and arg.annotation is not None
    ]
    return [
        *fn.decorator_list,
        *args.defaults,
        *(d for d in args.kw_defaults if d is not None),
        *annotations,
        *([fn.returns] if fn.returns is not None else []),
    ]
