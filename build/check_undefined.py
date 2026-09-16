#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零依赖 AST 未定义名检查器
============================================================================

解决一个具体的失效模式：**语法合法但引用了不存在的函数**。

并行编辑/重构时误删一个函数、却留下对它的调用，`py_compile` 查不出来
（语法是合法的），单测也未必覆盖到那条分支。这个 bug 只会在真正跑到那一行时
炸掉 —— 而那一行往往在流水线的很后面，等发现时已经烧掉几十分钟容器构建。

做法：先把每个作用域里"被绑定的名字"收全（函数/类/import/赋值/for/with/
except/推导式/函数形参），再找 Load 上下文但全作用域链上都查不到的名字。

刻意偏保守：宁可漏报也不误报。误报会让 CI 变噪音，最后被 --ignore 掉。
所以嵌套函数里绑定的名字一律当作在外层可见（宽松），
只有"这个名字在整棵树里根本没被绑定过"才报。

用法：
    python check_undefined.py build_bundle.py [more.py ...]
退出码非 0 表示发现可疑引用。
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

BUILTINS = frozenset(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__", "WindowsError",
}


class Scope:
    def __init__(self, parent: "Scope | None" = None):
        self.names: set[str] = set()
        self.parent = parent

    def has(self, name: str) -> bool:
        s: Scope | None = self
        while s is not None:
            if name in s.names:
                return True
            s = s.parent
        return False

    def add_args(self, args: ast.arguments) -> None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self.names.add(a.arg)
        if args.vararg:
            self.names.add(args.vararg.arg)
        if args.kwarg:
            self.names.add(args.kwarg.arg)


def bind_target(node: ast.AST, scope: Scope) -> None:
    """把赋值/for/with 目标里绑定的名字收进作用域。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            scope.names.add(sub.id)
        elif isinstance(sub, ast.arg):
            scope.names.add(sub.arg)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.names.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                scope.names.add((a.asname or a.name).split(".")[0])
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            scope.names.add(sub.name)
        elif isinstance(sub, ast.Global | ast.Nonlocal):
            scope.names.update(sub.names)


def bind_all(body: list[ast.stmt], scope: Scope) -> None:
    """把一段语句里所有会绑定名字的构造一次性收全（支持前向引用）。"""
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.names.add(sub.name)
                scope.add_args(sub.args)
            elif isinstance(sub, ast.ClassDef):
                scope.names.add(sub.name)
            elif isinstance(sub, ast.Lambda):
                scope.add_args(sub.args)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    scope.names.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                scope.names.add(sub.name)
            elif isinstance(sub, ast.Global | ast.Nonlocal):
                scope.names.update(sub.names)
            elif isinstance(sub, ast.comprehension):
                bind_target(sub.target, scope)
            elif isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign,
                                  ast.For, ast.AsyncFor, ast.withitem, ast.NamedExpr)):
                for t in targets_of(sub):
                    bind_target(t, scope)


def targets_of(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign):
        return [node.target] if node.target else []
    if isinstance(node, ast.AugAssign):
        return [node.target]
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, ast.withitem) and node.optional_vars:
        return [node.optional_vars]
    if isinstance(node, ast.NamedExpr):
        return [node.target]
    return []


def check(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = Scope()
    module.names.update(BUILTINS)
    bind_all(tree.body, module)

    problems: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if not module.has(node.id):
                problems.add((node.lineno, node.id))
    return sorted(problems)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total = 0
    for arg in argv:
        p = Path(arg)
        if not p.exists():
            print(f"✗ 文件不存在: {p}")
            total += 1
            continue
        try:
            problems = check(p)
        except SyntaxError as e:
            print(f"✗ {p.name}: 语法错误 {e}")
            total += 1
            continue
        if problems:
            total += len(problems)
            print(f"✗ {p.name}: {len(problems)} 处可疑引用")
            for lineno, name in problems:
                print(f"    {p.name}:{lineno}  →  {name!r} 未定义")
        else:
            print(f"✓ {p.name}: 无未定义名")
    if total:
        print(f"\n共 {total} 处，请修掉再跑构建 —— 这类错误会在流水线后段才炸。")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
