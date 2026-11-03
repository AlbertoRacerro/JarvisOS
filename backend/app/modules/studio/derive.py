"""Derive read-only mathematical documentation from user Python source (spec 188, item 6).

The source is only parsed with ``ast.parse`` and walked. It is never executed, imported
or evaluated. Output uses the existing typed MathML tree
(``{"tag", "children"?, "text"?}`` with tags math/mrow/mi/mn/mo/mtext/msup/msub/mfrac/msqrt)
that the frontend ``MathTree`` renderer displays. Constructs that cannot be rendered are
emitted as labelled ``code`` steps; they are never dropped or guessed.
"""
from __future__ import annotations

import ast
import hashlib
from typing import Any

Tree = dict[str, Any]

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "iota": "ι", "kappa": "κ", "lam": "λ", "lambda_": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "omicron": "ο", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_MODULES = {"math", "numpy", "np"}
_PLAIN_FUNCS = {"sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh"}
_ALL_FUNCS = _PLAIN_FUNCS | {"sqrt", "log", "log10", "log2", "exp", "abs", "fabs", "min", "max", "pow"}
_BARE_FUNCS = {"abs", "min", "max", "pow", "sqrt", "log", "log10", "log2", "exp"} | _PLAIN_FUNCS
_CONST_ATTRS = {"pi": "π", "e": "e"}
_CMP = {ast.Eq: "=", ast.NotEq: "≠", ast.Lt: "<", ast.LtE: "≤", ast.Gt: ">", ast.GtE: "≥"}

# Binding strength used to decide where parentheses are needed.
_P_OR, _P_AND, _P_NOT, _P_CMP, _P_ADD, _P_MUL, _P_UNARY, _P_POW, _P_ATOM = 1, 2, 3, 4, 5, 6, 7, 8, 10
_P_POWRES = 9


class _Unsupported(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _node(tag: str, *children: Tree, text: str | None = None) -> Tree:
    result: Tree = {"tag": tag}
    if children:
        result["children"] = list(children)
    if text is not None:
        result["text"] = text
    return result


def _mi(text: str) -> Tree:
    return _node("mi", text=text)


def _mn(text: str) -> Tree:
    return _node("mn", text=text)


def _mo(text: str) -> Tree:
    return _node("mo", text=text)


def _mtext(text: str) -> Tree:
    return _node("mtext", text=text)


def _row(*items: Tree) -> Tree:
    return _node("mrow", *items)


def _paren(item: Tree) -> Tree:
    return _row(_mo("("), item, _mo(")"))


def _symbol(name: str) -> Tree:
    base, sep, suffix = name.partition("_")
    if sep and base and suffix:
        return _node("msub", _mi(_GREEK.get(base, base)), _mi(suffix))
    return _mi(_GREEK.get(name, name))


def _tree(content: Tree) -> Tree:
    return _node("math", content if content["tag"] == "mrow" else _row(content))


def _call(head: Tree, args: list[Tree]) -> Tree:
    parts: list[Tree] = [head, _mo("(")]
    for index, arg in enumerate(args):
        if index:
            parts.append(_mo(","))
        parts.append(arg)
    parts.append(_mo(")"))
    return _row(*parts)


class _Renderer:
    def __init__(self, helpers: set[str]) -> None:
        self.helpers = helpers

    def render(self, node: ast.expr) -> Tree:
        try:
            return self._expr(node)[0]
        except RecursionError:
            raise _Unsupported("expression is nested too deeply") from None

    def _need(self, node: ast.expr, minimum: int) -> Tree:
        tree, prec = self._expr(node)
        return tree if prec >= minimum else _paren(tree)

    def _expr(self, node: ast.expr) -> tuple[Tree, int]:
        if isinstance(node, ast.Constant):
            return self._constant(node), _P_ATOM
        if isinstance(node, ast.Name):
            return _symbol(node.id), _P_ATOM
        if isinstance(node, ast.Attribute):
            symbol = self._module_attr(node)
            if symbol is None:
                raise _Unsupported(f"attribute access {ast.unparse(node)} cannot be mapped")
            return _mi(symbol), _P_ATOM
        if isinstance(node, ast.UnaryOp):
            return self._unary(node)
        if isinstance(node, ast.BinOp):
            return self._binop(node)
        if isinstance(node, ast.BoolOp):
            word, prec = ("and", _P_AND) if isinstance(node.op, ast.And) else ("or", _P_OR)
            parts: list[Tree] = []
            for index, value in enumerate(node.values):
                if index:
                    parts.append(_mtext(word))
                parts.append(self._need(value, prec + 1))
            return _row(*parts), prec
        if isinstance(node, ast.Compare):
            parts = [self._need(node.left, _P_CMP + 1)]
            for op, right in zip(node.ops, node.comparators, strict=True):
                symbol = _CMP.get(type(op))
                if symbol is None:
                    raise _Unsupported(f"comparison operator {type(op).__name__} is not rendered")
                parts += [_mo(symbol), self._need(right, _P_CMP + 1)]
            return _row(*parts), _P_CMP
        if isinstance(node, ast.Call):
            return self._call(node), _P_ATOM
        raise _Unsupported(f"{type(node).__name__} expression is not rendered")

    @staticmethod
    def _constant(node: ast.Constant) -> Tree:
        value = node.value
        if isinstance(value, bool) or value is None:
            return _mtext(str(value))
        if isinstance(value, (int, float)):
            try:
                return _mn(repr(value))
            except ValueError:
                raise _Unsupported("numeric literal is too large to render") from None
        if isinstance(value, str):
            return _mtext(value)
        raise _Unsupported(f"{type(value).__name__} literal is not rendered")

    @staticmethod
    def _module_attr(node: ast.Attribute) -> str | None:
        if isinstance(node.value, ast.Name) and node.value.id in _MODULES and node.attr in _CONST_ATTRS:
            return _CONST_ATTRS[node.attr]
        return None

    def _unary(self, node: ast.UnaryOp) -> tuple[Tree, int]:
        if isinstance(node.op, ast.USub):
            return _row(_mo("−"), self._need(node.operand, _P_UNARY)), _P_UNARY
        if isinstance(node.op, ast.UAdd):
            return _row(_mo("+"), self._need(node.operand, _P_UNARY)), _P_UNARY
        if isinstance(node.op, ast.Not):
            return _row(_mo("¬"), self._need(node.operand, _P_NOT + 1)), _P_NOT
        raise _Unsupported("bitwise invert is not rendered")

    def _binop(self, node: ast.BinOp) -> tuple[Tree, int]:
        op = node.op
        if isinstance(op, (ast.Add, ast.Sub)):
            right_min = _P_ADD if isinstance(op, ast.Add) else _P_MUL
            symbol = "+" if isinstance(op, ast.Add) else "−"
            return _row(self._need(node.left, _P_ADD), _mo(symbol), self._need(node.right, right_min)), _P_ADD
        if isinstance(op, ast.Mult):
            right_min = _P_POW if isinstance(node.right, ast.UnaryOp) else _P_MUL
            return _row(self._need(node.left, _P_MUL), _mo("·"), self._need(node.right, right_min)), _P_MUL
        if isinstance(op, ast.Mod):
            return _row(self._need(node.left, _P_MUL), _mo("mod"), self._need(node.right, _P_MUL + 1)), _P_MUL
        if isinstance(op, ast.Div):
            return _node("mfrac", self._expr(node.left)[0], self._expr(node.right)[0]), _P_ATOM
        if isinstance(op, ast.FloorDiv):
            frac = _node("mfrac", self._expr(node.left)[0], self._expr(node.right)[0])
            return _row(_mo("⌊"), frac, _mo("⌋")), _P_ATOM
        if isinstance(op, ast.Pow):
            return self._power(node.left, node.right), _P_POWRES
        raise _Unsupported(f"operator {type(op).__name__} is not rendered")

    def _power(self, base: ast.expr, exponent: ast.expr) -> Tree:
        return _node("msup", self._need(base, _P_ATOM), self._expr(exponent)[0])

    def _call(self, node: ast.Call) -> Tree:
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise _Unsupported("call with keyword or starred arguments is not rendered")
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Name):
            if func.id in self.helpers:
                return _call(_mi(func.id), [self._expr(arg)[0] for arg in node.args])
            if func.id in _BARE_FUNCS:
                name = func.id
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                and func.value.id in _MODULES and func.attr in _ALL_FUNCS:
            name = func.attr
        if name is None:
            raise _Unsupported(f"call to {ast.unparse(func)} cannot be mapped")
        return self._math_func(name, node.args)

    def _math_func(self, name: str, args: list[ast.expr]) -> Tree:
        def arity(low: int, high: int) -> None:
            if not low <= len(args) <= high:
                raise _Unsupported(f"{name} called with an unsupported number of arguments")

        if name == "pow":
            arity(2, 2)
            return self._power(args[0], args[1])
        if name in ("min", "max"):
            if len(args) < 2:
                raise _Unsupported(f"{name} of a single iterable is not rendered")
            return _call(_mi(name), [self._expr(arg)[0] for arg in args])
        if name == "log" and len(args) == 2:
            return _call(_node("msub", _mi("log"), self._expr(args[1])[0]), [self._expr(args[0])[0]])
        arity(1, 1)
        arg = self._expr(args[0])[0]
        if name == "sqrt":
            return _node("msqrt", arg)
        if name == "exp":
            return _node("msup", _mi("e"), arg)
        if name in ("abs", "fabs"):
            return _row(_mo("|"), arg, _mo("|"))
        if name == "log":
            return _call(_mi("ln"), [arg])
        if name in ("log10", "log2"):
            return _call(_node("msub", _mi("log"), _mn(name[3:])), [arg])
        return _call(_mi(name), [arg])


def _name_key(name: str) -> tuple[str, str]:
    return ("name", name)


class _Deriver:
    def __init__(self, source: str, helper_defs: dict[str, ast.FunctionDef]) -> None:
        self.source = source
        self.renderer = _Renderer(set(helper_defs))

    # ---- helpers
    def _segment(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or ast.unparse(node)

    def _code(self, node: ast.AST, reason: str, text: str | None = None) -> Tree:
        return {"kind": "code", "text": text if text is not None else self._segment(node),
                "reason": reason, "line": getattr(node, "lineno", 0)}

    @staticmethod
    def _equation(target: Tree, expr: Tree, line: int) -> Tree:
        return {"kind": "equation", "target": _tree(target), "expr": _tree(expr), "line": line}

    def _pair(self, target: Tree, value: ast.expr, line: int, stmt: ast.AST, label: str | None = None) -> list[Tree]:
        cases = self._ifexp_cases(value)
        try:
            if cases is not None:
                return [self._piecewise(target, cases, line)]
            return [self._equation(target, self.renderer.render(value), line)]
        except _Unsupported as error:
            text = self._segment(value) if label is None else f"{label}: {self._segment(value)}"
            return [self._code(stmt, error.reason, text)]

    @staticmethod
    def _ifexp_cases(value: ast.expr) -> list[tuple[ast.expr | None, ast.expr]] | None:
        if not isinstance(value, ast.IfExp):
            return None
        cases: list[tuple[ast.expr | None, ast.expr]] = []
        current: ast.expr = value
        while isinstance(current, ast.IfExp):
            cases.append((current.test, current.body))
            current = current.orelse
        cases.append((None, current))
        return cases

    def _piecewise(self, target: Tree, cases: list[tuple[ast.expr | None, ast.expr]], line: int) -> Tree:
        rendered = [{"condition": None if test is None else _tree(self.renderer.render(test)),
                     "expr": _tree(self.renderer.render(expr))} for test, expr in cases]
        return {"kind": "conditional", "form": "piecewise", "target": _tree(target), "cases": rendered, "line": line}

    # ---- statements
    def steps(self, body: list[ast.stmt]) -> list[Tree]:
        out: list[Tree] = []
        for index, stmt in enumerate(body):
            if index == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                    and isinstance(stmt.value.value, str):
                continue  # docstring is reported separately
            if isinstance(stmt, ast.Pass):
                continue
            out.extend(self._stmt(stmt))
        return out

    def _stmt(self, stmt: ast.stmt) -> list[Tree]:
        if isinstance(stmt, ast.Assign):
            return self._assign(stmt)
        if isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                return self._pair(_symbol(stmt.target.id), stmt.value, stmt.lineno, stmt)
            return [self._code(stmt, "annotation without a simple value is not rendered")]
        if isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                combined = ast.BinOp(left=ast.Name(id=stmt.target.id, ctx=ast.Load()), op=stmt.op, right=stmt.value)
                return self._pair(_symbol(stmt.target.id), combined, stmt.lineno, stmt)
            return [self._code(stmt, "augmented assignment to a non-name target is not rendered")]
        if isinstance(stmt, ast.Return):
            return self._return(stmt)
        if isinstance(stmt, ast.If):
            return self._if(stmt)
        if isinstance(stmt, ast.Expr):
            return [self._code(stmt, "expression statement is not rendered")]
        return [self._code(stmt, f"{type(stmt).__name__} statement is not rendered")]

    def _assign(self, stmt: ast.Assign) -> list[Tree]:
        out: list[Tree] = []
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                out.extend(self._pair(_symbol(target.id), stmt.value, stmt.lineno, stmt))
            elif isinstance(target, ast.Tuple) and isinstance(stmt.value, ast.Tuple) \
                    and len(target.elts) == len(stmt.value.elts) \
                    and all(isinstance(item, ast.Name) for item in target.elts):
                for item, value in zip(target.elts, stmt.value.elts, strict=True):
                    assert isinstance(item, ast.Name)
                    out.extend(self._pair(_symbol(item.id), value, stmt.lineno, stmt))
            else:
                out.append(self._code(stmt, "assignment target is not a simple name"))
        return out

    def _return(self, stmt: ast.Return) -> list[Tree]:
        value = stmt.value
        if value is None:
            return [self._code(stmt, "return without a value is not rendered")]
        if isinstance(value, ast.Dict):
            if not all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in value.keys):
                return [self._code(stmt, "dict return with non-literal or unpacked keys is not rendered")]
            out: list[Tree] = []
            for key, item in zip(value.keys, value.values, strict=True):
                assert isinstance(key, ast.Constant)
                if isinstance(item, ast.Name) and item.id == key.value:
                    continue  # ``"x": x`` restates an equation already shown above
                out.extend(self._pair(_symbol(str(key.value)), item, stmt.lineno, stmt, label=repr(key.value)))
            return out
        if isinstance(value, ast.Tuple):
            out = []
            for index, item in enumerate(value.elts):
                out.extend(self._pair(_mtext(f"return[{index}]"), item, stmt.lineno, stmt))
            return out
        return self._pair(_mtext("return"), value, stmt.lineno, stmt)

    def _single_target(self, body: list[ast.stmt]) -> tuple[tuple[str, str], Tree, ast.expr] | None:
        if len(body) != 1:
            return None
        stmt = body[0]
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            return _name_key(name), _symbol(name), stmt.value
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            return _name_key(stmt.target.id), _symbol(stmt.target.id), stmt.value
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            combined = ast.BinOp(left=ast.Name(id=stmt.target.id, ctx=ast.Load()), op=stmt.op, right=stmt.value)
            return _name_key(stmt.target.id), _symbol(stmt.target.id), combined
        if isinstance(stmt, ast.Return) and stmt.value is not None \
                and not isinstance(stmt.value, (ast.Dict, ast.Tuple, ast.IfExp)):
            return ("return", ""), _mtext("return"), stmt.value
        return None

    def _if(self, stmt: ast.If) -> list[Tree]:
        chain: list[tuple[ast.expr | None, list[ast.stmt]]] = []
        current = stmt
        while True:
            chain.append((current.test, current.body))
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            if current.orelse:
                chain.append((None, current.orelse))
            break
        try:
            conditions: list[Tree | None] = [None if test is None else _tree(self.renderer.render(test))
                                             for test, _ in chain]
        except _Unsupported as error:
            return [self._code(stmt, f"condition is not rendered: {error.reason}")]
        if chain[-1][0] is None:
            piecewise = self._try_piecewise(chain, stmt.lineno)
            if piecewise is not None:
                return [piecewise]
        branches: list[Tree] = []
        for index, ((test, body), condition) in enumerate(zip(chain, conditions, strict=True)):
            label = "else" if test is None else ("if" if index == 0 else "elif")
            branches.append({"label": label, "condition": condition, "steps": self.steps(body)})
        return [{"kind": "conditional", "form": "branches", "branches": branches, "line": stmt.lineno}]

    def _try_piecewise(self, chain: list[tuple[ast.expr | None, list[ast.stmt]]], line: int) -> Tree | None:
        found = [self._single_target(body) for _, body in chain]
        if any(item is None for item in found):
            return None
        entries = [item for item in found if item is not None]
        if any(item[0] != entries[0][0] for item in entries):
            return None
        cases = [(test, item[2]) for (test, _), item in zip(chain, entries, strict=True)]
        try:
            return self._piecewise(entries[0][1], cases, line)
        except _Unsupported:
            return None


def _unrendered(steps: list[Tree]) -> int:
    total = 0
    for step in steps:
        if step["kind"] == "code":
            total += 1
        elif step["kind"] == "conditional":
            for branch in step.get("branches", []):
                total += _unrendered(branch["steps"])
    return total


def _arg_names(args: ast.arguments) -> list[str]:
    names = [item.arg for item in [*args.posonlyargs, *args.args]]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names += [item.arg for item in args.kwonlyargs]
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def _called_helpers(func: ast.FunctionDef, defs: dict[str, ast.FunctionDef]) -> list[str]:
    found: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in defs \
                and node.func.id not in found:
            found.append(node.func.id)
    return found


def _module_constants(module: ast.Module, deriver: _Deriver) -> list[Tree]:
    out: list[Tree] = []

    def numeric(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        if isinstance(node, ast.UnaryOp):
            return isinstance(node.op, (ast.USub, ast.UAdd)) and numeric(node.operand)
        if isinstance(node, ast.BinOp):
            return numeric(node.left) and numeric(node.right)
        return False

    for stmt in module.body:
        value: ast.expr | None = None
        name: str | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name, value = stmt.target.id, stmt.value
        if name is None or value is None:
            continue
        try:
            if numeric(value):
                out.append(deriver._equation(_symbol(name), deriver.renderer.render(value), stmt.lineno))
        except (_Unsupported, RecursionError):
            continue
    return out


def _empty(digest: str, status: str) -> dict[str, Any]:
    return {"source_sha256": digest, "status": status, "functions": [], "module_constants": [],
            "unrendered_count": 0}


def derive_documentation(source: str, entry: str = "evaluate") -> dict[str, Any]:
    """Return read-only documentation for ``entry`` and its helpers. Never executes ``source``."""
    digest = hashlib.sha256(source.encode("utf-8", errors="surrogatepass")).hexdigest()
    try:
        module = ast.parse(source)
    except SyntaxError as error:
        result = _empty(digest, "syntax_error")
        result["error"] = {"message": error.msg, "line": error.lineno}
        return result
    except (ValueError, RecursionError, MemoryError) as error:
        result = _empty(digest, "syntax_error")
        result["error"] = {"message": str(error) or type(error).__name__, "line": None}
        return result

    defs: dict[str, ast.FunctionDef] = {}
    for stmt in module.body:
        if isinstance(stmt, ast.FunctionDef):
            defs[stmt.name] = stmt
    deriver = _Deriver(source, defs)
    constants = _module_constants(module, deriver)
    if entry not in defs:
        result = _empty(digest, "entry_missing")
        result["module_constants"] = constants
        return result

    order: list[str] = [entry]
    index = 0
    while index < len(order):
        for name in _called_helpers(defs[order[index]], defs):
            if name not in order:
                order.append(name)
        index += 1

    functions: list[dict[str, Any]] = []
    unrendered = 0
    for name in order:
        func = defs[name]
        steps = deriver.steps(func.body)
        unrendered += _unrendered(steps)
        functions.append({"name": name, "args": _arg_names(func.args),
                          "docstring": ast.get_docstring(func), "steps": steps})
    return {"source_sha256": digest, "status": "ok", "functions": functions,
            "module_constants": constants, "unrendered_count": unrendered}
