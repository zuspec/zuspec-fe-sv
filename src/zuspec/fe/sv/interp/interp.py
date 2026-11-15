"""The SV class-subset interpreter: runs SV class and function code over the
pyslang AST.

Ported from fw-hdl ``python/fw/hdl/fe/elab/interp.py`` (the elaboration
interpreter). Changes in the port:

- execution is sequential (plain recursion, no generator threads): fork,
  timing controls, ``wait`` and events are reported as unsupported;
- interface instances / virtual interfaces are dropped;
- fw-hdl specifics are gone (supervised-process and crossing-check special
  cases, the top-instance name for ``%m``); the host hooks in through
  :class:`~zuspec.fe.sv.interp.Intrinsics` instead;
- integral values carry their sign; unpacked structs are :class:`Struct`
  values; ``ref`` arguments alias the caller's variable;
- default arguments are evaluated in the callee's frame; named and Python
  calls are supported (:meth:`Interpreter.call`).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import zuspec.ir.core as ir
from pyslang import ast, syntax

from . import trace as tr
from .intrinsics import NOT_HANDLED, Intrinsics
from .trace import CtlFrame
from .values import (Obj, Struct, Sym, SymList, const_to_py, svint_to_py, copy_value,
                     default_value, fmt_sv, int_to_str, mask, norm, truthy, unwrap, width_of)

SK = ast.StatementKind
EK = ast.ExpressionKind
SymK = ast.SymbolKind
_AD = ast.ArgumentDirection
_STATIC = ast.VariableLifetime.Static
_STATIC_METHOD = int(ast.MethodFlags.Static.value)
_PURE_METHOD = int(ast.MethodFlags.Pure.value)
_SynK = syntax.SyntaxKind

__all__ = ["Interpreter", "InterpError", "InterpFatal", "Frame", "Ref"]


class InterpError(Exception):
    """The code cannot be run: an unsupported construct, a null access, a
    call with no body, ... ``sv_stack`` is the SV call stack (innermost
    last) when the error was raised."""

    def __init__(self, msg: str, sv_stack: Optional[List[str]] = None):
        super().__init__(msg)
        self.sv_stack = sv_stack or []

    def __str__(self) -> str:
        s = super().__str__()
        if self.sv_stack:
            s += "\n  SV call stack (innermost last):\n" + "\n".join(
                "    " + x for x in self.sv_stack)
        return s


class InterpFatal(InterpError):
    """``$fatal`` was executed."""


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _Return(Exception):
    pass


# ----------------------------------------------------------------- locations

class Ref:
    """A storage location (what a ``ref`` argument binds to). ``get()`` and
    ``set(v)``; a host can pass one for a ``ref`` argument of
    :meth:`Interpreter.call`."""

    def get(self):  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, v) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class Cell(Ref):
    """A free-standing location holding one value."""

    def __init__(self, v=None):
        self.v = v

    def get(self):
        return self.v

    def set(self, v) -> None:
        self.v = v


class _KeyLoc(Ref):
    """Element *k* of a dict or list *d*, holding values of type *t*."""
    __slots__ = ("d", "k", "t")

    def __init__(self, d, k, t):
        self.d, self.k, self.t = d, k, t

    def get(self):
        return self.d[self.k]

    def set(self, v) -> None:
        self.d[self.k] = norm(v, self.t)


class _FrameLoc(Ref):
    """A local of a frame (follows a ``ref`` binding)."""
    __slots__ = ("fr", "sym", "t")

    def __init__(self, fr, sym, t):
        self.fr, self.sym, self.t = fr, sym, t

    def get(self):
        v = self.fr.locals[self.sym]
        return v.get() if isinstance(v, _RefBinding) else v

    def set(self, v) -> None:
        cur = self.fr.locals.get(self.sym)
        if isinstance(cur, _RefBinding):
            cur.loc.set(norm(v, self.t))
        else:
            self.fr.locals[self.sym] = norm(v, self.t)


class _BitsLoc(Ref):
    """Bits [lo +: w] of the integral at *parent*."""
    __slots__ = ("parent", "lo", "w", "t")

    def __init__(self, parent, lo, w, t):
        self.parent, self.lo, self.w, self.t = parent, lo, w, t

    def get(self):
        return norm(self.parent.get() >> self.lo, self.t) if self.lo >= 0 else 0

    def set(self, v) -> None:
        if self.lo < 0:
            return
        old = self.parent.get()
        fm = ((1 << self.w) - 1) << self.lo
        self.parent.set((old & ~fm) | ((mask(v, self.w) << self.lo) & fm))


class _NullLoc(Ref):
    """An out-of-range element: reads the default, writes are ignored."""

    def __init__(self, default):
        self.default = default

    def get(self):
        return self.default

    def set(self, v) -> None:
        pass


class _ConcatLoc(Ref):
    def __init__(self, parts):          # [(loc, width)], msb first
        self.parts = parts

    def get(self):
        out = 0
        for loc, w in self.parts:
            out = (out << w) | mask(loc.get(), w)
        return out

    def set(self, v) -> None:
        pos = 0
        for loc, w in reversed(self.parts):
            loc.set(mask(v >> pos, w))
            pos += w


class _RefBinding:
    """A frame local bound to a caller location (a ``ref`` formal)."""
    __slots__ = ("loc",)

    def __init__(self, loc: Ref):
        self.loc = loc

    def get(self):
        return self.loc.get()


class Frame:
    """One activation: a subroutine with its receiver and locals."""

    __slots__ = ("sub", "this", "locals", "call")

    def __init__(self, sub, this: Optional[Obj], call=None):
        self.sub = sub              # SubroutineSymbol, or None
        self.this = this
        self.call = call            # the CallExpression that made this frame
        self.locals: Dict[Any, Any] = {}


_DEFAULT = object()     # an argument slot to fill from the formal's default


# ------------------------------------------------------------------ helpers

def _base(cls):
    b = cls.baseClass
    if b is None:
        return None
    b = b.canonicalType
    return b if b.kind == SymK.ClassType else None


def _is_static_prop(prop) -> bool:
    return prop.lifetime == _STATIC


def _owner_class(sub):
    """The class a non-static method belongs to (from its ``this``)."""
    tv = sub.thisVar
    if tv is None:
        return None
    t = tv.type.canonicalType
    return t if t.kind == SymK.ClassType else None


def _is_virtual(sub) -> bool:
    # isVirtual covers virtual, pure and interface-class methods
    return sub.kind == SymK.MethodPrototype or bool(sub.isVirtual)


def _src_text(node) -> str:
    return _syn_text(node.syntax)


def _syn_text(syn) -> str:
    """Source text of a syntax node without its leading trivia."""
    if syn is None:
        return ""
    s = str(syn)
    first = syn.getFirstToken()
    if first is not None:
        lead = "".join(str(x) for x in first.trivia)
        if s.startswith(lead):
            s = s[len(lead):]
    return s.strip()


def _ref_name(e) -> str:
    """A name for the array an element select reads: the variable or member
    name (``u`` of ``this.u[i]``), else the source text."""
    if e.kind in (EK.NamedValue, EK.HierarchicalValue):
        return e.symbol.name
    if e.kind == EK.MemberAccess:
        return e.member.name
    return _src_text(e)


def _same_node(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        ra, rb = a.sourceRange, b.sourceRange
        return (ra.start.offset == rb.start.offset and ra.end.offset == rb.end.offset
                and ra.start.buffer.id == rb.start.buffer.id)
    except Exception:
        return False


def _flat(s) -> list:
    if s is None:
        return []
    if s.kind == SK.List:
        out = []
        for x in s.list:
            out += _flat(x)
        return out
    if s.kind == SK.Block and s.blockKind == ast.StatementBlockKind.Sequential:
        return _flat(s.body)
    return [s]


class Interpreter:
    """Runs SV subroutines of a pyslang compilation.

    :param compilation: a pyslang ``ast.Compilation`` (elaborated).
    :param intrinsics: host hooks (default: :class:`Intrinsics`, no hooks).
    """

    def __init__(self, compilation, intrinsics: Optional[Intrinsics] = None):
        self.comp = compilation
        self.intrinsics = intrinsics if intrinsics is not None else Intrinsics()
        self.statics: Dict[Any, Any] = {}          # static variable symbol -> value
        self.objects: List[Obj] = []
        self.messages: List[tuple] = []
        self.stack: List[Frame] = []
        self._oid = 0
        self._lvref = None
        self._sm = getattr(compilation, "sourceManager", None)
        self._owner: Dict[Any, Any] = {}           # subroutine -> ClassType
        self._seen: set = set()
        self._static_decl: Dict[Any, bool] = {}
        #: Active control frames that depend on traced values (trace.py).
        self.ctl: List[CtlFrame] = []
        #: The receiver of the call the intrinsics are looking at, as a
        #: traced value (a Sym) when it was selected through a traced index.
        self.call_this: Any = None
        #: The call expression (AST) the intrinsics are looking at, or None
        #: for a call the host made.
        self.call_expr: Any = None
        self._this_sym = None

    # ================================================================ host API
    def allocate(self, cls) -> Obj:
        """A new object of ClassType *cls*: fields at their type defaults, no
        initializers or constructor run."""
        cls = cls.canonicalType if hasattr(cls, "canonicalType") else cls
        self._oid += 1
        o = Obj(cls, self._oid)
        self.objects.append(o)
        c = cls
        while c is not None:
            for m in c:
                if m.kind == SymK.ClassProperty and not _is_static_prop(m):
                    o.fields[m] = default_value(m.type)
            c = _base(c)
        return o

    def run_ctor(self, obj: Obj, cls=None, args: Sequence[Any] = (),
                 named: Optional[Mapping[str, Any]] = None) -> Obj:
        """Run the constructor chain of *cls* (default ``obj.cls``) on *obj*:
        the explicit ``new`` (whose body calls ``super.new``), or the implicit
        constructor (base chain, then property initializers)."""
        cls = cls or obj.cls
        ctor = cls.find("new")
        if ctor is not None and ctor.kind == SymK.Subroutine:
            self.call(ctor, obj, args, named)
        else:
            if args or named:
                raise InterpError(f"{cls} has no constructor taking arguments")
            self._implicit_ctor(cls, obj)
        return obj

    def construct(self, cls, args: Sequence[Any] = (),
                  named: Optional[Mapping[str, Any]] = None) -> Obj:
        """``new``: allocate, then run the constructor chain."""
        o = self.allocate(cls)
        return self.run_ctor(o, cls, args, named)

    def call(self, sub, this: Optional[Obj] = None, args: Sequence[Any] = (),
             named: Optional[Mapping[str, Any]] = None, keep_ctl: bool = False):
        """Run SubroutineSymbol *sub* (static if *this* is None; no virtual
        dispatch -- see :meth:`call_method`). Positional *args*, then *named*
        ``{name: value}``; missing arguments take their declared defaults.
        A ``ref``/``output`` argument may be given a :class:`Ref` to receive
        writes. The intrinsics see the call. Returns the return value.

        The call starts with no traced control frames (``trace.py``) unless
        *keep_ctl*: an intrinsic that runs SV on behalf of the code calling
        it passes True to keep that code's frames."""
        if keep_ctl:
            return self._call_py1(sub, this, args, named, True)
        return self._call_py(sub, this, args, named, hooks=True)

    def call_body(self, sub, this: Optional[Obj] = None, args: Sequence[Any] = (),
                  named: Optional[Mapping[str, Any]] = None):
        """As :meth:`call`, but the intrinsics are not consulted for this
        call (they still are for the calls it makes). For a host that wraps
        an SV body."""
        return self._call_py(sub, this, args, named, hooks=False)

    def call_method(self, obj: Obj, name: str, args: Sequence[Any] = (),
                    named: Optional[Mapping[str, Any]] = None):
        """Call method *name* on *obj* with virtual dispatch from ``obj.cls``."""
        sub = self.find_method(obj.cls, name)
        if sub is None:
            raise InterpError(f"{obj.cls} has no method {name}")
        return self.call(sub, obj, args, named)

    def find_method(self, cls, name: str):
        """The subroutine *name* as seen from *cls*: its own or the nearest
        base's (a prototype with no body if that is all there is)."""
        proto = None
        self._register(cls)
        c = cls
        while c is not None:
            m = c.find(name)
            if m is not None:
                if m.kind == SymK.Subroutine:
                    return m
                if m.kind == SymK.MethodPrototype and proto is None:
                    impl = getattr(m, "subroutine", None)
                    if impl is not None:
                        return impl
                    proto = m
            c = _base(c)
        return proto

    def dispatch(self, cls, sub):
        """Virtual dispatch of *sub* on an object of class *cls*."""
        name = sub.name
        c = cls
        while c is not None:
            m = c.find(name)
            if m is not None and m.kind == SymK.Subroutine and m.body is not None \
                    and not (int(m.flags) & _PURE_METHOD):
                return m
            c = _base(c)
        return sub

    def static_field(self, cls, name: str):
        """The value of static property *name* of class *cls* (each
        specialization has its own)."""
        return self._static(self._static_sym(cls, name))

    def set_static_field(self, cls, name: str, value) -> None:
        sym = self._static_sym(cls, name)
        self.statics[sym] = norm(value, sym.type)

    def owner_class(self, sub):
        """The class specialization (ClassType) *sub* is a member of, or None
        for a package / unit function. Known for every method with a body
        (from ``this``) and for every static method the interpreter has
        reached through a class it has seen (a static call ``C#(P)::f()``
        resolves ``C#(P)`` in the caller's scope)."""
        o = self._owner.get(sub)
        if o is not None:
            return o
        t = _owner_class(sub)
        if t is not None:
            self._register(t)
            return self._owner.get(sub, t)
        return None

    def param_value(self, sym, t=None):
        """The value of parameter *sym* as code reads it: what the host's
        :meth:`Intrinsics.symbolic_param` makes of it, if anything; else its
        constant value, traced (a Sym) when it is a ``localparam`` whose
        initializer is written over traced values."""
        r = self.intrinsics.symbolic_param(self, sym)
        if r is not None:
            return r
        v = const_to_py(sym.value, t if t is not None else sym.type)
        if getattr(sym, "isLocalParam", False):
            syn = getattr(sym.declaredType, "initializerSyntax", None)
            scope = sym.parentScope
            if syn is not None and scope is not None:
                try:
                    tv = tr.SynTracer(self).value(syn, scope)
                except Exception:          # pragma: no cover - be safe: untraced
                    tv = None
                if isinstance(tv, Sym):
                    return Sym(v, tv.expr)
        return v

    def _register(self, cls) -> None:
        """Record *cls* (and its bases) as the owner of its subroutines."""
        while cls is not None and cls not in self._seen:
            self._seen.add(cls)
            for m in cls:
                k = m.kind
                if k == SymK.Subroutine:
                    self._owner.setdefault(m, cls)
                elif k == SymK.MethodPrototype:
                    self._owner.setdefault(m, cls)
                    impl = getattr(m, "subroutine", None)
                    if impl is not None:
                        self._owner.setdefault(impl, cls)
            cls = _base(cls)

    def _note_static_owner(self, e, sub, fr: Frame) -> None:
        """Find the class of static method *sub* called by *e* from *fr*."""
        syn = e.syntax
        left = getattr(syn, "left", None) if syn is not None else None
        if left is not None and left.kind == _SynK.ScopedName:
            scope = fr.sub if fr.sub is not None else None
            if scope is not None:
                try:
                    cand = scope.lookupName(_syn_text(left.left))
                except Exception:
                    cand = None
                if cand is not None and cand.kind == SymK.TypeAlias:
                    cand = cand.targetType.type.canonicalType
                if cand is not None and cand.kind == SymK.ClassType:
                    self._register(cand)
                    return
        if fr.this is not None:
            self._register(fr.this.cls)
        elif fr.sub is not None:
            self.owner_class(fr.sub)

    def where(self, node) -> str:
        """``file:line`` of an AST node, for messages."""
        try:
            loc = node.sourceRange.start if hasattr(node, "sourceRange") else node.location
            sm = self._sm
            if sm is None:
                return ""
            loc = sm.getFullyOriginalLoc(loc)
            return f"{sm.getFileName(loc)}:{sm.getLineNumber(loc)}"
        except Exception:
            return ""

    def error(self, msg: str, node=None) -> InterpError:
        w = self.where(node) if node is not None else ""
        return InterpError(f"{w}: {msg}" if w else msg, self._sv_stack())

    # ============================================================== internals
    def _sv_stack(self) -> List[str]:
        out = []
        for f in self.stack:
            s = f.sub
            name = s.name if s is not None else "<init>"
            owner = self.owner_class(s) if s is not None else None
            label = f"{owner}::{name}" if owner is not None else name
            if f.call is not None:
                label += f" (called at {self.where(f.call)})"
            out.append(label)
        return out

    def _static_sym(self, cls, name):
        c = cls
        while c is not None:
            m = c.find(name)
            if m is not None and m.kind in (SymK.ClassProperty, SymK.Variable) \
                    and _is_static_prop(m):
                return m
            c = _base(c)
        raise InterpError(f"{cls} has no static property {name}")

    def _static(self, sym):
        if sym not in self.statics:
            # The default first: an initializer may read the variable itself
            # (`static T me = get();` where get() tests `me == null`).
            self.statics[sym] = default_value(sym.type)
            if sym.initializer is not None:
                self.statics[sym] = norm(copy_value(self.eval(sym.initializer,
                                                              Frame(None, None))), sym.type)
        return self.statics[sym]

    # ------------------------------------------------------------------ calls
    def _call_py(self, sub, this, args, named, hooks):
        saved, self.ctl = self.ctl, []          # a host call starts with no frames
        try:
            return self._call_py1(sub, this, args, named, hooks)
        finally:
            self.ctl = saved

    def _call_py1(self, sub, this, args, named, hooks):
        if sub.kind == SymK.MethodPrototype and getattr(sub, "subroutine", None) is not None:
            sub = sub.subroutine
        formals = list(sub.arguments)
        named = dict(named or {})
        if len(args) > len(formals):
            raise InterpError(f"{sub.name}: {len(args)} arguments for {len(formals)} formals")
        entries = []
        for i, fa in enumerate(formals):
            if i < len(args):
                v = args[i]
            elif fa.name in named:
                v = named.pop(fa.name)
            else:
                entries.append(_DEFAULT)
                continue
            if fa.direction == _AD.Ref or fa.direction == _AD.Out or fa.direction == _AD.InOut:
                if isinstance(v, Ref):
                    entries.append(("ref" if fa.direction == _AD.Ref else "out", v))
                    continue
                if fa.direction == _AD.Ref:
                    entries.append(("ref", Cell(v)))
                    continue
            entries.append(("in", copy_value(v)))
        if named:
            raise InterpError(f"{sub.name}: no formal(s) {', '.join(named)}")
        return self._invoke(sub, this, entries, None, hooks)

    def _eval_args(self, sub, e, fr: Frame):
        """Actual arguments of call *e* (evaluated in *fr*) as entries."""
        entries = []
        actuals = list(e.arguments)
        for i, fa in enumerate(sub.arguments):
            a = actuals[i] if i < len(actuals) else None
            if a is None or a.kind == EK.EmptyArgument or _same_node(a, fa.defaultValue):
                entries.append(_DEFAULT)
                continue
            d = fa.direction
            if d == _AD.In:
                entries.append(("in", copy_value(self.eval(a, fr))))
                continue
            lhs = a.left if a.kind == EK.Assignment else a
            loc = self.lvalue(lhs, fr)
            if d == _AD.Ref:
                entries.append(("ref", loc))
            elif d == _AD.InOut:
                entries.append(("inout", loc))
            else:
                entries.append(("out", loc))
        return entries

    def _invoke(self, sub, this, entries, call_expr, hooks=True, this_sym=None):
        fr = Frame(sub, this, call_expr)
        outs = []
        formals = list(sub.arguments)
        if self.ctl:
            self.ctl[-1].decls.update(formals)
            if sub.returnValVar is not None:
                self.ctl[-1].decls.add(sub.returnValVar)
        for fa, ent in zip(formals, entries):
            if ent is _DEFAULT:
                dv = fa.defaultValue
                if dv is not None:
                    # A default is the callee's: evaluated with its frame
                    # on the stack (a host can see which call it is for).
                    self.stack.append(fr)
                    try:
                        v = self.eval(dv, fr)
                    finally:
                        self.stack.pop()
                else:
                    v = default_value(fa.type)
                fr.locals[fa] = norm(copy_value(v), fa.type)
                continue
            how, x = ent
            if how == "in":
                fr.locals[fa] = norm(x, fa.type)
            elif how == "ref":
                fr.locals[fa] = _RefBinding(x)
            elif how == "inout":
                fr.locals[fa] = norm(copy_value(x.get()), fa.type)
                outs.append((fa, x))
            else:
                fr.locals[fa] = default_value(fa.type)
                outs.append((fa, x))
        rv = sub.returnValVar
        if rv is not None:
            fr.locals[rv] = default_value(rv.type)

        if hooks:
            vals = []
            for fa in formals:
                v = fr.locals.get(fa)
                vals.append(v.get() if isinstance(v, _RefBinding) else v)
            self.call_this = this_sym
            self.call_expr = call_expr
            r = self.intrinsics.call(self, sub, this, vals,
                                     {fa.name: v for fa, v in zip(formals, vals)})
            if r is not NOT_HANDLED:
                return norm(r, rv.type) if rv is not None else r

        if sub.kind != SymK.Subroutine or sub.body is None or (int(sub.flags) & _PURE_METHOD):
            raise self.error(f"call to {self._label(sub)}, which has no body", call_expr)
        self.stack.append(fr)
        try:
            if sub.name == "new" and this is not None and sub.thisVar is not None:
                self._ctor_body(sub, this, fr)
            else:
                try:
                    self.exec(sub.body, fr)
                except _Return:
                    pass
        finally:
            self.stack.pop()
        for fa, loc in outs:
            loc.set(fr.locals[fa])
        if rv is not None:
            return fr.locals[rv]
        return None

    def _label(self, sub) -> str:
        owner = self.owner_class(sub)
        return f"{owner}::{sub.name}" if owner is not None else sub.name

    def _resolve_call(self, e, fr: Frame):
        sub = e.subroutine
        if sub.kind == SymK.MethodPrototype and getattr(sub, "subroutine", None) is not None:
            sub = sub.subroutine
        this: Optional[Obj] = None
        this_sym = None
        if e.thisClass is not None:
            recv = self.eval(e.thisClass, fr)
            if isinstance(recv, Sym):
                this_sym, recv = recv, recv.value
            if recv is None:
                raise self.error(f"null object access calling {sub.name}", e)
            if not isinstance(recv, Obj):
                raise self.error(f"call {sub.name} on non-object {recv!r}", e)
            this = recv
        elif not (int(sub.flags) & _STATIC_METHOD) and fr.this is not None and (
                sub.thisVar is not None or self.owner_class(sub) is not None
                or self.find_method(fr.this.cls, sub.name) is not None):
            this = fr.this
        elif sub not in self._owner:
            self._note_static_owner(e, sub, fr)
        if this is not None:
            self._register(this.cls)
        if this is not None and _is_virtual(sub) and not self._is_static_bound(e):
            sub = self.dispatch(this.cls, sub)
        self._this_sym = this_sym
        return sub, this

    @staticmethod
    def _is_static_bound(e) -> bool:
        """``super.f()`` and ``C::f()`` are not dispatched."""
        if e.thisClass is not None:
            return False
        txt = _src_text(e)
        if txt.startswith("super"):
            return True
        head = txt.split("(", 1)[0]
        return "::" in head

    def _call_value(self, e, fr: Frame):
        if e.isSystemCall:
            return self._system_call(e, fr)
        sub, this = self._resolve_call(e, fr)
        this_sym = self._this_sym
        entries = self._eval_args(sub, e, fr)
        return self._invoke(sub, this, entries, e, this_sym=this_sym)

    # ------------------------------------------------------------ construction
    def _new_class(self, e, fr: Frame):
        if e.isSuperClass:
            self._super_new(e, fr.this, fr)
            return None
        cls = e.type.canonicalType
        if not cls.isClass:
            raise self.error(f"new of non-class type {cls}", e)
        o = self.intrinsics.new_object(self, cls, fr.sub)
        if o is not None:
            return o
        o = self.allocate(cls)
        cc = e.constructorCall
        if cc is not None and cc.kind == EK.Call:
            sub = cc.subroutine
            self._invoke(sub, o, self._eval_args(sub, cc, fr), cc)
        else:
            ctor = cls.find("new")
            if ctor is not None and ctor.kind == SymK.Subroutine:
                self._invoke(ctor, o, [_DEFAULT] * len(list(ctor.arguments)), e)
            else:
                self._implicit_ctor(cls, o)
        return o

    def _base_ctor(self, cls, o: Obj, fr: Optional[Frame]) -> None:
        """Run the base-class part of *cls*'s construction on *o*."""
        base = _base(cls)
        if base is None:
            return
        bcall = cls.baseConstructorCall
        if bcall is not None and bcall.kind == EK.Call:       # `extends B(args)`
            sub = bcall.subroutine
            self._invoke(sub, o, self._eval_args(sub, bcall, Frame(None, o)), bcall)
            return
        bctor = base.find("new")
        if bctor is not None and bctor.kind == SymK.Subroutine:
            self._invoke(bctor, o, [_DEFAULT] * len(list(bctor.arguments)), None)
        else:
            self._implicit_ctor(base, o)

    def _implicit_ctor(self, cls, o: Obj) -> None:
        self._base_ctor(cls, o, None)
        self._init_props(cls, o)

    def _init_props(self, cls, o: Obj) -> None:
        fr = Frame(None, o)
        for m in cls:
            if m.kind == SymK.ClassProperty and not _is_static_prop(m) \
                    and m.initializer is not None:
                o.fields[m] = norm(copy_value(self.eval(m.initializer, fr)), m.type)

    def _ctor_body(self, sub, o: Obj, fr: Frame) -> None:
        cls = _owner_class(sub)
        stmts = _flat(sub.body)
        first = stmts[0] if stmts else None
        explicit_super = (first is not None and first.kind == SK.ExpressionStatement
                          and first.expr.kind == EK.NewClass and first.expr.isSuperClass)
        if explicit_super:
            self._super_new(first.expr, o, fr)
            rest = stmts[1:]
        else:
            self._base_ctor(cls, o, fr)
            rest = stmts
        self._init_props(cls, o)
        try:
            for x in rest:
                self.exec(x, fr)
        except _Return:
            pass

    def _super_new(self, e, o: Obj, fr: Frame) -> None:
        """``super.new(...)``: the base constructor on the object being built."""
        cc = e.constructorCall
        if cc is not None and cc.kind == EK.Call:
            sub = cc.subroutine
            self._invoke(sub, o, self._eval_args(sub, cc, fr), cc)
            return
        cls = _owner_class(fr.sub) if fr.sub is not None else None
        base = _base(cls) if cls is not None else None
        if base is not None:
            bctor = base.find("new")
            if bctor is not None and bctor.kind == SymK.Subroutine:
                self._invoke(bctor, o, [_DEFAULT] * len(list(bctor.arguments)), e)
            else:
                self._implicit_ctor(base, o)

    # ------------------------------------------------------------ statements
    def exec(self, s, fr: Frame) -> None:
        k = s.kind
        if k == SK.List:
            for x in s.list:
                self.exec(x, fr)
        elif k == SK.Block:
            if s.blockKind != ast.StatementBlockKind.Sequential:
                raise self.error("fork is not supported", s)
            self.exec(s.body, fr)
        elif k == SK.ExpressionStatement:
            self.eval(s.expr, fr)
        elif k == SK.VariableDeclaration:
            sym = s.symbol
            if sym.lifetime == _STATIC and self._explicit_static(sym):
                self._static(sym)
                return
            if self.ctl:
                self.ctl[-1].decls.add(sym)
            v = (self.eval(sym.initializer, fr) if sym.initializer is not None
                 else default_value(sym.type))
            fr.locals[sym] = norm(copy_value(v), sym.type)
        elif k == SK.Conditional:
            ok = True
            traced = []
            for c in s.conditions:
                if c.pattern is not None:
                    raise self.error("pattern-matching if is not supported", s)
                cv = self.eval(c.expr, fr)
                if isinstance(cv, Sym):
                    traced.append(cv)
                if not truthy(cv):
                    ok = False
                    break
            if traced:
                self._traced_if(s, traced, ok, fr)
            elif ok:
                self.exec(s.ifTrue, fr)
            elif s.ifFalse is not None:
                self.exec(s.ifFalse, fr)
        elif k == SK.Case:
            v = self.eval(s.expr, fr)
            item_stmt = None
            for item in s.items:
                if any(self._case_match(v, x, fr) for x in item.expressions):
                    item_stmt = item.stmt
                    break
            else:
                item_stmt = s.defaultCase
            if isinstance(v, Sym):
                self._opaque(s, "a case on a parameter", lambda: item_stmt is not None and
                             self.exec(item_stmt, fr),
                             any(tr.may_call(it.stmt) for it in s.items
                                 if it.stmt is not item_stmt)
                             or (s.defaultCase is not None and s.defaultCase is not item_stmt
                                 and tr.may_call(s.defaultCase)))
            elif item_stmt is not None:
                self.exec(item_stmt, fr)
        elif k == SK.ForLoop:
            self._for(s, fr)
        elif k == SK.ForeachLoop:
            try:
                self._foreach(s, list(s.loopDims), 0, self.eval(s.arrayRef, fr),
                              s.arrayRef.type, fr)
            except _Break:
                pass
        elif k == SK.RepeatLoop:
            n = self.eval(s.count, fr)

            def rep():
                for _ in range(max(0, unwrap(n) or 0)):
                    try:
                        self.exec(s.body, fr)
                    except _Break:
                        break
                    except _Continue:
                        pass
            if isinstance(n, Sym):
                self._opaque(s, "a repeat count that is a parameter", rep, tr.may_call(s.body))
            else:
                rep()
        elif k == SK.WhileLoop:
            def loop():
                while True:
                    c = self.eval(s.cond, fr)
                    if isinstance(c, Sym) and not self._in_opaque(s):
                        self._opaque(s, "a while loop on a parameter", loop, tr.may_call(s.body))
                        return
                    if not truthy(c):
                        return
                    try:
                        self.exec(s.body, fr)
                    except _Break:
                        return
                    except _Continue:
                        pass
            loop()
        elif k == SK.DoWhileLoop:
            def doloop():
                while True:
                    try:
                        self.exec(s.body, fr)
                    except _Break:
                        return
                    except _Continue:
                        pass
                    c = self.eval(s.cond, fr)
                    if isinstance(c, Sym) and not self._in_opaque(s):
                        self._opaque(s, "a do-while loop on a parameter", lambda: truthy(c) and
                                     doloop(), tr.may_call(s.body))
                        return
                    if not truthy(c):
                        return
            doloop()
        elif k == SK.ForeverLoop:
            while True:
                try:
                    self.exec(s.body, fr)
                except _Break:
                    break
                except _Continue:
                    pass
        elif k == SK.Return:
            if s.expr is not None and fr.sub is not None and fr.sub.returnValVar is not None:
                rv = fr.sub.returnValVar
                v = norm(copy_value(self.eval(s.expr, fr)), rv.type)
                if self.ctl and rv not in self.ctl[-1].decls:
                    v = tr.taint(v)
                fr.locals[rv] = v
            raise _Return()
        elif k == SK.Break:
            raise _Break()
        elif k == SK.Continue:
            raise _Continue()
        elif k == SK.Empty:
            pass
        elif k == SK.ImmediateAssertion:
            if not truthy(self.eval(s.cond, fr)):
                if s.ifFalse is not None:
                    self.exec(s.ifFalse, fr)
                else:
                    self.intrinsics.message(self, "error",
                                            f"assertion failed at {self.where(s)}")
            elif s.ifTrue is not None:
                self.exec(s.ifTrue, fr)
        else:
            raise self.error(f"unsupported statement {k.name}", s)

    # ------------------------------------------------------------ traced control
    def _run_frame(self, f: CtlFrame, fn) -> None:
        """Run *fn* with control frame *f* active; tell the host when it ends."""
        self.ctl.append(f)
        try:
            fn()
        except (_Return, _Break, _Continue):
            f.irregular = True
            raise
        finally:
            self.ctl.pop()
            self.intrinsics.ctl_end(self, f)

    def _frame(self, kind: str, s) -> CtlFrame:
        f = CtlFrame(kind, s, self.where(s))
        f.outer = tuple((x, x.iteration) for x in self.ctl)
        return f

    def _opaque(self, s, reason: str, fn, unexplored: bool) -> None:
        f = self._frame("opaque", s)
        f.reason = reason
        f.unexplored = bool(unexplored) or tr.may_call(s)
        self._run_frame(f, fn)

    def _in_opaque(self, s) -> bool:
        return any(f.node is s for f in self.ctl)

    def _traced_if(self, s, traced, ok: bool, fr: Frame) -> None:
        f = self._frame("if", s)
        f.cond = traced[0].expr if len(s.conditions) == 1 else None
        arm = self.intrinsics.ctl_force(self, f)
        if arm is not None:
            f.forced = True
            ok = bool(arm)
        f.taken = ok
        f.unexplored = tr.may_effect(s.ifFalse if ok else s.ifTrue)

        def arm():
            if ok:
                self.exec(s.ifTrue, fr)
            elif s.ifFalse is not None:
                self.exec(s.ifFalse, fr)
        self._run_frame(f, arm)

    def _for_var(self, s, lvars, fr):
        """The variable of a ``for`` that counts up by one: ``(symbol,
        location, declared-in-the-loop)``, or None."""
        steps = list(s.steps or [])
        if len(steps) != 1:
            return None
        st = steps[0]
        target = None
        if st.kind == EK.UnaryOp and st.op.name in ("Postincrement", "Preincrement"):
            target = st.operand
        elif st.kind == EK.Assignment and st.isCompound and st.right.kind == EK.BinaryOp \
                and st.right.op.name == "Add" and st.right.right.constant is not None \
                and const_to_py(st.right.right.constant) == 1:
            target = st.left
        if target is None or target.kind != EK.NamedValue:
            return None
        sym = target.symbol
        if sym in lvars:
            return sym, _FrameLoc(fr, sym, sym.type), True
        inits = list(s.initializers or [])
        if len(inits) == 1 and inits[0].kind == EK.Assignment \
                and inits[0].left.kind == EK.NamedValue and inits[0].left.symbol is sym:
            return sym, self.lvalue(inits[0].left, fr), False
        return None

    def _loop_plain(self, s, fr: Frame) -> None:
        while s.stopExpr is None or truthy(self.eval(s.stopExpr, fr)):
            try:
                self.exec(s.body, fr)
            except _Break:
                break
            except _Continue:
                pass
            for st in s.steps:
                self.eval(st, fr)

    def _for(self, s, fr: Frame) -> None:
        lvars = list(s.loopVars or [])
        for sym in lvars:
            if self.ctl:
                self.ctl[-1].decls.add(sym)
            fr.locals[sym] = norm(self.eval(sym.initializer, fr)
                                  if sym.initializer is not None
                                  else default_value(sym.type), sym.type)
        for init in (s.initializers or []):
            self.eval(init, fr)
        var = self._for_var(s, lvars, fr) if s.stopExpr is not None else None
        if var is None:
            if s.stopExpr is not None and isinstance(self.eval(s.stopExpr, fr), Sym):
                self._opaque(s, "a loop on a parameter that does not count up by one",
                             lambda: self._loop_plain(s, fr), tr.may_call(s.body))
            else:
                self._loop_plain(s, fr)
            return
        sym, loc, inner = var
        v0 = loc.get()
        leaf = ir.ExprRefLocal(name=sym.name)
        loc.set(Sym(unwrap(v0), leaf))
        c = self.eval(s.stopExpr, fr)
        dep = isinstance(v0, Sym) or (isinstance(c, Sym) and (
            c.expr is None or any(x is not leaf for x in tr.leaves(c.expr))))
        loc.set(v0)
        if not dep:
            self._loop_plain(s, fr)
            return
        start = tr.expr_of(v0)
        if start is None or c.expr is None:
            self._opaque(s, "a loop whose bounds are not expressions of parameters",
                         lambda: self._loop_plain(s, fr), tr.may_call(s.body))
            return
        f = self._frame("loop", s)
        f.var, f.leaf, f.start, f.cond = sym.name, leaf, start, c.expr
        if inner:
            f.decls.add(sym)
        cur = unwrap(v0)

        def run():
            nonlocal cur
            k = 0
            while True:
                loc.set(Sym(cur, leaf))
                if not truthy(self.eval(s.stopExpr, fr)):
                    break
                f.value, f.iteration = cur, k
                try:
                    self.exec(s.body, fr)
                except _Break:
                    f.irregular = True
                    break
                except _Continue:
                    pass
                for st in s.steps:
                    self.eval(st, fr)
                nv = unwrap(loc.get())
                if nv != cur + 1:
                    f.irregular = True
                cur = nv
                k += 1
            f.count = k
            if k == 0:
                f.unexplored = tr.may_call(s.body)
        self._run_frame(f, run)
        loc.set(cur if inner else Sym(cur, None))

    def _explicit_static(self, sym) -> bool:
        """A local declared ``static`` (a local of a static function without
        the keyword is run as automatic: re-initialized on each call)."""
        r = self._static_decl.get(sym)
        if r is None:
            r = False
            syn = sym.syntax
            p = syn.parent if syn is not None else None
            for t in (getattr(p, "modifiers", None) or []):
                if getattr(t, "valueText", "") == "static":
                    r = True
            self._static_decl[sym] = r
        return r

    def _case_match(self, v, x, fr) -> bool:
        v = unwrap(v)
        if x.kind == EK.ValueRange:
            return unwrap(self.eval(x.left, fr)) <= v <= unwrap(self.eval(x.right, fr))
        return self._eq(v, unwrap(self.eval(x, fr)))

    def _foreach(self, s, dims, i, arr, t, fr: Frame) -> None:
        if isinstance(arr, SymList) and arr.size is not None and i == 0:
            if len(dims) == 1 and dims[0].loopVar is not None and arr.size.expr is not None:
                self._foreach_traced(s, dims[0].loopVar, arr, fr)
            else:
                self._opaque(s, "a foreach over an array sized by a parameter",
                             lambda: self._foreach_plain(s, dims, i, arr, t, fr),
                             tr.may_call(s.body))
            return
        self._foreach_plain(s, dims, i, arr, t, fr)

    def _foreach_traced(self, s, var, arr: SymList, fr: Frame) -> None:
        f = self._frame("loop", s)
        leaf = ir.ExprRefLocal(name=var.name)
        f.var, f.leaf, f.start = var.name, leaf, ir.ExprConstant(value=0)
        f.cond = ir.ExprCompare(left=leaf, ops=[ir.CmpOp.Lt], comparators=[arr.size.expr])
        f.decls.add(var)

        def run():
            for k in range(len(arr)):
                fr.locals[var] = Sym(k, leaf)
                f.value, f.iteration = k, k
                try:
                    self.exec(s.body, fr)
                except _Continue:
                    pass
                f.count = k + 1
            if len(arr) == 0:
                f.unexplored = tr.may_call(s.body)
        self._run_frame(f, run)

    def _foreach_plain(self, s, dims, i, arr, t, fr: Frame) -> None:
        var = dims[i].loopVar
        t = t.canonicalType
        if isinstance(arr, dict):
            keys = sorted(arr.keys())
        elif isinstance(arr, (list, str)) and not (
                t.isUnpackedArray and not t.isDynamicallySizedArray and t.hasFixedRange):
            keys = list(range(len(arr)))
        elif t.hasFixedRange:           # fixed unpacked array, or packed vector
            r = t.fixedRange
            lft, rgt = int(r.left), int(r.right)
            step = 1 if rgt >= lft else -1
            keys = list(range(lft, rgt + step, step))
        else:
            keys = []
        last = i + 1 >= len(dims)
        et = t.arrayElementType if t.isArray else None
        for n, key in enumerate(keys):
            if var is not None:
                fr.locals[var] = key
            if last:
                try:
                    self.exec(s.body, fr)
                except _Continue:
                    pass
            else:
                sub = arr[key] if isinstance(arr, dict) else arr[n]
                self._foreach_plain(s, dims, i + 1, sub, et, fr)

    # ------------------------------------------------------------ expressions
    def eval(self, e, fr: Frame):
        k = e.kind
        if k == EK.IntegerLiteral:
            return svint_to_py(e.value, e.type)
        if k == EK.RealLiteral or k == EK.TimeLiteral:
            return float(e.value)
        if k == EK.StringLiteral:
            cv = e.constant
            v = const_to_py(cv) if cv is not None else None
            if isinstance(v, str):
                return v
            txt = _src_text(e)
            if len(txt) >= 2 and txt[0] == '"' and txt[-1] == '"':
                return txt[1:-1].encode().decode("unicode_escape")
            return v if v is not None else ""
        if k == EK.UnbasedUnsizedIntegerLiteral:
            bit = _src_text(e)[-1:]
            return norm(-1 if bit == "1" else 0, e.type)
        if k == EK.NullLiteral:
            return None
        if k == EK.NamedValue or k == EK.HierarchicalValue:
            return self._named(e, fr)
        if k == EK.MemberAccess:
            return self._member(e, fr)
        if k == EK.ElementSelect:
            return self._elem(e, fr)
        if k == EK.RangeSelect:
            return self.lvalue(e, fr).get() if not isinstance(
                unwrap(self.eval(e.value, fr)), list) else self._range_list(e, fr)
        if k == EK.Conversion:
            return self._convert(e, fr)
        if k == EK.UnaryOp:
            return self._unary(e, fr)
        if k == EK.BinaryOp:
            return self._binary(e, fr)
        if k == EK.ConditionalOp:
            ok = True
            traced = []
            for c in e.conditions:
                cv = self.eval(c.expr, fr)
                if isinstance(cv, Sym):
                    traced.append(cv)
                if not truthy(cv):
                    ok = False
                    break
            if not traced:
                return self.eval(e.left if ok else e.right, fr)
            return self._traced_cond(e, traced, ok, fr)
        if k == EK.Assignment:
            return self._assign_expr(e, fr)
        if k == EK.Call:
            return self._call_value(e, fr)
        if k == EK.NewClass:
            return self._new_class(e, fr)
        if k == EK.NewArray:
            return self._new_array(e, fr)
        if k == EK.CopyClass:
            src = self.eval(e.sourceExpr, fr)
            if src is None:
                raise self.error("shallow copy of null", e)
            self._oid += 1
            o = Obj(src.cls, self._oid)
            o.fields = {s: copy_value(v) if not isinstance(v, Obj) else v
                        for s, v in src.fields.items()}
            self.objects.append(o)
            return o
        if k == EK.Concatenation:
            return self._concat(e, fr)
        if k == EK.Replication:
            n = self.eval(e.count, fr)
            v = self.eval(e.concat, fr)
            if isinstance(n, Sym) or isinstance(v, Sym):
                return Sym(self._replicate(e, unwrap(n), unwrap(v)), None)
            return self._replicate(e, n, v)
        if k in (EK.SimpleAssignmentPattern, EK.StructuredAssignmentPattern,
                 EK.ReplicatedAssignmentPattern):
            return self._pattern(e, fr)
        if k == EK.Inside:
            v = self.eval(e.left, fr)
            traced = isinstance(v, Sym)
            v = unwrap(v)
            r = 0
            for rg in e.rangeList:
                if rg.kind == EK.ValueRange:
                    lo, hi = self.eval(rg.left, fr), self.eval(rg.right, fr)
                    traced = traced or isinstance(lo, Sym) or isinstance(hi, Sym)
                    if unwrap(lo) <= v <= unwrap(hi):
                        r = 1
                else:
                    rv = self.eval(rg, fr)
                    traced = traced or isinstance(rv, Sym)
                    rv = unwrap(rv)
                    if isinstance(rv, list) and not isinstance(v, list):
                        if v in rv:
                            r = 1
                    elif rv == v:
                        r = 1
                if r and not traced:
                    return 1
            return Sym(r, None) if traced else r
        return self._eval_rest(e, k, fr)

    def _replicate(self, e, n, v):
        if isinstance(v, str):
            return v * n
        w = width_of(e.concat.type) or 0
        out = 0
        for _ in range(n):
            out = (out << w) | mask(v, w)
        return out

    def _eval_rest(self, e, k, fr: Frame):
        if k == EK.LValueReference:
            return self._lvref
        if k == EK.TypeReference or k == EK.DataType:
            return e.type
        if k == EK.EmptyArgument:
            return None
        if e.constant is not None:
            return const_to_py(e.constant, e.type)
        raise self.error(f"unsupported expression {k.name}", e)

    def _named(self, e, fr: Frame):
        sym = e.symbol
        sk = sym.kind
        if sk in (SymK.Variable, SymK.FormalArgument, SymK.Iterator):
            if sym in fr.locals:
                v = fr.locals[sym]
                return v.get() if isinstance(v, _RefBinding) else v
            if sk == SymK.Variable and sym.name == "this":
                return fr.this
            if sym.lifetime == _STATIC:
                return self._static(sym)
            raise self.error(f"variable {sym.name} has no value", e)
        if sk == SymK.ClassProperty:
            if _is_static_prop(sym):
                return self._static(sym)
            if fr.this is None:
                raise self.error(f"property {sym.name} without an object", e)
            return self._field_get(fr.this, sym, e)
        if sk == SymK.Parameter:
            return self.param_value(sym, e.type)
        if sk == SymK.EnumValue:
            return const_to_py(sym.value, e.type)
        if e.constant is not None:
            return const_to_py(e.constant, e.type)
        raise self.error(f"unsupported named value {sym.name} ({sk.name})", e)

    def _traced_cond(self, e, traced, ok: bool, fr: Frame):
        """``c ? a : b`` with a traced condition: the taken arm's value, with
        ``ExprIfExp`` over both arms when the other one is safe to evaluate."""
        taken, other = (e.left, e.right) if ok else (e.right, e.left)
        tv = self.eval(taken, fr)
        if len(e.conditions) == 1 and traced[0].expr is not None and tr.is_pure(other):
            try:
                ov = self.eval(other, fr)
            except InterpError:
                ov = None
            lv, rv = (tv, ov) if ok else (ov, tv)
            el, er = tr.expr_of(lv), tr.expr_of(rv)
            if el is not None and er is not None and not tr.is_handle(tv):
                return Sym(tv, ir.ExprIfExp(test=traced[0].expr, body=el, orelse=er))
        return Sym(tv, None)

    def _field_get(self, o: Obj, sym, e):
        try:
            return o.fields[sym]
        except KeyError:
            key = o._lookup(sym)
            if key is None:
                raise self.error(f"{o!r} has no field {sym.name}", e)
            return o.fields[key]

    def _member(self, e, fr: Frame):
        base_t = e.value.type.canonicalType
        m = e.member
        if base_t.isClass:
            if m.kind == SymK.ClassProperty and _is_static_prop(m):
                return self._static(m)
            if m.kind == SymK.Parameter:
                return self.param_value(m, e.type)
            if m.kind == SymK.EnumValue:
                return const_to_py(m.value, e.type)
            o = self.eval(e.value, fr)
            if isinstance(o, Sym):
                if o.value is None:
                    raise self.error(f"null object access .{m.name}", e)
                v = self._field_get(o.value, m, e)
                return Sym(unwrap(v), None if o.expr is None else
                           ir.ExprAttribute(value=o.expr, attr=m.name))
            if o is None:
                raise self.error(f"null object access .{m.name}", e)
            return self._field_get(o, m, e)
        if base_t.isUnpackedStruct or base_t.isUnpackedUnion:
            b = self.eval(e.value, fr)
            if isinstance(b, Sym):
                return Sym(unwrap(b.value[m.name]), None if b.expr is None else
                           ir.ExprAttribute(value=b.expr, attr=m.name))
            return b[m.name]
        if base_t.isIntegral:            # packed struct / union field
            v = self.eval(e.value, fr)
            if isinstance(v, Sym):
                return Sym(norm(v.value >> int(m.bitOffset), m.type), None if v.expr is None
                           else ir.ExprAttribute(value=v.expr, attr=m.name))
            return norm(v >> int(m.bitOffset), m.type)
        raise self.error(f"unsupported member access .{m.name} on {base_t}", e)

    def _elem(self, e, fr: Frame):
        t = e.value.type.canonicalType
        base = self.eval(e.value, fr)
        idx = self.eval(e.selector, fr)
        if isinstance(base, Sym) or isinstance(idx, Sym):
            r = self._elem_v(e, t, unwrap(base), unwrap(idx))
            be = base.expr if isinstance(base, Sym) else \
                ir.ExprRefUnresolved(name=_ref_name(e.value))
            ie = tr.expr_of(idx)
            return Sym(unwrap(r), None if be is None or ie is None else
                       ir.ExprSubscript(value=be, slice=ie))
        return self._elem_v(e, t, base, idx)

    def _elem_v(self, e, t, base, idx):
        if t.isAssociativeArray:
            if idx not in base:
                return default_value(t.arrayElementType)
            return base[idx]
        if t.isQueue or (t.isUnpackedArray and t.isDynamicallySizedArray):
            return base[idx] if 0 <= idx < len(base) else default_value(t.arrayElementType)
        if t.isUnpackedArray:
            i = self._fixed_index(t, idx)
            return base[i] if 0 <= i < len(base) else default_value(t.arrayElementType)
        if t.isIntegral:
            ew = width_of(e.type) or 1
            lo = self._bit_offset(t, idx, ew)
            if lo < 0 or lo >= int(t.bitWidth):
                return 0
            return norm(base >> lo, e.type)
        if t.isString:
            return ord(base[idx]) if 0 <= idx < len(base) else 0
        raise self.error(f"unsupported element select on {t}", e)

    @staticmethod
    def _fixed_index(t, idx) -> int:
        r = t.fixedRange
        left, right = int(r.left), int(r.right)
        return (idx - left) if right >= left else (left - idx)

    @staticmethod
    def _bit_offset(t, idx, ew) -> int:
        """Bit position of element *idx* of a packed vector/array type."""
        r = t.fixedRange
        left, right = int(r.left), int(r.right)
        pos = (idx - right) if left >= right else (right - idx)
        return pos * ew

    def _range_lo(self, e, t, fr) -> int:
        r = t.fixedRange
        left, right = int(r.left), int(r.right)
        a = unwrap(self.eval(e.left, fr))
        b = unwrap(self.eval(e.right, fr))
        sk = e.selectionKind
        if sk == ast.RangeSelectionKind.Simple:
            lo_idx = min(a, b) if left >= right else max(a, b)
        elif sk == ast.RangeSelectionKind.IndexedUp:
            lo_idx = a if left >= right else a + b - 1
        else:
            lo_idx = a - b + 1 if left >= right else a
        ew = (width_of(t) or 1) // (abs(left - right) + 1)
        return self._bit_offset(t, lo_idx, ew)

    def _range_list(self, e, fr):
        base = unwrap(self.eval(e.value, fr))
        t = e.value.type.canonicalType
        a = unwrap(self.eval(e.left, fr))
        b = unwrap(self.eval(e.right, fr))
        sk = e.selectionKind
        if sk == ast.RangeSelectionKind.Simple:
            lo, hi = a, b
        elif sk == ast.RangeSelectionKind.IndexedUp:
            lo, hi = a, a + b - 1
        else:
            lo, hi = a - b + 1, a
        if t.isUnpackedArray and not t.isDynamicallySizedArray and t.hasFixedRange:
            i0, i1 = self._fixed_index(t, lo), self._fixed_index(t, hi)
            lo, hi = min(i0, i1), max(i0, i1)
        return [copy_value(x) for x in base[lo:hi + 1]]

    def _new_array(self, e, fr: Frame):
        sz = self.eval(e.sizeExpr, fr)
        n = unwrap(sz)
        et = e.type.canonicalType.arrayElementType
        out = [default_value(et) for _ in range(max(0, n))]
        if e.initExpr is not None:
            init = self.eval(e.initExpr, fr)
            for i, v in enumerate(init[:n]):
                out[i] = copy_value(v)
        if isinstance(sz, Sym):
            return SymList(out, sz if e.initExpr is None else Sym(n, None))
        return out

    def _convert(self, e, fr: Frame):
        v = self.eval(e.operand, fr)
        to_t = e.type.canonicalType
        if to_t.isVoid:
            return None
        if to_t.isIntegral or to_t.isFloating or to_t.isString:
            return norm(v, to_t)
        if to_t.isUnpackedStruct and isinstance(v, list):
            names = [m.name for m in to_t if m.kind == SymK.Field]
            return Struct(to_t, dict(zip(names, v)))
        return v

    def _unary(self, e, fr: Frame):
        op = e.op.name
        if op in ("Preincrement", "Predecrement", "Postincrement", "Postdecrement"):
            loc = self.lvalue(e.operand, fr)
            old = loc.get()
            d = 1 if "increment" in op else -1
            if isinstance(old, Sym):
                new = Sym(norm(old.value + d, e.operand.type),
                          tr.binary("Add" if d > 0 else "Subtract", old, 1))
            else:
                new = norm(old + d, e.operand.type)
            new = self._write_value(e.operand, new, fr)
            loc.set(new)
            return new if op.startswith("Pre") else old
        v = self.eval(e.operand, fr)
        if isinstance(v, Sym):
            return Sym(self._unary_v(e, op, v.value), tr.unary(op, v))
        return self._unary_v(e, op, v)

    def _unary_v(self, e, op, v):
        if op == "LogicalNot":
            return 0 if truthy(v) else 1
        if op == "Plus":
            return v
        if op == "Minus":
            return norm(-v, e.type) if isinstance(v, int) else -v
        if op == "BitwiseNot":
            return norm(~v, e.type)
        ow = width_of(e.operand.type) or 0
        uv = mask(v, ow)
        ones = (1 << ow) - 1
        if op == "BitwiseAnd":
            return int(uv == ones)
        if op == "BitwiseNand":
            return int(uv != ones)
        if op == "BitwiseOr":
            return int(uv != 0)
        if op == "BitwiseNor":
            return int(uv == 0)
        if op == "BitwiseXor":
            return bin(uv).count("1") & 1
        if op == "BitwiseXnor":
            return 1 - (bin(uv).count("1") & 1)
        raise self.error(f"unsupported unary {op}", e)

    _CMP = {"LessThan": lambda a, b: a < b, "LessThanEqual": lambda a, b: a <= b,
            "GreaterThan": lambda a, b: a > b, "GreaterThanEqual": lambda a, b: a >= b}

    def _binary(self, e, fr: Frame):
        op = e.op.name
        if op in ("LogicalAnd", "LogicalOr"):
            a = self.eval(e.left, fr)
            short = (not truthy(a)) if op == "LogicalAnd" else truthy(a)
            if isinstance(a, Sym):
                if not tr.is_pure(e.right):
                    if short:
                        return Sym(int(truthy(a)), None)
                    b = self.eval(e.right, fr)
                    return Sym(int(truthy(b)), None)
                b = self.eval(e.right, fr)
                r = (truthy(a) and truthy(b)) if op == "LogicalAnd" else (truthy(a) or truthy(b))
                return Sym(int(r), tr.binary(op, a, b))
            if short:
                return int(truthy(a))
            b = self.eval(e.right, fr)
            if isinstance(b, Sym):
                return Sym(int(truthy(b)), tr.binary(op, a, b))
            return int(truthy(b))
        a = self.eval(e.left, fr)
        b = self.eval(e.right, fr)
        if isinstance(a, Sym) or isinstance(b, Sym):
            return Sym(self._binary_v(e, op, unwrap(a), unwrap(b)), tr.binary(op, a, b))
        return self._binary_v(e, op, a, b)

    def _binary_v(self, e, op, a, b):
        if op in ("Equality", "CaseEquality", "WildcardEquality"):
            return int(self._eq(a, b))
        if op in ("Inequality", "CaseInequality", "WildcardInequality"):
            return int(not self._eq(a, b))
        if op == "LogicalImplication":
            return int((not truthy(a)) or truthy(b))
        if op == "LogicalEquivalence":
            return int(truthy(a) == truthy(b))
        cmp = self._CMP.get(op)
        if cmp is not None:
            return int(cmp(a, b))
        if isinstance(a, float) or isinstance(b, float):
            if op == "Add":
                return a + b
            if op == "Subtract":
                return a - b
            if op == "Multiply":
                return a * b
            if op == "Divide":
                return a / b if b else 0.0
            if op == "Power":
                return a ** b
            raise self.error(f"unsupported real operator {op}", e)
        lt = e.left.type.canonicalType
        lw = width_of(lt) or 0
        if op == "Add":
            r = a + b
        elif op == "Subtract":
            r = a - b
        elif op == "Multiply":
            r = a * b
        elif op == "Divide":
            if not b:
                return 0
            q = abs(a) // abs(b)
            r = q if (a >= 0) == (b >= 0) else -q
        elif op == "Mod":
            if not b:
                return 0
            r = abs(a) % abs(b)
            r = r if a >= 0 else -r
        elif op == "BinaryAnd":
            r = a & b
        elif op == "BinaryOr":
            r = a | b
        elif op == "BinaryXor":
            r = a ^ b
        elif op == "BinaryXnor":
            r = ~(a ^ b)
        elif op in ("LogicalShiftLeft", "ArithmeticShiftLeft"):
            r = a << b
        elif op == "LogicalShiftRight":
            r = mask(a, lw) >> b
        elif op == "ArithmeticShiftRight":
            r = a >> b if lt.isSigned else mask(a, lw) >> b
        elif op == "Power":
            if b < 0:
                r = 0 if abs(a) != 1 else (a if b % 2 else 1)
            else:
                r = a ** b
        else:
            raise self.error(f"unsupported binary {op}", e)
        return norm(r, e.type)

    @staticmethod
    def _eq(a, b) -> bool:
        if isinstance(a, Obj) or isinstance(b, Obj):
            return a is b
        return a == b

    def _concat(self, e, fr: Frame):
        t = e.type.canonicalType
        vals = [self.eval(x, fr) for x in e.operands]
        if t.isQueue or t.isUnpackedArray:
            out = []
            for v in vals:
                out += [copy_value(y) for y in v] if isinstance(v, list) else [copy_value(v)]
            return out
        traced = any(isinstance(v, Sym) for v in vals)
        vals = [unwrap(v) for v in vals]
        if t.isString:
            r = "".join(v if isinstance(v, str) else int_to_str(v or 0) for v in vals)
        else:
            r = 0
            for x, v in zip(e.operands, vals):
                w = width_of(x.type) or 0
                r = (r << w) | mask(v, w)
            r = norm(r, t)
        return Sym(r, None) if traced else r

    def _pattern(self, e, fr: Frame):
        t = e.type.canonicalType
        if e.kind == EK.ReplicatedAssignmentPattern:
            n = self.eval(e.count, fr)
            one = [self.eval(x, fr) for x in e.elements]
            elems = [copy_value(v) for _ in range(n) for v in one]
            srcs = list(e.elements) * n
        else:
            srcs = list(e.elements)
            elems = [copy_value(self.eval(x, fr)) for x in srcs]
        if t.isIntegral:
            out = 0
            for x, v in zip(srcs, elems):
                w = width_of(x.type) or 0
                out = (out << w) | mask(v, w)
            return norm(out, t)
        if t.isUnpackedStruct:
            fields = [m for m in t if m.kind == SymK.Field]
            return Struct(t, {f.name: norm(v, f.type) for f, v in zip(fields, elems)})
        if t.isArray:
            et = t.arrayElementType
            return [norm(v, et) for v in elems]
        return elems

    def _assign_expr(self, e, fr: Frame):
        if e.isNonBlocking:
            raise self.error("nonblocking assignment is not supported", e)
        loc = self.lvalue(e.left, fr)
        saved = self._lvref
        if e.isCompound:
            self._lvref = loc.get()
        try:
            v = self.eval(e.right, fr)
        finally:
            self._lvref = saved
        v = norm(copy_value(v), e.left.type)
        v = self._write_value(e.left, v, fr)
        loc.set(v)
        return v

    def _write_value(self, lhs, v, fr: Frame):
        """The value to store by a write to *lhs*: tainted if a traced
        control frame is active and *lhs* is not declared inside it."""
        if not self.ctl or tr.is_handle(v):
            return v
        if self._lv_root_inner(lhs, fr):
            return v
        return tr.taint(v)

    def _lv_root_inner(self, lhs, fr: Frame) -> bool:
        k = lhs.kind
        while k in (EK.MemberAccess, EK.ElementSelect, EK.RangeSelect, EK.Conversion):
            if k == EK.MemberAccess and lhs.value.type.canonicalType.isClass:
                return False                      # a class property: outside
            lhs = lhs.operand if k == EK.Conversion else lhs.value
            k = lhs.kind
        if k == EK.Assignment:
            return self._lv_root_inner(lhs.left, fr)
        if k != EK.NamedValue:
            return False
        sym = lhs.symbol
        if isinstance(fr.locals.get(sym), _RefBinding):
            return False
        return sym in self.ctl[-1].decls

    # ------------------------------------------------------------- l-values
    def lvalue(self, lhs, fr: Frame) -> Ref:
        """The storage location an l-value expression denotes."""
        k = lhs.kind
        if k == EK.NamedValue or k == EK.HierarchicalValue:
            sym = lhs.symbol
            sk = sym.kind
            t = sym.type
            if sk in (SymK.Variable, SymK.FormalArgument, SymK.Iterator):
                if sym in fr.locals:
                    v = fr.locals[sym]
                    if isinstance(v, _RefBinding):
                        return v.loc
                    return _FrameLoc(fr, sym, t)
                if sym.lifetime == _STATIC:
                    self._static(sym)
                    return _KeyLoc(self.statics, sym, t)
                return _FrameLoc(fr, sym, t)
            if sk == SymK.ClassProperty:
                if _is_static_prop(sym):
                    self._static(sym)
                    return _KeyLoc(self.statics, sym, t)
                if fr.this is None:
                    raise self.error(f"property {sym.name} without an object", lhs)
                return self._field_loc(fr.this, sym, lhs)
            raise self.error(f"cannot assign {sym.name} ({sk.name})", lhs)
        if k == EK.MemberAccess:
            base_t = lhs.value.type.canonicalType
            m = lhs.member
            if base_t.isClass:
                if _is_static_prop(m):
                    self._static(m)
                    return _KeyLoc(self.statics, m, m.type)
                o = unwrap(self.eval(lhs.value, fr))
                if o is None:
                    raise self.error(f"null object write .{m.name}", lhs)
                return self._field_loc(o, m, lhs)
            if base_t.isUnpackedStruct or base_t.isUnpackedUnion:
                return _KeyLoc(self.lvalue(lhs.value, fr).get(), m.name, m.type)
            if base_t.isIntegral:
                return _BitsLoc(self.lvalue(lhs.value, fr), int(m.bitOffset),
                                width_of(m.type) or 1, m.type)
        if k == EK.ElementSelect:
            t = lhs.value.type.canonicalType
            idx = unwrap(self.eval(lhs.selector, fr))
            if t.isAssociativeArray:
                return _KeyLoc(self.lvalue(lhs.value, fr).get(), idx, t.arrayElementType)
            if t.isQueue or t.isUnpackedArray:
                arr = self.lvalue(lhs.value, fr).get()
                et = t.arrayElementType
                i = idx if (t.isQueue or t.isDynamicallySizedArray) else self._fixed_index(t, idx)
                if 0 <= i < len(arr):
                    return _KeyLoc(arr, i, et)
                if t.isQueue and i == len(arr):
                    arr.append(default_value(et))
                    return _KeyLoc(arr, i, et)
                return _NullLoc(default_value(et))
            if t.isIntegral:
                ew = width_of(lhs.type) or 1
                lo = self._bit_offset(t, idx, ew)
                if lo < 0 or lo >= int(t.bitWidth):
                    return _NullLoc(0)
                return _BitsLoc(self.lvalue(lhs.value, fr), lo, ew, lhs.type)
        if k == EK.RangeSelect:
            t = lhs.value.type.canonicalType
            if t.isIntegral:
                w = width_of(lhs.type) or 1
                return _BitsLoc(self.lvalue(lhs.value, fr), self._range_lo(lhs, t, fr),
                                w, lhs.type)
        if k == EK.Concatenation:
            return _ConcatLoc([(self.lvalue(x, fr), width_of(x.type) or 0)
                               for x in lhs.operands])
        if k == EK.Conversion:
            return self.lvalue(lhs.operand, fr)
        if k == EK.Assignment:              # an output-argument wrapper
            return self.lvalue(lhs.left, fr)
        raise self.error(f"unsupported assignment target {k.name}", lhs)

    def _field_loc(self, o: Obj, sym, node) -> Ref:
        key = sym if sym in o.fields else o._lookup(sym)
        if key is None:
            raise self.error(f"{o!r} has no field {sym.name}", node)
        return _KeyLoc(o.fields, key, sym.type)

    def assign(self, lhs, fr: Frame, v) -> None:
        self.lvalue(lhs, fr).set(v)

    # ------------------------------------------------------------ system calls
    _MSG = {"$display": "display", "$displayb": "display", "$displayh": "display",
            "$displayo": "display", "$write": "write", "$info": "info",
            "$warning": "warning", "$error": "error", "$fatal": "fatal"}

    def _system_call(self, e, fr: Frame):
        name = e.subroutineName
        args = list(e.arguments)
        kind = self._MSG.get(name)
        if kind is not None:
            fargs = args[1:] if name == "$fatal" else args
            text = self._fmt_args(fargs, fr) if fargs else ""
            self.intrinsics.message(self, kind, text)
            if kind == "fatal":
                raise InterpFatal(f"$fatal: {text}", self._sv_stack())
            return None
        if name in ("$fdisplay", "$fwrite", "$fclose", "$fflush", "$monitor", "$strobe"):
            return 0
        if name in ("$sformatf", "$psprintf"):
            return self._fmt_traced(args, fr)
        if name == "$sformat":
            self.assign(args[0], fr, self._fmt_args(args[1:], fr))
            return None
        if name in ("$test$plusargs", "$value$plusargs", "$fopen", "$isunknown",
                    "$time", "$stime", "$realtime"):
            return 0
        if name == "$bits":
            w = int(args[0].type.bitWidth)
            a0 = args[0]
            if a0.kind in (EK.DataType, EK.TypeReference):
                leaf = self.intrinsics.symbolic_type(self, a0.type)
                if leaf is not None:
                    return Sym(w, ir.ExprCall(func=ir.ExprRefUnresolved(name="bits"),
                                              args=[leaf]))
            return w
        if name == "$clog2":
            v = self.eval(args[0], fr)
            r = max(0, (unwrap(v) - 1).bit_length())
            return Sym(r, tr.call("clog2", [v])) if isinstance(v, Sym) else r
        if name in ("$signed", "$unsigned"):
            return norm(self.eval(args[0], fr), e.type)
        if name == "$cast":
            dst = args[0].left if args[0].kind == EK.Assignment else args[0]
            src = self.eval(args[1], fr)
            dst_t = dst.type.canonicalType
            ok = 1
            if isinstance(unwrap(src), Obj):
                ok = int(bool(dst_t.isAssignmentCompatible(unwrap(src).cls)))
            elif src is not None:
                src = norm(src, dst_t)
            if ok:
                self.assign(dst, fr, src)
            elif e.type.isVoid:
                raise self.error(f"$cast of {src!r} to {dst_t} failed", e)
            return ok
        if name == "$typename":
            return str(args[0].type)
        if name in ("$size", "$high", "$low", "$left", "$right", "$increment",
                    "$dimensions", "$unpacked_dimensions"):
            return self._array_query(name, args, fr, e)
        if args:
            return self._builtin_method(name, args, fr, e)
        raise self.error(f"unsupported system call {name}", e)

    def _array_query(self, name, args, fr, e):
        t = args[0].type.canonicalType
        if t.hasFixedRange:
            r = t.fixedRange
            left, right = int(r.left), int(r.right)
        else:
            v = unwrap(self.eval(args[0], fr))
            n = len(v) if v is not None else 0
            left, right = 0, n - 1
            if isinstance(v, SymList) and v.size is not None:
                if name == "$size":
                    return Sym(n, v.size.expr)
                r = {"$high": n - 1, "$right": n - 1}.get(name)
                if r is not None:
                    return Sym(r, tr.binary("Subtract", v.size, 1))
        return {"$size": abs(left - right) + 1, "$high": max(left, right),
                "$low": min(left, right), "$left": left, "$right": right,
                "$increment": 1 if left >= right else -1}.get(name, 1)

    def _fmt_traced(self, args, fr):
        """``$sformatf``: a string; traced (``ExprCall("$sformatf", fmt,
        args)``) if an argument is."""
        if not args:
            return ""
        raw = [self.eval(a, fr) for a in args]
        text = self._fmt_values(args, [unwrap(v) for v in raw], fr)
        if any(isinstance(v, Sym) for v in raw):
            return Sym(text, tr.call("$sformatf", raw))
        return text

    def _fmt_args(self, args, fr) -> str:
        if not args:
            return ""
        return self._fmt_values(args, [unwrap(self.eval(a, fr)) for a in args], fr)

    def _fmt_values(self, args, raw, fr) -> str:
        fmt = raw[0]
        vals = [(v, a.type) for v, a in zip(raw[1:], args[1:])]
        scope = ""
        if fr.sub is not None:
            scope = self._label(fr.sub)
        return fmt_sv(fmt if isinstance(fmt, str) else int_to_str(fmt), vals, scope)

    def _builtin_method(self, name, args, fr, e):
        recv_t = args[0].type.canonicalType
        recv = self.eval(args[0], fr)
        rest = args[1:]
        if isinstance(recv, Sym):
            r = self._builtin_method_v(name, recv.value, recv_t, rest, fr, e)
            return r if r is None else Sym(unwrap(r), None)
        if isinstance(recv, SymList) and recv.size is not None:
            if name == "size":
                return Sym(len(recv), recv.size.expr)
            r = self._builtin_method_v(name, recv, recv_t, rest, fr, e)
            if name in ("push_back", "push_front", "pop_front", "pop_back", "delete",
                        "insert"):
                recv.size = Sym(len(recv), None)     # the size no longer follows
            return r
        return self._builtin_method_v(name, recv, recv_t, rest, fr, e)

    def _builtin_method_v(self, name, recv, recv_t, rest, fr, e):
        if isinstance(recv, list):
            et = recv_t.arrayElementType if recv_t.isArray else None
            if name == "size":
                return len(recv)
            if name == "push_back":
                recv.append(norm(copy_value(self.eval(rest[0], fr)), et))
                return None
            if name == "push_front":
                recv.insert(0, norm(copy_value(self.eval(rest[0], fr)), et))
                return None
            if name == "pop_front":
                return recv.pop(0) if recv else default_value(et)
            if name == "pop_back":
                return recv.pop() if recv else default_value(et)
            if name == "delete":
                if rest:
                    i = self.eval(rest[0], fr)
                    if 0 <= i < len(recv):
                        recv.pop(i)
                else:
                    recv.clear()
                return None
            if name == "insert":
                recv.insert(self.eval(rest[0], fr), norm(copy_value(self.eval(rest[1], fr)), et))
                return None
            if name in ("sum", "product", "min", "max", "reverse", "sort", "rsort"):
                if e.subroutine is not None and getattr(e, "iterOrThis", None) is not None:
                    raise self.error(f"array method {name} with 'with' is not supported", e)
                if name == "sum":
                    return norm(sum(recv), e.type)
                if name == "product":
                    p = 1
                    for x in recv:
                        p *= x
                    return norm(p, e.type)
                if name == "min":
                    return [min(recv)] if recv else []
                if name == "max":
                    return [max(recv)] if recv else []
                if name == "reverse":
                    recv.reverse()
                elif name == "sort":
                    recv.sort()
                else:
                    recv.sort(reverse=True)
                return None
        if isinstance(recv, dict):
            if name == "exists":
                return int(self.eval(rest[0], fr) in recv)
            if name in ("num", "size"):
                return len(recv)
            if name == "delete":
                if rest:
                    recv.pop(self.eval(rest[0], fr), None)
                else:
                    recv.clear()
                return None
            if name in ("first", "last", "next", "prev"):
                keys = sorted(recv.keys())
                if not keys:
                    return 0
                if name in ("first", "last"):
                    self.assign(rest[0], fr, keys[0] if name == "first" else keys[-1])
                    return 1
                cur = self.eval(rest[0], fr)
                cand = [x for x in keys if (x > cur if name == "next" else x < cur)]
                if not cand:
                    return 0
                self.assign(rest[0], fr, cand[0] if name == "next" else cand[-1])
                return 1
        if isinstance(recv, str):
            if name == "len":
                return len(recv)
            if name == "substr":
                i, j = self.eval(rest[0], fr), self.eval(rest[1], fr)
                return recv[i:j + 1] if 0 <= i <= j < len(recv) else ""
            if name == "toupper":
                return recv.upper()
            if name == "tolower":
                return recv.lower()
            if name in ("compare", "icompare"):
                o = self.eval(rest[0], fr)
                a, b = (recv, o) if name == "compare" else (recv.lower(), o.lower())
                return (a > b) - (a < b)
            if name == "getc":
                i = self.eval(rest[0], fr)
                return ord(recv[i]) if 0 <= i < len(recv) else 0
            if name in ("atoi", "atohex", "atobin", "atooct"):
                base = {"atoi": 10, "atohex": 16, "atobin": 2, "atooct": 8}[name]
                digits = ""
                for ch in recv.replace("_", ""):
                    try:
                        int(ch, base)
                    except ValueError:
                        if not (ch == "-" and not digits and base == 10):
                            break
                    digits += ch
                try:
                    return norm(int(digits, base), e.type)
                except ValueError:
                    return 0
            if name == "itoa":
                self.assign(args[0], fr, str(self.eval(rest[0], fr)))
                return None
        if recv_t.isEnum:
            vals = [(m.name, const_to_py(m.value, recv_t))
                    for m in recv_t if m.kind == SymK.EnumValue]
            if name == "name":
                for n, v in vals:
                    if v == recv:
                        return n
                return ""
            if name == "num":
                return len(vals)
            if name in ("first", "last"):
                return vals[0][1] if name == "first" else vals[-1][1]
            if name in ("next", "prev"):
                step = self.eval(rest[0], fr) if rest else 1
                idx = next((i for i, (_, v) in enumerate(vals) if v == recv), 0)
                idx = (idx + (step if name == "next" else -step)) % len(vals)
                return vals[idx][1]
        raise self.error(f"unsupported built-in method {name} on {recv_t}", e)
