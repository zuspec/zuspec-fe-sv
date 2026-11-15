"""Read a Verilog module's header: its parameters and ports, with widths
resolved for given parameter values.

Ported from fw-hdl's ``python/fw/hdl/rtl_header.py``. A front end uses it to
check how a class-level description maps onto an existing RTL module (fw-hdl
extern pin maps).

The module is elaborated by pyslang as the only top, with the values applied
as parameter overrides, so a width written in terms of parameters
(``[W*2-1:0]``) comes back as a number. ANSI and non-ANSI headers both work.
"""
from __future__ import annotations

import dataclasses as dc
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

Value = Union[int, str]


class HeaderError(Exception):
    pass


@dc.dataclass(frozen=True)
class HeaderPort:
    name: str
    direction: str              # "in" | "out" | "inout"
    width: int                  # bits; 0 for an interface port
    sv_type: str                # as slang prints it, e.g. "logic[36:0]"
    signed: bool = False
    interface: bool = False
    four_state: bool = True
    #: The width as written, a Zuspec IR expression whose leaves are the
    #: module's parameters (``ExprRefParam``; localparams too): ``W-1+1`` for
    #: ``[W-1:0]``, an ``ExprConstant`` for a width that names none. None
    #: when the declaration is not one :func:`width_expr` reads (a type
    #: parameter, a typedef declared in the module, an unsupported operator).
    width_expr: Any = dc.field(default=None, compare=False)
    #: The module type parameter that types the port (``input T d``), or "".
    type_param: str = ""


@dc.dataclass(frozen=True)
class HeaderParam:
    """An overridable parameter: ``kind`` is "value" or "type". For a value,
    ``type`` is its declared type as slang prints it, ``qual`` the
    package-qualified name of an enum or struct type ("" otherwise),
    ``is_string`` and ``is_enum`` classify it."""
    name: str
    kind: str
    type: str = ""
    qual: str = ""
    is_string: bool = False
    is_enum: bool = False


@dc.dataclass(frozen=True)
class Header:
    module: str
    params: Dict[str, Value]            # every overridable value parameter
    localparams: Dict[str, Value]
    type_params: List[str]
    ports: List[HeaderPort]
    param_info: Dict[str, HeaderParam] = dc.field(default_factory=dict)
    #: Each value parameter's and localparam's initializer as an IR
    #: expression over the module's parameters (as
    #: :attr:`HeaderPort.width_expr`), or None when it cannot be read.
    param_exprs: Dict[str, Any] = dc.field(default_factory=dict, compare=False)

    def port(self, name: str) -> Optional[HeaderPort]:
        return next((p for p in self.ports if p.name == name), None)


_DIR = {"In": "in", "Out": "out", "InOut": "inout", "Ref": "ref"}


def _value(cv) -> Value:
    s = str(cv)
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    # Sized literals print as e.g. 8'd5 / 4'b1010.
    if "'" in s:
        size, _, rest = s.partition("'")
        rest = rest.lstrip("sS")
        base = {"d": 10, "h": 16, "b": 2, "o": 8}.get(rest[:1].lower())
        if base is not None:
            try:
                return int(rest[1:].replace("_", ""), base)
            except ValueError:
                pass
    return s


def _literal(v: Value) -> str:
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def _qual(t) -> str:
    """The package-qualified name of an enum or struct typedef, else ""."""
    a = t
    while getattr(a, "isAlias", False):
        lp = str(getattr(a, "lexicalPath", "") or "")
        if lp:
            return lp
        a = a.targetType.type
    return ""


def read_header(files: Iterable[str], module: str,
                params: Optional[Mapping[str, Value]] = None,
                defines: Optional[Mapping[str, str]] = None,
                include_dirs: Iterable[str] = (),
                overrides: Optional[Mapping[str, str]] = None) -> Header:
    """The header of *module* in *files*, elaborated with *params* (int and
    string values) or *overrides* (any parameter, value or type, as SV
    source text: ``{"MODE": "pkg::FAST", "data_t": "logic [7:0]"}``; the
    module is then elaborated as an instance in a wrapper).

    Raises :class:`HeaderError` if the module is not found, a value names no
    parameter of the module (or a localparam), or elaboration reports errors.
    """
    import pyslang
    from pyslang import ast, syntax

    params = dict(params or {})
    overrides = dict(overrides or {})
    wrapper = "__zsp_hdr_top"
    co = ast.CompilationOptions()
    if overrides:
        co.topModules = {wrapper}
    else:
        co.topModules = {module}
        co.paramOverrides = [f"{k}={_literal(v)}" for k, v in params.items()]
    opts = [co]
    if defines or include_dirs:
        po = pyslang.parsing.PreprocessorOptions()
        if defines:
            po.predefines = [f"{k}={v}" for k, v in defines.items()]
        if include_dirs:
            po.additionalIncludePaths = [str(d) for d in include_dirs]
        opts.append(po)
    bag = pyslang.Bag(opts)
    sm = pyslang.SourceManager()
    comp = ast.Compilation(bag)
    trees = []
    for f in files:
        trees.append(syntax.SyntaxTree.fromFile(str(f), sm, bag))
        comp.addSyntaxTree(trees[-1])
    if overrides:
        ov = ", ".join(f".{k}({v})" for k, v in overrides.items())
        trees.append(syntax.SyntaxTree.fromText(
            f"module {wrapper}; {module} #({ov}) u(); endmodule\n", sm, "__zsp_hdr.sv", "", bag))
        comp.addSyntaxTree(trees[-1])
    tops = [i for i in comp.getRoot().topInstances if i.name == (wrapper if overrides else module)]
    if not tops:
        raise HeaderError(f"module {module!r} not found in {[str(f) for f in files]}")
    errors = [d for d in comp.getAllDiagnostics() if d.isError()]
    if errors:
        text = pyslang.DiagnosticEngine.reportAll(sm, errors)
        raise HeaderError(f"module {module!r}: elaboration errors:\n{text}")
    body = tops[0].body
    if overrides:
        inst = body.find("u")
        if inst is None:
            raise HeaderError(f"module {module!r} not found in {[str(f) for f in files]}")
        body = inst.body

    values: Dict[str, Value] = {}
    local: Dict[str, Value] = {}
    types: List[str] = []
    info: Dict[str, HeaderParam] = {}
    pexprs: Dict[str, Any] = {}
    for p in body.parameters:
        if getattr(p, "isType", False) or type(p).__name__ == "TypeParameterSymbol":
            if not getattr(p, "isLocalParam", False):
                types.append(p.name)
                info[p.name] = HeaderParam(p.name, "type")
            continue
        init = getattr(p.syntax, "initializer", None) if p.syntax is not None else None
        pexprs[p.name] = _xlate(init.expr, body) if init is not None else None
        if p.isLocalParam:
            local[p.name] = _value(p.value)
        else:
            values[p.name] = _value(p.value)
            t = p.type
            c = t.canonicalType
            info[p.name] = HeaderParam(p.name, "value", str(c), _qual(t),
                                       bool(c.isString), bool(c.isEnum))
    unknown = sorted(set(params) - set(values) - set(types)) if not overrides else []
    if unknown:
        bad = [n for n in unknown if n in local]
        why = (f"{', '.join(bad)} {'is a localparam' if len(bad) == 1 else 'are localparams'}"
               if bad else f"module parameters are {sorted(values) or 'none'}")
        raise HeaderError(f"module {module!r}: no parameter {', '.join(unknown)} ({why})")

    ports: List[HeaderPort] = []
    for p in body.portList:
        kind = type(p).__name__
        if kind == "InterfacePortSymbol":
            ports.append(HeaderPort(p.name, "inout", 0, "interface", interface=True))
            continue
        if kind != "PortSymbol":
            raise HeaderError(f"module {module!r}: port {p.name!r} of kind {kind} "
                              f"is not supported")
        t = p.type
        tsyn = _port_type_syntax(p)
        tparam = ""
        if tsyn is not None and tsyn.kind.name == "NamedType":
            m = _lookup(body, str(tsyn.name).strip())
            if m is not None and type(m).__name__ == "TypeParameterSymbol":
                tparam = m.name
        ports.append(HeaderPort(p.name, _DIR.get(str(p.direction).split(".")[-1], "inout"),
                                int(t.bitWidth), str(t), bool(t.isSigned),
                                four_state=bool(t.isFourState),
                                width_expr=None if tparam else width_expr(tsyn, t, body),
                                type_param=tparam))
    return Header(module, values, local, types, ports, info, pexprs)


# ------------------------------------------------- widths as IR expressions

def _port_type_syntax(p):
    s = getattr(p, "internalSymbol", None)
    dt = getattr(s, "declaredType", None) if s is not None else None
    return getattr(dt, "typeSyntax", None) if dt is not None else None


def _lookup(scope, name: str):
    """``name`` looked up in ``scope``; a type parameter (which lookup finds
    as its alias) as its ``TypeParameterSymbol``."""
    try:
        m = scope.lookupName(name)
    except Exception:
        return None
    if m is not None and m.kind.name == "TypeAlias" \
            and getattr(getattr(m, "syntax", None), "kind", None) is not None \
            and m.syntax.kind.name == "TypeAssignment":
        p = m.parentScope.find(m.name) if m.parentScope is not None else None
        if p is not None and type(p).__name__ == "TypeParameterSymbol":
            return p
    return m


def width_expr(tsyn, t, scope):
    """The width of a port declared with type syntax ``tsyn`` (resolved to
    ``t``) in module body ``scope``, as an IR expression over the module's
    parameters, or None (see :attr:`HeaderPort.width_expr`)."""
    import zuspec.ir.core as ir
    const = ir.ExprConstant(value=int(t.bitWidth))
    if tsyn is None:
        return const if not getattr(t, "isAlias", False) else None
    k = tsyn.kind.name
    if k in ("LogicType", "BitType", "RegType", "ImplicitType"):
        w = None
        for d in tsyn.dimensions or []:
            sel = getattr(d.specifier, "selector", None) if d.specifier is not None else None
            if sel is None or sel.kind.name != "SimpleRangeSelect":
                return None
            hi, lo = _xlate(sel.left, scope), _xlate(sel.right, scope)
            if hi is None or lo is None:
                return None
            n = _sub1(hi, lo)
            if n is None:
                return None
            w = n if w is None else ir.ExprBin(lhs=w, op=ir.BinOp.Mult, rhs=n)
        return w if w is not None else ir.ExprConstant(value=1)
    if k == "NamedType":
        # A typedef: its width is fixed unless the typedef is the module's own.
        m = _lookup(scope, str(tsyn.name).strip())
        if m is None or type(m).__name__ != "TypeAliasType":
            return None
        ps = getattr(m, "parentScope", None)
        if ps is not None and getattr(ps, "kind", None) is not None \
                and ps.kind.name in ("InstanceBody", "GenerateBlock"):
            return None
        return const
    if k in ("IntType", "IntegerType", "ShortIntType", "LongIntType", "ByteType"):
        return const
    return None


def _sub1(hi, lo):
    """``hi - lo + 1``, simplified for ``[X-1:0]`` and ``[X:0]``."""
    import zuspec.ir.core as ir
    if isinstance(lo, ir.ExprConstant) and lo.value == 0:
        if isinstance(hi, ir.ExprConstant):
            return ir.ExprConstant(value=hi.value + 1)
        if isinstance(hi, ir.ExprBin) and hi.op == ir.BinOp.Sub \
                and isinstance(hi.rhs, ir.ExprConstant) and hi.rhs.value == 1:
            return hi.lhs
        return ir.ExprBin(lhs=hi, op=ir.BinOp.Add, rhs=ir.ExprConstant(value=1))
    if isinstance(hi, ir.ExprConstant) and isinstance(lo, ir.ExprConstant):
        return ir.ExprConstant(value=abs(hi.value - lo.value) + 1)
    return None                       # a descending or offset range over parameters


_XBIN = {"AddExpression": "Add", "SubtractExpression": "Sub", "MultiplyExpression": "Mult",
         "DivideExpression": "Div", "ModExpression": "Mod", "LogicalShiftLeftExpression": "LShift",
         "LogicalShiftRightExpression": "RShift", "ArithmeticShiftLeftExpression": "LShift",
         "PowerExpression": "Exp"}


def _xlate(syn, scope):
    """Constant expression syntax as an IR expression over the parameters
    of ``scope`` (a module body), or None. Reads integer literals, parameter
    names, ``+ - * / % << >> **``, unary minus, parentheses and
    ``$clog2``."""
    import zuspec.ir.core as ir
    if syn is None:
        return None
    k = syn.kind.name
    if k in ("SimplePropertyExpr", "SimpleSequenceExpr"):     # a call argument
        return _xlate(syn.expr, scope)
    if k == "ParenthesizedExpression":
        return _xlate(syn.expression, scope)
    if k in ("IntegerLiteralExpression", "IntegerVectorExpression"):
        v = _value(str(syn).strip())
        return ir.ExprConstant(value=v) if isinstance(v, int) else None
    if k == "IdentifierName":
        m = _lookup(scope, syn.identifier.valueText)
        if m is not None and type(m).__name__ == "ParameterSymbol":
            return ir.ExprRefParam(name=m.name)
        return None
    if k in _XBIN:
        a, b = _xlate(syn.left, scope), _xlate(syn.right, scope)
        if a is None or b is None:
            return None
        return ir.ExprBin(lhs=a, op=getattr(ir.BinOp, _XBIN[k]), rhs=b)
    if k == "UnaryMinusExpression":
        a = _xlate(syn.operand, scope)
        return None if a is None else ir.ExprUnary(op=ir.UnaryOp.USub, operand=a)
    if k == "InvocationExpression":
        name = str(syn.left).strip()
        args = getattr(syn.arguments, "parameters", None) if syn.arguments is not None else None
        if name == "$clog2" and args is not None and len(args) == 1 \
                and args[0].kind.name == "OrderedArgument":
            a = _xlate(getattr(args[0], "expr", None), scope)
            if a is not None:
                return ir.ExprCall(func=ir.ExprRefUnresolved(name="clog2"), args=[a])
        return None
    return None


# ----------------------------------------------------- package type lookup

def type_signature(t) -> str:
    """A structural signature of an integral type (a pyslang Type): two
    declarations of the same packed struct or enum, compiled separately,
    have the same signature."""
    c = t.canonicalType if hasattr(t, "canonicalType") else t
    if getattr(c, "isEnum", False):
        items = []
        for m in c:
            if type(m).__name__ == "EnumValueSymbol":
                items.append(f"{m.name}={m.value}")
        return f"enum {type_signature(c.baseType)} {{{','.join(items)}}}"
    if getattr(c, "isStruct", False) or getattr(c, "isPackedUnion", False):
        kind = "union" if getattr(c, "isPackedUnion", False) else "struct"
        mems = [f"{type_signature(m.type)} {m.name}" for m in c
                if type(m).__name__ == "FieldSymbol"]
        sgn = " signed" if c.isSigned else ""
        return f"{kind}{sgn} {{{';'.join(mems)}}}"
    w = int(getattr(c, "bitWidth", 0) or 0)
    return f"{'logic' if c.isFourState else 'bit'}{' signed' if c.isSigned else ''}[{w}]"


def package_types(files: Iterable[str], names: Optional[Iterable[str]] = None,
                  defines: Optional[Mapping[str, str]] = None,
                  include_dirs: Iterable[str] = ()) -> Dict[str, str]:
    """Which of ``names`` (package-qualified typedef names,
    ``prm_pkg::cfg_t``) *files* declare: ``{name: type_signature}`` for each
    one found. ``names`` None: every typedef of every package."""
    import pyslang
    from pyslang import ast, syntax

    if names is not None:
        names = [n for n in names if "::" in n]
        if not names:
            return {}
    opts = []
    if defines or include_dirs:
        po = pyslang.parsing.PreprocessorOptions()
        if defines:
            po.predefines = [f"{k}={v}" for k, v in defines.items()]
        if include_dirs:
            po.additionalIncludePaths = [str(d) for d in include_dirs]
        opts.append(po)
    bag = pyslang.Bag(opts)
    sm = pyslang.SourceManager()
    comp = ast.Compilation(bag)
    trees = []
    for f in files:
        trees.append(syntax.SyntaxTree.fromFile(str(f), sm, bag))
        comp.addSyntaxTree(trees[-1])
    out: Dict[str, str] = {}
    if names is None:
        for pk in comp.getPackages():
            for m in pk:
                if type(m).__name__ == "TypeAliasType":
                    out[f"{pk.name}::{m.name}"] = type_signature(m)
        return out
    for n in names:
        pkg, _, tname = n.rpartition("::")
        if "::" in pkg:
            continue                  # a class-scoped typedef: never the RTL's
        pk = comp.getPackage(pkg)
        m = pk.find(tname) if pk is not None else None
        if m is not None and type(m).__name__ == "TypeAliasType":
            out[n] = type_signature(m)
    return out

