"""Runtime values of the SV class-subset interpreter.

Ported from fw-hdl ``python/fw/hdl/fe/elab/values.py``, with integral values
changed to carry their sign (see the package docstring for the
representation).
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from pyslang import ast

__all__ = ["Obj", "Struct", "Sym", "SymList", "unwrap", "strip", "sym_expr", "mask",
           "to_signed", "norm", "width_of", "const_to_py", "default_value", "copy_value",
           "truthy", "fmt_sv", "int_to_str", "str_to_int"]


class Sym:
    """A *traced* value: a concrete value and the expression that computed it.

    The host makes the leaves: it puts ``Sym(v, leaf)`` where it wants a value
    followed (a component's parameters, say), with ``leaf`` any Zuspec IR
    ``Expr`` it can recognize later (an ``ExprRefParam``). The interpreter
    computes with ``value`` as it always does, and builds ``expr`` alongside
    from Zuspec IR expressions (``ExprBin``, ``ExprCompare``, ``ExprBool``,
    ``ExprUnary``, ``ExprIfExp``, ``ExprCall``, ``ExprSubscript``,
    ``ExprAttribute``) over the leaves and ``ExprConstant``\ s.

    ``expr`` None means *tainted*: the value depends on a traced value in a
    way the interpreter does not express (an unsupported operator, a write
    under control flow that depends on a traced value). A host must treat a
    tainted value as unknown.

    A ``Sym`` may also wrap a class handle selected through a traced index
    (``u[i]`` -> ``Sym(obj, ExprSubscript(u, i))``); member access keeps the
    path (``ExprAttribute``). Values are immutable.
    """

    __slots__ = ("value", "expr")

    def __init__(self, value, expr):
        if isinstance(value, Sym):
            value = value.value
        self.value = value
        self.expr = expr

    def __repr__(self) -> str:
        return f"Sym({self.value!r}, {'tainted' if self.expr is None else 'traced'})"


class SymList(list):
    """A dynamic array whose size is traced (``new[params.n]``): ``size`` is
    a :class:`Sym`. A ``foreach`` over it is a traced loop."""

    __slots__ = ("size",)

    def __init__(self, items=(), size: Optional["Sym"] = None):
        super().__init__(items)
        self.size = size


def unwrap(v):
    """The concrete value of *v* (a :class:`Sym`'s ``value``; else *v*)."""
    return v.value if isinstance(v, Sym) else v


def sym_expr(v):
    """The expression of a :class:`Sym` (None if tainted); a plain value has
    none either: test ``isinstance(v, Sym)`` first."""
    return v.expr if isinstance(v, Sym) else None


def strip(v):
    """*v* with every :class:`Sym` replaced by its value, deeply (structs,
    lists and dicts are copied)."""
    if isinstance(v, Sym):
        return strip(v.value)
    if isinstance(v, list):
        return [strip(x) for x in v]
    if isinstance(v, Struct):
        return Struct(v.type, {k: strip(x) for k, x in v.fields.items()})
    if isinstance(v, dict):
        return {k: strip(x) for k, x in v.items()}
    return v


class Obj:
    """A class object on the interpreter heap.

    ``cls`` is the ClassType (a specialization) the object was created as.
    Field values are keyed by their ClassProperty symbol in ``fields``; use
    :meth:`get` / :meth:`set` to access them by name (the most-derived
    declaration of a name wins). ``host`` is free for the host's own data.
    """

    __slots__ = ("cls", "fields", "oid", "host", "_names")

    def __init__(self, cls, oid: int):
        self.cls = cls
        self.fields: Dict[Any, Any] = {}
        self.oid = oid
        self.host: Any = None
        self._names: Optional[Dict[str, Any]] = None

    # -- by-name access ------------------------------------------------------
    def _name_map(self) -> Dict[str, Any]:
        if self._names is None:
            names: Dict[str, Any] = {}
            for sym in self.fields:          # insertion order: most derived first
                names.setdefault(sym.name, sym)
            self._names = names
        return self._names

    def field_symbol(self, name: str):
        """The ClassProperty symbol of field *name* (KeyError if none)."""
        return self._name_map()[name]

    def has(self, name: str) -> bool:
        return name in self._name_map()

    def get(self, name: str, default: Any = KeyError) -> Any:
        try:
            return self.fields[self._name_map()[name]]
        except KeyError:
            if default is KeyError:
                raise KeyError(f"{self!r} has no field {name!r}") from None
            return default

    def set(self, name: str, value: Any) -> None:
        self.fields[self.field_symbol(name)] = value

    def field_items(self) -> Iterator[Tuple[str, Any]]:
        """(name, value) for each field, most-derived class first."""
        for sym, v in self.fields.items():
            yield sym.name, v

    def _lookup(self, sym):
        """The key for *sym* in ``fields``: itself, or the field of the same
        name declared in a class of the same name (another wrapper or
        specialization object for the same declaration)."""
        if sym in self.fields:
            return sym
        owner = getattr(sym.parentScope, "name", None)
        for s in self.fields:
            if s.name == sym.name and getattr(s.parentScope, "name", None) == owner:
                return s
        return self._name_map().get(sym.name)

    @property
    def type_name(self) -> str:
        return str(self.cls)

    def __repr__(self) -> str:
        return f"<{self.cls}#{self.oid}>"


class Struct:
    """An unpacked struct value: named fields, in declaration order.

    Structs have value semantics: the interpreter copies them on assignment,
    on argument passing (except ``ref``) and when stored into containers.
    """

    __slots__ = ("type", "fields")

    def __init__(self, type_, fields: Dict[str, Any]):
        self.type = type_                 # the struct Type (may be None)
        self.fields = fields

    def __getitem__(self, name: str) -> Any:
        return self.fields[name]

    def __setitem__(self, name: str, value: Any) -> None:
        self.fields[name] = value

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "fields")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def keys(self):
        return self.fields.keys()

    def items(self):
        return self.fields.items()

    def copy(self) -> "Struct":
        return Struct(self.type, {k: copy_value(v) for k, v in self.fields.items()})

    def __eq__(self, other) -> bool:
        return isinstance(other, Struct) and self.fields == other.fields

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}: {v!r}" for k, v in self.fields.items())
        return "'{" + inner + "}"


# ------------------------------------------------------------------ integers

def mask(v: int, w: int) -> int:
    return v & ((1 << w) - 1) if w > 0 else v


def to_signed(v: int, w: int) -> int:
    v = mask(v, w)
    return v - (1 << w) if w > 0 and (v >> (w - 1)) & 1 else v


def width_of(t) -> Optional[int]:
    """Bit width of an integral type, else None."""
    t = t.canonicalType
    if t.isIntegral:
        return int(t.bitWidth)
    return None


def str_to_int(s: str) -> int:
    n = 0
    for ch in s:
        n = (n << 8) | (ord(ch) & 0xFF)
    return n


def int_to_str(v: int) -> str:
    if v < 0:
        v = mask(v, max(8, (v.bit_length() + 8) // 8 * 8))
    bs = []
    while v:
        b = v & 0xFF
        if b:
            bs.append(chr(b))
        v >>= 8
    return "".join(reversed(bs))


def norm(v, t):
    """Convert *v* to a value of type *t* (truncate/extend and sign an
    integral, int<->str, int<->real). Other values pass through. A
    :class:`Sym` keeps its expression (truncation is not traced)."""
    if t is None:
        return v
    if isinstance(v, Sym):
        return Sym(norm(v.value, t), v.expr)
    t = t.canonicalType
    if t.isIntegral:
        if isinstance(v, bool):
            v = int(v)
        elif isinstance(v, float):
            v = int(round(v))
        elif isinstance(v, str):
            v = str_to_int(v)
        elif not isinstance(v, int):
            return v
        w = int(t.bitWidth)
        return to_signed(v, w) if t.isSigned else mask(v, w)
    if t.isFloating:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return v
    if t.isString:
        if isinstance(v, int):
            return int_to_str(v)
        return v
    return v


# ------------------------------------------------------------- constants etc.

def const_to_py(cv, t=None):
    """A slang ConstantValue to an interpreter value (of type *t* if given)."""
    if cv is None:
        return None
    v = cv.value
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, str)):
        return norm(v, t) if t is not None else v
    name = type(v).__name__
    if name == "SVInt":
        return svint_to_py(v, t)
    if name == "NullValue":
        return None
    if isinstance(v, list):
        if t is not None:
            ct = t.canonicalType
            if ct.isUnpackedStruct:
                fields = [m for m in ct if m.kind == ast.SymbolKind.Field]
                return Struct(ct, {f.name: const_to_py(x, f.type) for f, x in zip(fields, v)})
            if ct.isArray:
                et = ct.arrayElementType
                return [const_to_py(x, et) for x in v]
        return [const_to_py(x) for x in v]
    try:
        return cv.convertToInt()
    except Exception:
        return v


def svint_to_py(v, t=None) -> int:
    """A pyslang SVInt to an int (of type *t* if given). X/Z read as 0."""
    if v.hasUnknown:
        v = v.flattenUnknowns()
    w = int(v.bitWidth)
    n = mask(int(v), w)
    if t is not None:
        return norm(n, t)
    return to_signed(n, w) if v.isSigned else n


def default_value(t):
    """The value a variable of type *t* holds before any assignment."""
    t = t.canonicalType
    if t.isIntegral:
        return 0
    if t.isString:
        return ""
    if t.isFloating:
        return 0.0
    if t.isAssociativeArray:
        return {}
    if t.isQueue or (t.isUnpackedArray and t.isDynamicallySizedArray):
        return []
    if t.isUnpackedArray:          # fixed-size unpacked
        r = t.fixedRange
        n = abs(int(r.left) - int(r.right)) + 1
        return [default_value(t.arrayElementType) for _ in range(n)]
    if t.isUnpackedStruct:
        out: Dict[str, Any] = {}
        for m in t:
            if m.kind == ast.SymbolKind.Field:
                out[m.name] = default_value(m.type)
        return Struct(t, out)
    # class handles, virtual interfaces, chandles, events: null
    return None


def copy_value(v):
    """Value semantics for assignment: containers and structs are copied,
    handles are not."""
    if isinstance(v, SymList):
        return SymList([copy_value(x) for x in v], v.size)
    if isinstance(v, list):
        return [copy_value(x) for x in v]
    if isinstance(v, Struct):
        return v.copy()
    if isinstance(v, dict):
        return {k: copy_value(x) for k, x in v.items()}
    return v


def truthy(v) -> bool:
    if isinstance(v, Sym):
        v = v.value
    if v is None:
        return False
    if isinstance(v, Obj):
        return True
    if isinstance(v, str):
        return v != ""
    return bool(v)


# ---------------------------------------------------------------- formatting

def _dec_width(w: int, signed: bool) -> int:
    return len(str((1 << (w - 1)) if signed else (1 << w) - 1)) + (1 if signed else 0)


def _fmt_p(a) -> str:
    if a is None:
        return "null"
    if isinstance(a, str):
        return '"' + a + '"'
    if isinstance(a, list):
        return "'{" + ", ".join(_fmt_p(x) for x in a) + "}"
    if isinstance(a, dict):
        return "'{" + ", ".join(f"{_fmt_p(k)}:{_fmt_p(v)}" for k, v in a.items()) + "}"
    if isinstance(a, Struct):
        return "'{" + ", ".join(f"{k}:{_fmt_p(v)}" for k, v in a.fields.items()) + "}"
    return str(a)


def fmt_sv(fmt: str, args: List[Tuple[Any, Any]], scope_name: str = "") -> str:
    """A small $sformatf. *args* are (value, pyslang Type or None) pairs.

    Supports %d %h %x %b %o %s %c %m %t %e %f %g %p %%, with an optional
    field width (``%0d`` is minimal; no width pads as SV does).
    """
    out = []
    i = 0
    ai = 0
    n = len(fmt)
    while i < n:
        c = fmt[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        i += 1
        j = i
        left = False
        if j < n and fmt[j] == "-":
            left = True
            j += 1
        k = j
        while k < n and (fmt[k].isdigit() or fmt[k] == "."):
            k += 1
        width_s = fmt[j:k]
        spec = fmt[k].lower() if k < n else ""
        i = k + 1
        if spec == "%":
            out.append("%")
            continue
        if spec == "m":
            out.append(scope_name)
            continue
        a, at = args[ai] if ai < len(args) else (0, None)
        ai += 1
        at = at.canonicalType if at is not None else None
        w = int(at.bitWidth) if at is not None and at.isIntegral else None
        signed = bool(at.isSigned) if w else False
        explicit = width_s != ""
        fw = int(width_s.split(".")[0]) if width_s and width_s.split(".")[0] else 0
        pad = " "
        if spec == "s":
            s = a if isinstance(a, str) else (int_to_str(a) if isinstance(a, int) else str(a))
        elif spec == "d":
            v = a if isinstance(a, int) else (int(a) if isinstance(a, float) else 0)
            s = str(v)
            if not explicit and w:
                fw = _dec_width(w, signed)
        elif spec in ("h", "x", "b", "o"):
            base = {"h": "x", "x": "x", "b": "b", "o": "o"}[spec]
            v = a if isinstance(a, int) else 0
            if v < 0:
                v = mask(v, w or max(32, v.bit_length() + 1))
            s = format(v, base)
            if not explicit and w:
                bits = {"x": 4, "b": 1, "o": 3}[base]
                fw = (w + bits - 1) // bits
                pad = "0"
        elif spec == "c":
            s = chr(a & 0xFF) if isinstance(a, int) else str(a)
        elif spec in ("e", "f", "g"):
            s = ("%" + width_s + spec) % float(a or 0)
            fw = 0
        elif spec == "p":
            s = _fmt_p(a)
        else:                       # %t and anything else
            s = str(a)
        if fw and len(s) < fw:
            s = s.ljust(fw) if left else s.rjust(fw, pad)
        out.append(s)
    return "".join(out)
