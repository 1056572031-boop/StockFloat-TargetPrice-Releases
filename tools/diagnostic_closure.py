"""Lexically resolve reachable diagnostic helpers without executing product code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import symtable
from typing import Iterator


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Definition bodies have their own scope; defaults execute in the parent."""
    yield node
    children: list[ast.AST]
    if isinstance(node, _FUNCTIONS):
        children = [*node.decorator_list, *node.args.defaults,
                    *(value for value in node.args.kw_defaults if value is not None)]
    elif isinstance(node, ast.ClassDef):
        children = [*node.decorator_list, *node.bases, *node.keywords]
    elif isinstance(node, ast.Lambda):
        children = list(node.args.defaults)
    elif isinstance(node, _COMPREHENSIONS):
        children = [node.generators[0].iter]
    else:
        children = list(ast.iter_child_nodes(node))
    for child in children:
        yield from _nodes(child)


@dataclass(frozen=True)
class _Binding:
    module: str
    name: str = ""
    node: ast.AST | None = None
    owner: _Scope | None = None


class _Scope:
    def __init__(self, module: str, node: ast.AST, table: symtable.SymbolTable,
                 parent: _Scope | None = None):
        self.module, self.node, self.table, self.parent = module, node, table, parent
        self.bindings: dict[str, _Binding | None] = {}
        self.blocked: set[str] = set()
        body: list[ast.AST]
        if isinstance(node, ast.Lambda):
            body = [node.body]
        elif isinstance(node, _COMPREHENSIONS):
            body = ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
            for index, generator in enumerate(node.generators):
                body += [generator.target, *generator.ifs]
                if index:
                    body.append(generator.iter)
        elif isinstance(node, (ast.Module, ast.ClassDef, *_FUNCTIONS)):
            body = list(node.body)
        else:
            body = []
        self.nodes = [child for statement in body for child in _nodes(statement)]
        for child in self.nodes:
            if isinstance(child, (ast.Name,)) and isinstance(child.ctx, (ast.Store, ast.Del)):
                self.blocked.add(child.id)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                self.blocked.add(child.name)
            elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
                self.blocked.add(child.name)
            elif isinstance(child, ast.MatchMapping) and child.rest:
                self.blocked.add(child.rest)
            elif isinstance(child, (*_FUNCTIONS, ast.ClassDef)):
                self.bind(child.name, _Binding(module, node=child, owner=self))
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    self.bind(alias.asname or alias.name.split(".")[0], _Binding(alias.name))
            elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                for alias in child.names:
                    self.bind(alias.asname or alias.name, _Binding(child.module, alias.name))

    def bind(self, name: str, value: _Binding) -> None:
        if name not in self.bindings:
            self.bindings[name] = value
        elif self.bindings[name] != value:
            self.bindings[name] = None  # Ambiguous bindings must not certify coverage.

    def lookup(self, name: str) -> _Binding | None:
        if name in self.blocked:
            return None
        try:
            symbol = self.table.lookup(name)
        except KeyError:
            return None
        if self.parent is not None and symbol.is_global():
            scope = self
            while scope.parent is not None:
                scope = scope.parent
            return scope.lookup(name)
        if symbol.is_free() or symbol.is_nonlocal():
            parent = self.parent
            # Class namespaces are not lexical parents of method bodies.
            if parent is not None and isinstance(parent.node, ast.ClassDef):
                parent = parent.parent
            return parent.lookup(name) if parent is not None else None
        if symbol.is_parameter():
            return None
        return self.bindings.get(name)

    def receiver(self, name: str) -> bool:
        if name not in ("self", "window") or name in self.blocked:
            return False
        try:
            symbol = self.table.lookup(name)
        except KeyError:
            return False
        if symbol.is_parameter():
            return True
        if symbol.is_free() and self.parent is not None:
            return self.parent.receiver(name)
        return False


def diagnostic_modules(sources: dict[str, str]) -> set[str]:
    """Trace calls and callback references with the same lexical binding rules."""
    scopes: dict[tuple[str, int], _Scope] = {}
    modules: dict[str, _Scope] = {}
    parents: dict[int, ast.AST] = {}
    for module, source in sources.items():
        tree = ast.parse(source)
        modules[module] = _Scope(module, tree, symtable.symtable(source, module, "exec"))
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[id(child)] = node

    def scope_for(binding: _Binding) -> _Scope | None:
        node, owner = binding.node, binding.owner
        if not isinstance(node, (ast.expr, ast.stmt)) or owner is None:
            return None
        key = (binding.module, id(node))
        if key not in scopes:
            name = (node.name if isinstance(node, (*_FUNCTIONS, ast.ClassDef)) else
                    "lambda" if isinstance(node, ast.Lambda) else
                    {ast.ListComp: "listcomp", ast.SetComp: "setcomp",
                     ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr"}.get(type(node), ""))
            candidates = [table for table in owner.table.get_children()
                          if table.get_name() == name and table.get_lineno() == node.lineno]
            if len(candidates) != 1:
                return None
            scopes[key] = _Scope(binding.module, node, candidates[0], owner)
        return scopes[key]

    def resolve(binding: _Binding | None, seen: set[tuple[str, str]] | None = None) -> _Binding | None:
        if binding is None or binding.node is not None or not binding.name:
            return binding
        seen = set() if seen is None else seen
        key = (binding.module, binding.name)
        if key in seen or binding.module not in modules:
            return None
        seen.add(key)
        return resolve(modules[binding.module].lookup(binding.name), seen)

    def expression(node: ast.AST, scope: _Scope) -> _Binding | None:
        if isinstance(node, ast.Name):
            return scope.lookup(node.id)
        if isinstance(node, ast.Attribute):
            owner = resolve(expression(node.value, scope))
            if owner is not None and owner.node is None and not owner.name:
                return _Binding(owner.module, node.attr)
        return None

    methods: dict[str, _Binding] = {}
    classes_seen: set[int] = set()

    def class_methods(binding: _Binding | None) -> None:
        binding = resolve(binding)
        if binding is None or not isinstance(binding.node, ast.ClassDef) or id(binding.node) in classes_seen:
            return
        classes_seen.add(id(binding.node))
        scope = scope_for(binding)
        if scope is None:
            return
        for name, method in scope.bindings.items():
            if method is not None and isinstance(method.node, _FUNCTIONS):
                methods.setdefault(name, method)
        for base in binding.node.bases:
            class_methods(expression(base, binding.owner or scope))

    entry = modules.get("stock_float_window")
    if entry is None:
        return set()
    class_methods(entry.lookup("FloatWindow"))
    reached: set[str] = set()
    visited: set[tuple[str, int]] = set()

    def follow(binding: _Binding | None) -> None:
        if binding is None:
            return
        if binding.module in modules:
            reached.add(binding.module)
        target = resolve(binding)
        if target is not None:
            reached.add(target.module)
            if isinstance(target.node, (*_FUNCTIONS, ast.Lambda)):
                scan(scope_for(target))

    def scan(scope: _Scope | None) -> None:
        if scope is None:
            return
        key = (scope.module, id(scope.node))
        if key in visited:
            return
        visited.add(key)
        for node in scope.nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                follow(scope.lookup(node.id))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                follow(expression(node, scope))
                if isinstance(node.value, ast.Name) and scope.receiver(node.value.id):
                    follow(methods.get(node.attr))
            elif isinstance(node, ast.Lambda):
                if not isinstance(parents.get(id(node)), (ast.Assign, ast.AnnAssign)):
                    scan(scope_for(_Binding(scope.module, node=node, owner=scope)))
            elif isinstance(node, (*_COMPREHENSIONS, ast.ClassDef)):
                scan(scope_for(_Binding(scope.module, node=node, owner=scope)))

    export = methods.get("_export_diagnostics")
    if export is None:
        return set()
    follow(export)
    return reached.intersection(modules)
