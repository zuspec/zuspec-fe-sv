"""Tracing: following values through the interpreter symbolically.

A host puts :class:`~zuspec.fe.sv.interp.values.Sym` values (a concrete
value plus a leaf expression) where it wants a value followed, and runs the
code as usual. The interpreter computes concretely and, alongside, builds
Zuspec IR expressions over the leaves (*concolic* execution). Control flow
that depends on a traced value is reported as a :class:`CtlFrame`:

- a ``for``/``foreach`` whose bounds depend on a traced value is a
  *traced loop*: its variable becomes a leaf of its own (a *genvar*,
  ``ExprRefLocal``), so the code in the body is traced over it;
- an ``if`` on a traced condition is a *traced branch*;
- any other control on a traced value (``while``, ``case``, an irregular
  ``for``, a ``break`` out of a traced loop) is *opaque*.

Every frame has a :attr:`CtlFrame.key` (the statement's source position) and
its enclosing frames (:attr:`CtlFrame.outer`), so a host can tell the same
construct apart across runs. A host that wants the arm of a traced ``if``
that did not run runs the code again and picks the arm in
:meth:`Intrinsics.ctl_force`; the frame then has ``forced`` set.

Types are traced from their syntax: a class parameter that reaches a type
(``fw_input #(bit [W-1:0])``) is folded away by the compiler, so
:class:`SynTracer` re-reads the type as written at a call site
(``C #(args)::f()``) or in a typedef, valuing its names as the interpreter
would (:meth:`Interpreter.param_value`: class parameters through
:meth:`Intrinsics.symbolic_param`). The result is a :class:`TType`.

While a frame is active, a write of a non-handle value to a variable
declared outside it taints the value (its expression becomes None): the
value then depends on which iterations or arm ran. Each frame records
whether a part of its code that did *not* run (a loop that ran no
iteration, the arm not taken) may have effects a host cares about, which
is what a host needs to decide whether the run it saw covers every
parameter value.

Host-initiated calls (:meth:`Interpreter.call`) start with no active frames.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

import zuspec.ir.core as ir
from pyslang import ast

from .values import Obj, Sym, Struct

__all__ = ["CtlFrame", "TType", "SynTracer", "TAINTED", "const", "leaves", "may_call",
           "node_key"]

EK = ast.ExpressionKind


class CtlFrame:
    """One control construct that depends on a traced value.

    :ivar kind: ``"loop"``, ``"if"`` or ``"opaque"``.
    :ivar node: the statement (pyslang AST).
    :ivar where: its source position, for messages.
    :ivar var: a loop's variable name; :ivar leaf: its genvar leaf
        (``ExprRefLocal``); :ivar start: the start value (an Expr);
        :ivar cond: the continuation condition over ``leaf`` (an Expr, or
        None if it could not be traced).
    :ivar value: the loop variable's concrete value in the current
        iteration; :ivar iteration: the iteration count, from 0;
        :ivar count: when the frame has ended, how many iterations ran.
    :ivar taken: for ``"if"``, whether the true arm runs.
    :ivar reason: for ``"opaque"``, what made it so.
    :ivar unexplored: set when the frame ends: code that did not run (no
        iteration of a loop, the arm not taken) contains a call or ``new``
        (for an ``if``: or an assignment, :func:`may_effect`).
    :ivar key: the statement's source position (:func:`node_key`), the same
        in every run of the same code.
    :ivar outer: the enclosing traced frames when this one started, as
        ``(frame, iteration)``, outermost first.
    :ivar forced: for ``"if"``: the arm was picked by the host
        (:meth:`Intrinsics.ctl_force`), not by the condition's value.
    """

    __slots__ = ("kind", "node", "where", "var", "leaf", "start", "cond", "value",
                 "iteration", "count", "taken", "reason", "unexplored", "decls", "irregular",
                 "key", "outer", "forced")

    def __init__(self, kind: str, node, where: str = ""):
        self.kind = kind
        self.node = node
        self.where = where
        self.var: Optional[str] = None
        self.leaf = None
        self.start = None
        self.cond = None
        self.value: Any = None
        self.iteration = 0
        self.count = 0                # iterations a loop ran
        self.taken: Optional[bool] = None
        self.reason = ""
        self.unexplored = False
        self.irregular = False
        self.decls: set = set()       # variables declared while this frame is innermost
        self.key = node_key(node)
        self.outer: tuple = ()
        self.forced = False

    def __repr__(self) -> str:
        return f"CtlFrame({self.kind} at {self.where})"


def node_key(node) -> Optional[tuple]:
    """A key for an AST or syntax node that is the same in every run: its
    source buffer and offsets."""
    try:
        r = node.sourceRange
        return (r.start.buffer.id, r.start.offset, r.end.offset)
    except Exception:                     # pragma: no cover
        return None


def const(v) -> Optional[ir.Expr]:
    """A plain value as an expression: an ``ExprConstant`` for an int or a
    string; None (not expressible) for anything else."""
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, (int, str)):
        return ir.ExprConstant(value=v)
    return None


def expr_of(v) -> Optional[ir.Expr]:
    """The expression of a value: a Sym's (None if tainted), or a constant."""
    if isinstance(v, Sym):
        return v.expr
    return const(v)


def leaves(e) -> Iterator[ir.Expr]:
    """The leaves of an expression: every node that is not a constant and
    has no sub-expressions the interpreter builds (refs, unresolved names)."""
    if e is None:
        return
    if isinstance(e, ir.ExprConstant):
        return
    if isinstance(e, ir.ExprBin):
        yield from leaves(e.lhs)
        yield from leaves(e.rhs)
    elif isinstance(e, ir.ExprCompare):
        yield from leaves(e.left)
        for c in e.comparators:
            yield from leaves(c)
    elif isinstance(e, ir.ExprBool):
        for x in e.values:
            yield from leaves(x)
    elif isinstance(e, ir.ExprUnary):
        yield from leaves(e.operand)
    elif isinstance(e, ir.ExprIfExp):
        yield from leaves(e.test)
        yield from leaves(e.body)
        yield from leaves(e.orelse)
    elif isinstance(e, ir.ExprCall):
        for a in e.args:
            yield from leaves(a)
    elif isinstance(e, ir.ExprSubscript):
        yield from leaves(e.value)
        yield from leaves(e.slice)
    elif isinstance(e, ir.ExprAttribute):
        yield from leaves(e.value)
    else:
        yield e


def is_handle(v) -> bool:
    """A class handle (or null), possibly traced: never tainted by a write."""
    v = v.value if isinstance(v, Sym) else v
    return v is None or isinstance(v, Obj)


def taint(v):
    """*v* tainted: scalars become ``Sym(v, None)``; a struct's fields are
    tainted one by one; handles and containers are left alone."""
    if is_handle(v) or isinstance(v, (list, dict)):
        return v
    if isinstance(v, Struct):
        return Struct(v.type, {k: taint(x) for k, x in v.fields.items()})
    return Sym(v, None)


def may_call(node) -> bool:
    """Whether a statement or expression contains a call of a subroutine
    (not a system task or function) or a ``new``: code that may have
    effects a host observes through its intrinsics."""
    if node is None:
        return False
    found = [False]

    def visit(n):
        k = getattr(n, "kind", None)
        if k == EK.Call:
            if not n.isSystemCall:
                found[0] = True
                return ast.VisitAction.Interrupt
        elif k in (EK.NewClass, EK.NewArray, EK.CopyClass):
            found[0] = True
            return ast.VisitAction.Interrupt
        return ast.VisitAction.Advance

    try:
        node.visit(visit)
    except Exception:                     # pragma: no cover - be safe: assume it may
        return True
    return found[0]


def may_effect(node) -> bool:
    """Whether a statement may have effects outside itself that a host
    observes: a call or ``new`` (:func:`may_call`), or an assignment (a
    handle written in one arm of a branch and used after it, say)."""
    if node is None:
        return False
    if may_call(node):
        return True
    found = [False]

    def visit(n):
        if getattr(n, "kind", None) == EK.Assignment:
            found[0] = True
            return ast.VisitAction.Interrupt
        return ast.VisitAction.Advance

    try:
        node.visit(visit)
    except Exception:                     # pragma: no cover
        return True
    return found[0]


def is_pure(node) -> bool:
    """An expression with no calls, ``new``, assignments or increments: safe
    to evaluate even where the program would not."""
    if node is None:
        return True
    bad = [False]

    def visit(n):
        k = getattr(n, "kind", None)
        if k == EK.Call and not n.isSystemCall:
            bad[0] = True
        elif k in (EK.NewClass, EK.NewArray, EK.CopyClass, EK.Assignment):
            bad[0] = True
        elif k == EK.UnaryOp and "crement" in n.op.name:
            bad[0] = True
        if bad[0]:
            return ast.VisitAction.Interrupt
        return ast.VisitAction.Advance

    try:
        node.visit(visit)
    except Exception:                     # pragma: no cover
        return False
    return not bad[0]


_BIN = {"Add": ir.BinOp.Add, "Subtract": ir.BinOp.Sub, "Multiply": ir.BinOp.Mult,
        "Divide": ir.BinOp.Div, "Mod": ir.BinOp.Mod, "BinaryAnd": ir.BinOp.BitAnd,
        "BinaryOr": ir.BinOp.BitOr, "BinaryXor": ir.BinOp.BitXor,
        "LogicalShiftLeft": ir.BinOp.LShift, "ArithmeticShiftLeft": ir.BinOp.LShift,
        "LogicalShiftRight": ir.BinOp.RShift, "ArithmeticShiftRight": ir.BinOp.ARShift}
_CMP = {"Equality": ir.CmpOp.Eq, "CaseEquality": ir.CmpOp.Eq,
        "Inequality": ir.CmpOp.NotEq, "CaseInequality": ir.CmpOp.NotEq,
        "LessThan": ir.CmpOp.Lt, "LessThanEqual": ir.CmpOp.LtE,
        "GreaterThan": ir.CmpOp.Gt, "GreaterThanEqual": ir.CmpOp.GtE}


def binary(op: str, a, b) -> Optional[ir.Expr]:
    """The expression of ``a op b`` (SV operator name), or None."""
    ea, eb = expr_of(a), expr_of(b)
    if ea is None or eb is None:
        return None
    if op in _BIN:
        return ir.ExprBin(lhs=ea, op=_BIN[op], rhs=eb)
    if op in _CMP:
        if is_handle(a) or is_handle(b):
            return None
        return ir.ExprCompare(left=ea, ops=[_CMP[op]], comparators=[eb])
    if op == "LogicalAnd":
        return ir.ExprBool(op=ir.BoolOp.And, values=[ea, eb])
    if op == "LogicalOr":
        return ir.ExprBool(op=ir.BoolOp.Or, values=[ea, eb])
    return None


_UN = {"LogicalNot": ir.UnaryOp.Not, "Minus": ir.UnaryOp.USub, "BitwiseNot": ir.UnaryOp.Invert}


def unary(op: str, a) -> Optional[ir.Expr]:
    ea = expr_of(a)
    if ea is None:
        return None
    if op == "Plus":
        return ea
    if op in _UN:
        return ir.ExprUnary(op=_UN[op], operand=ea)
    return None


def call(name: str, args: List[Any]) -> Optional[ir.Expr]:
    """``name(args)`` (a system function, by its name without ``$``)."""
    es = [expr_of(a) for a in args]
    if any(x is None for x in es):
        return None
    return ir.ExprCall(func=ir.ExprRefUnresolved(name=name), args=es)


# ------------------------------------------------------------- traced types

class _Tainted:
    __slots__ = ()

    def __repr__(self) -> str:
        return "TAINTED"


#: Returned by :meth:`Intrinsics.symbolic_param` for a type parameter the
#: host knows depends on traced values in a way it cannot express.
TAINTED: Any = _Tainted()


class _Const:
    """A value the tracer cannot compute but that depends on no traced value:
    the caller uses the compiler's value instead."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "CONST"


_CONST = _Const()


class TType:
    """A type as written, traced.

    :ivar type: the type's value (a pyslang Type, as the compiler resolved it).
    :ivar ir: a Zuspec IR type over the traced leaves (a ``DataTypeInt``
        whose ``width_expr`` is an expression of them, or a ``DataTypeRef``
        naming a traced type parameter); None when the type does not depend
        on a traced value.
    :ivar args: for a class specialization, its parameters as written:
        ``{name: value}``, a value parameter's value a plain value or a
        :class:`Sym`, a type parameter's a :class:`TType`. Parameters left
        to their defaults are absent.
    :ivar tainted: the type depends on a traced value in a way the tracer
        cannot express.
    """

    __slots__ = ("type", "ir", "args", "tainted", "leaf")

    def __init__(self, type_, ir_=None, args=None, tainted=False, leaf=None):
        self.type = type_
        self.ir = ir_
        self.args = args
        self.tainted = tainted
        self.leaf = leaf              # a traced type parameter: the host's leaf

    @property
    def traced(self) -> bool:
        """Depends on a traced value (expressibly or not)."""
        if self.tainted or self.ir is not None:
            return True
        for v in (self.args or {}).values():
            if isinstance(v, TType) and v.traced:
                return True
            if isinstance(v, Sym):
                return True
        return False

    def __repr__(self) -> str:
        if self.tainted:
            return "TType(tainted)"
        return f"TType({self.type}, ir={self.ir!r}, args={self.args!r})"


_SYN_BIN = {
    "AddExpression": "Add", "SubtractExpression": "Subtract",
    "MultiplyExpression": "Multiply", "DivideExpression": "Divide", "ModExpression": "Mod",
    "LogicalShiftLeftExpression": "LogicalShiftLeft",
    "ArithmeticShiftLeftExpression": "ArithmeticShiftLeft",
    "LogicalShiftRightExpression": "LogicalShiftRight",
    "ArithmeticShiftRightExpression": "ArithmeticShiftRight",
    "BinaryAndExpression": "BinaryAnd", "BinaryOrExpression": "BinaryOr",
    "BinaryXorExpression": "BinaryXor",
    "EqualityExpression": "Equality", "InequalityExpression": "Inequality",
    "CaseEqualityExpression": "CaseEquality", "CaseInequalityExpression": "CaseInequality",
    "LessThanExpression": "LessThan", "LessThanEqualExpression": "LessThanEqual",
    "GreaterThanExpression": "GreaterThan", "GreaterThanEqualExpression": "GreaterThanEqual",
    "LogicalAndExpression": "LogicalAnd", "LogicalOrExpression": "LogicalOr",
}

_PY_BIN = {
    "Add": lambda a, b: a + b, "Subtract": lambda a, b: a - b,
    "Multiply": lambda a, b: a * b,
    "Divide": lambda a, b: int(a / b) if b else 0, "Mod": lambda a, b: a % b if b else 0,
    "LogicalShiftLeft": lambda a, b: a << b, "ArithmeticShiftLeft": lambda a, b: a << b,
    "LogicalShiftRight": lambda a, b: a >> b, "ArithmeticShiftRight": lambda a, b: a >> b,
    "BinaryAnd": lambda a, b: a & b, "BinaryOr": lambda a, b: a | b,
    "BinaryXor": lambda a, b: a ^ b,
    "Equality": lambda a, b: int(a == b), "Inequality": lambda a, b: int(a != b),
    "CaseEquality": lambda a, b: int(a == b), "CaseInequality": lambda a, b: int(a != b),
    "LessThan": lambda a, b: int(a < b), "LessThanEqual": lambda a, b: int(a <= b),
    "GreaterThan": lambda a, b: int(a > b), "GreaterThanEqual": lambda a, b: int(a >= b),
    "LogicalAnd": lambda a, b: int(bool(a) and bool(b)),
    "LogicalOr": lambda a, b: int(bool(a) or bool(b)),
}

_INT_KW = {"IntType": (32, True), "IntegerType": (32, True), "ShortIntType": (16, True),
           "LongIntType": (64, True), "ByteType": (8, True)}
_VEC_KW = ("BitType", "LogicType", "RegType")


def _canon(t):
    return t.canonicalType if t is not None and hasattr(t, "canonicalType") else t


def _type_param_of(sym):
    """The type parameter whose alias is *sym* (name lookup finds a type
    parameter as its alias), else *sym*."""
    if sym is None or sym.kind.name != "TypeAlias":
        return sym
    syn = getattr(sym, "syntax", None)
    if syn is None or syn.kind.name != "TypeAssignment":
        return sym
    try:
        m = sym.parentScope.find(sym.name)
    except Exception:                     # pragma: no cover
        return sym
    if m is not None and type(m).__name__ == "TypeParameterSymbol":
        a = getattr(m, "typeAlias", None)
        if a is None or a is sym or a == sym:
            return m
    return sym


def _int_of(tok) -> Optional[int]:
    v = getattr(tok, "value", None)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(str(v).split("'")[-1].lstrip("sSdDhHbBoO"), 0)
        except Exception:
            return None


class SynTracer:
    """Reads a type or constant expression as written (pyslang syntax), in a
    scope, valuing names as the interpreter does: a class parameter through
    the host's :meth:`Intrinsics.symbolic_param`, a ``localparam`` by its
    initializer, anything else by the compiler's value. Results are plain
    values, :class:`Sym`\\ s over the host's leaves, or :class:`TType`\\ s."""

    def __init__(self, interp):
        self.interp = interp

    # -- values -----------------------------------------------------------
    def value(self, syn, scope):
        """The value of expression syntax *syn*: a plain value, a Sym, or
        (internally) ``_CONST`` when it cannot be computed here but depends
        on no traced value."""
        v, _t = self._value(syn, scope)
        return v

    def _value(self, syn, scope):
        k = syn.kind.name
        if k == "IntegerLiteralExpression":
            v = _int_of(syn.literal)
            return (v if v is not None else _CONST), None
        if k == "IntegerVectorExpression":
            v = _int_of(syn.value)
            return (v if v is not None else _CONST), None
        if k == "ParenthesizedExpression":
            return self._value(syn.expression, scope)
        if k == "IdentifierName":
            return self._name(syn.identifier.valueText, scope, syn)
        if k == "ScopedName":
            sep = str(getattr(syn.separator, "valueText", "") or "")
            if sep == ".":
                base, bt = self._value(syn.left, scope)
                return self._field(base, bt, syn.right, scope, syn)
            return self._name(str(syn).strip(), scope, syn)
        if k in ("UnaryMinusExpression", "UnaryPlusExpression", "UnaryLogicalNotExpression",
                 "UnaryBitwiseNotExpression"):
            a, _ = self._value(syn.operand, scope)
            op = {"UnaryMinusExpression": "Minus", "UnaryPlusExpression": "Plus",
                  "UnaryLogicalNotExpression": "LogicalNot",
                  "UnaryBitwiseNotExpression": "BitwiseNot"}[k]
            if a is _CONST:
                return _CONST, None
            av = a.value if isinstance(a, Sym) else a
            if not isinstance(av, int):
                return self._opaque(syn, scope), None
            r = {"Minus": -av, "Plus": av, "LogicalNot": int(not av), "BitwiseNot": ~av}[op]
            return (Sym(r, unary(op, a)) if isinstance(a, Sym) else r), None
        if k in _SYN_BIN:
            op = _SYN_BIN[k]
            a, _ = self._value(syn.left, scope)
            b, _ = self._value(syn.right, scope)
            if a is _CONST or b is _CONST:
                if isinstance(a, Sym) or isinstance(b, Sym):
                    return Sym(0, None), None
                return _CONST, None
            av = a.value if isinstance(a, Sym) else a
            bv = b.value if isinstance(b, Sym) else b
            if not (isinstance(av, int) and isinstance(bv, int)):
                return self._opaque(syn, scope), None
            r = _PY_BIN[op](av, bv)
            if isinstance(a, Sym) or isinstance(b, Sym):
                return Sym(r, binary(op, a, b)), None
            return r, None
        if k == "ConditionalExpression":
            preds = list(syn.predicate.conditions)
            if len(preds) != 1:
                return self._opaque(syn, scope), None
            c, _ = self._value(preds[0].expr, scope)
            a, at = self._value(syn.left, scope)
            b, bt = self._value(syn.right, scope)
            if _CONST in (c, a, b):
                if any(isinstance(x, Sym) for x in (c, a, b)):
                    return Sym(0, None), None
                return _CONST, None
            cv = c.value if isinstance(c, Sym) else c
            r = (a if cv else b)
            rv = r.value if isinstance(r, Sym) else r
            if any(isinstance(x, Sym) for x in (c, a, b)):
                ec, ea, eb = expr_of(c), expr_of(a), expr_of(b)
                if ec is None or ea is None or eb is None:
                    return Sym(rv, None), None
                return Sym(rv, ir.ExprIfExp(test=ec, body=ea, orelse=eb)), None
            return rv, (at if cv else bt)
        if k == "InvocationExpression":
            return self._invocation(syn, scope), None
        return self._opaque(syn, scope), None

    def _invocation(self, syn, scope):
        left = syn.left
        name = str(left).strip()
        args = []
        if syn.arguments is not None:
            for a in syn.arguments.parameters:
                args.append(getattr(a, "expr", a))
        if name == "$clog2" and len(args) == 1:
            v = self.value(args[0], scope)
            if v is _CONST:
                return _CONST
            vv = v.value if isinstance(v, Sym) else v
            if not isinstance(vv, int):
                return self._opaque(syn, scope)
            r = max(0, (vv - 1).bit_length())
            return Sym(r, call("clog2", [v])) if isinstance(v, Sym) else r
        if name == "$bits" and len(args) == 1:
            t = self.type(args[0], scope)
            if t is None:
                return self._opaque(syn, scope)
            if t.tainted:
                return Sym(0, None)
            w = getattr(_canon(t.type), "bitWidth", None)
            if t.ir is None:
                return int(w) if w is not None else _CONST
            if isinstance(t.ir, ir.DataTypeRef):
                leaf = t.leaf if t.leaf is not None else ir.ExprRefParam(name=t.ir.ref_name)
                return Sym(int(w or 0), ir.ExprCall(func=ir.ExprRefUnresolved(name="bits"),
                                                    args=[leaf]))
            we = getattr(t.ir, "width_expr", None)
            return Sym(int(w or 0), we)
        return self._opaque(syn, scope)

    def _name(self, name: str, scope, syn):
        try:
            sym = scope.lookupName(name)
        except Exception:
            sym = None
        if sym is None:
            return self._opaque(syn, scope), None
        kn = sym.kind.name
        if kn == "Parameter":
            return self.interp.param_value(sym), _canon(sym.type)
        if kn == "EnumValue":
            from .values import const_to_py
            return const_to_py(sym.value, sym.type), _canon(sym.type)
        return _CONST, None

    def _field(self, base, bt, name_syn, scope, syn):
        name = str(name_syn).strip()
        if base is _CONST:
            return _CONST, None
        bv = base.value if isinstance(base, Sym) else base
        t = _canon(bt)
        fv, ft = None, None
        from .values import Struct as _S, norm
        if isinstance(bv, _S):
            if name not in bv.fields:
                return self._opaque(syn, scope), None
            fv = bv[name]
            fv = fv.value if isinstance(fv, Sym) else fv
            if t is not None:
                for m in t:
                    if m.name == name:
                        ft = _canon(m.type)
        elif isinstance(bv, int) and t is not None and getattr(t, "isIntegral", False):
            for m in t:
                if m.name == name and hasattr(m, "bitOffset"):
                    fv = norm(bv >> int(m.bitOffset), m.type)
                    ft = _canon(m.type)
            if fv is None:
                return self._opaque(syn, scope), None
        else:
            return self._opaque(syn, scope), None
        if isinstance(base, Sym):
            return Sym(fv, None if base.expr is None else
                       ir.ExprAttribute(value=base.expr, attr=name)), ft
        return fv, ft

    def _opaque(self, syn, scope):
        """A construct the tracer does not read: tainted if it names a traced
        value, else ``_CONST``."""
        return Sym(0, None) if self._names_traced(syn, scope) else _CONST

    def _names_traced(self, syn, scope) -> bool:
        found = [False]

        def visit(n):
            if getattr(n, "kind", None) is not None and n.kind.name == "IdentifierName":
                try:
                    sym = _type_param_of(scope.lookupName(n.identifier.valueText))
                except Exception:
                    sym = None
                if sym is not None and sym.kind.name in ("Parameter", "TypeParameter"):
                    if sym.kind.name == "Parameter":
                        if isinstance(self.interp.param_value(sym), Sym):
                            found[0] = True
                    elif self.interp.intrinsics.symbolic_param(self.interp, sym) is not None:
                        found[0] = True
            return True

        try:
            syn.visit(visit)
        except Exception:                 # pragma: no cover
            return True
        return found[0]

    # -- types ------------------------------------------------------------
    def type(self, syn, scope, concrete=None) -> Optional[TType]:
        """The type written as *syn* in *scope*; ``concrete`` is the type the
        compiler resolved it to, where the caller knows it (a class
        argument). None when *syn* is not a type the tracer reads."""
        k = syn.kind.name
        if k in _VEC_KW:
            return self._vector(syn, scope, concrete)
        if k in _INT_KW:
            return TType(concrete)
        if k == "NamedType":
            return self.type(syn.name, scope, concrete)
        if k in ("IdentifierName", "ScopedName", "ClassName"):
            return self._named_type(syn, scope, concrete)
        if k == "ParenthesizedExpression":
            return self.type(syn.expression, scope, concrete)
        return TType(concrete, tainted=self._names_traced(syn, scope))

    def _vector(self, syn, scope, concrete) -> TType:
        signed = str(getattr(syn.signing, "valueText", "") or "") == "signed"
        dims = list(syn.dimensions or [])
        widths = []
        for d in dims:
            spec = d.specifier
            sel = getattr(spec, "selector", None) if spec is not None else None
            if sel is None or sel.kind.name != "SimpleRangeSelect":
                return TType(concrete, tainted=self._names_traced(syn, scope))
            hi, lo = self.value(sel.left, scope), self.value(sel.right, scope)
            if hi is _CONST or lo is _CONST:
                if isinstance(hi, Sym) or isinstance(lo, Sym):
                    return TType(concrete, tainted=True)
                widths.append(None)
                continue
            if isinstance(hi, Sym) and hi.expr is None or isinstance(lo, Sym) and lo.expr is None:
                return TType(concrete, tainted=True)
            widths.append((hi, lo))
        if not any(isinstance(x, Sym) for w in widths if w for x in w):
            return TType(concrete)
        if any(w is None for w in widths):
            return TType(concrete, tainted=True)
        we = None
        bits = 1
        for hi, lo in widths:
            hv = hi.value if isinstance(hi, Sym) else hi
            lv = lo.value if isinstance(lo, Sym) else lo
            bits *= abs(hv - lv) + 1
            e = _width(hi, lo)
            we = e if we is None else ir.ExprBin(lhs=we, op=ir.BinOp.Mult, rhs=e)
        c = _canon(concrete)
        if c is not None and getattr(c, "bitWidth", None) is not None:
            bits = int(c.bitWidth)
        return TType(concrete, ir.DataTypeInt(bits=bits, signed=signed, width_expr=we,
                                              four_state=syn.kind.name != "BitType"))

    def _named_type(self, syn, scope, concrete) -> TType:
        k = syn.kind.name
        if k == "ClassName":
            name = syn.identifier.valueText
        elif k == "ScopedName":
            sep = str(getattr(syn.separator, "valueText", "") or "")
            if sep != "::":
                return TType(concrete, tainted=self._names_traced(syn, scope))
            name = str(syn).strip()
            if syn.right.kind.name == "ClassName":
                name = str(syn.left).strip() + "::" + syn.right.identifier.valueText
        else:
            name = syn.identifier.valueText
        try:
            sym = _type_param_of(scope.lookupName(name))
        except Exception:
            sym = None
        if sym is None:
            return TType(concrete, tainted=self._names_traced(syn, scope))
        kn = sym.kind.name
        if kn == "TypeParameter":
            leaf = self.interp.intrinsics.symbolic_param(self.interp, sym)
            c = concrete if concrete is not None else sym.targetType.type
            if leaf is TAINTED:
                return TType(c, tainted=True)
            if leaf is not None:
                return TType(c, ir.DataTypeRef(ref_name=getattr(leaf, "name", sym.name)),
                             leaf=leaf)
            return TType(c)
        if kn == "TypeAlias":
            c = concrete if concrete is not None else sym.targetType.type
            tsyn = getattr(sym.targetType, "typeSyntax", None)
            if tsyn is None:
                return TType(c)
            inner = self.type(tsyn, sym.parentScope, c)
            if inner is None:
                return TType(c)
            return inner
        if kn in ("GenericClassDef", "ClassType"):
            cls = _canon(concrete)
            if cls is None and kn == "ClassType":
                cls = sym
            if cls is None or not getattr(cls, "isClass", False):
                return TType(concrete, tainted=self._names_traced(syn, scope))
            cname = syn.right if k == "ScopedName" else syn
            args = self.class_args(cname, scope, cls)
            return TType(cls, args=args)
        return TType(concrete, tainted=self._names_traced(syn, scope))

    def class_args(self, cname, scope, cls) -> Dict[str, Any]:
        """The parameters of class specialization *cls* as written in class
        name syntax *cname* (``C #(a, .B(b))``), in *scope*: ``{name:
        value}`` as :attr:`TType.args`. A value that cannot be computed but
        depends on no traced value is left out (the specialization's value
        holds)."""
        params = [m for m in _canon(cls)
                  if type(m).__name__ in ("ParameterSymbol", "TypeParameterSymbol")
                  and getattr(m, "isPortParam", False)]
        out: Dict[str, Any] = {}
        pa = getattr(cname, "parameters", None) if cname.kind.name == "ClassName" else None
        if pa is None:
            return out
        for i, a in enumerate(pa.parameters):
            if a.kind.name == "NamedParamAssignment":
                pname = a.name.valueText
                m = next((x for x in params if x.name == pname), None)
            else:
                m = params[i] if i < len(params) else None
            if m is None or a.expr is None:
                continue
            if type(m).__name__ == "TypeParameterSymbol":
                t = self.type(a.expr, scope, m.targetType.type)
                if t is None:
                    t = TType(m.targetType.type, tainted=self._names_traced(a.expr, scope))
                out[m.name] = t
            else:
                v = self.value(a.expr, scope)
                if v is not _CONST:
                    out[m.name] = v
        return out

    def call_class(self, call_expr, scope, cls) -> Optional[TType]:
        """For a call ``C #(args)::f(...)`` (an AST call expression) made in
        *scope*, class *cls* (``C``'s specialization) as written: a
        :class:`TType` whose ``args`` are its parameters; None when the call
        does not name its class that way."""
        syn = getattr(call_expr, "syntax", None)
        left = getattr(syn, "left", None) if syn is not None else None
        if left is None or left.kind.name != "ScopedName":
            return None
        t = self.type(left.left, scope, cls)
        if t is not None and t.args is None and not t.tainted:
            t.args = {}
        return t


def _width(hi, lo) -> ir.Expr:
    """``hi - lo + 1`` as an expression, simplified for ``[X-1:0]``."""
    eh, el = expr_of(hi), expr_of(lo)
    lv = lo.value if isinstance(lo, Sym) else lo
    if not isinstance(lo, Sym) and lv == 0:
        if isinstance(eh, ir.ExprBin) and eh.op == ir.BinOp.Sub and \
                isinstance(eh.rhs, ir.ExprConstant) and eh.rhs.value == 1:
            return eh.lhs
        return ir.ExprBin(lhs=eh, op=ir.BinOp.Add, rhs=ir.ExprConstant(value=1))
    return ir.ExprBin(lhs=ir.ExprBin(lhs=eh, op=ir.BinOp.Sub, rhs=el), op=ir.BinOp.Add,
                      rhs=ir.ExprConstant(value=1))
