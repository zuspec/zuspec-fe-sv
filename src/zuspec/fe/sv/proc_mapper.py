"""Map a fw-hdl component class's processes to process-block IR ("Z8").

The blocks of a class (spec ``semantics/processes.md``, "Process kinds"):

- ``step()`` of a class implementing ``fw_step_if`` and each
  `` `fw_step(name) `` block: a ``ProcKind.STEP`` block;
- ``comb()`` of a class implementing ``fw_comb_if`` and each
  `` `fw_comb(name) `` block: a ``ProcKind.COMB`` block;
- ``reset_state()`` (``fw_reset_state_if``) is split over the step blocks
  by what each of its statements writes (``reset_body``);
- ``run()`` of a class implementing ``fw_run_if`` (with the component
  functions and tasks it calls): a ``ProcKind.TASK`` block with the paths
  it ``reads`` and ``writes`` (its crossing ports, checked by
  zuspec-synth's ``check_xing``). Each other task of a user class that
  calls a task-to-sync crossing's method (an export's ``access()``, say)
  is a ``ProcKind.TASK`` block too, named for the task. A task block's
  ``body`` and ``args`` are the task's code (ProcBlock): blocking calls
  in ``ExprAwait``, and ``i = p.get_if(); i.m(...)`` on API port ``p`` as
  ``p.m(...)`` on the endpoint (``ExprHierarchical([p])``), ``i`` dropped.
  Code it cannot map leaves the body empty, with ``FW-W-TASK-MAP``.

A call of a crossing's method (``fw_to_sync``/``fw_from_sync``, on a path
from the component) is recorded in the block's ``xing_uses``, for
zuspec-synth's ``check_sync_xing``; in a step or comb body it is
``ExprCall(ExprAttribute(<crossing field>, method), args)`` (an output
argument as its target), which zuspec-synth's crossing lowering rewrites.

In a body, ``s.write(v)`` is an assignment to ``s`` and ``s.read()`` is
``s`` itself: a step block's assignments are registers, a comb block's
wires. A plain assignment to a component field is a write of that field.
``fw_domain #(fw_reset_kind)::in_reset(x)`` is ``ExprInReset`` on the reset
domain of ``x``. ``ProcBlock.writes`` lists the paths each block writes,
including the writes of the component functions it calls.

Checks ("Register or wire", "Lowering"), reported with the code first in
the message:

- a write whose target is not a path from the component (a signal handle
  in a local, say): ``FW-E-WRITE-PATH``;
- a path written by two step or comb blocks: ``FW-E-WRITE-MULTI``. A
  path written by a block and ``run()`` is ``check_xing``'s: a comb block
  and task code is the error there, a step block and task code is the
  port's default (C5);
- with ``emit=True`` (a back end lowering the class to RTL): a class
  implementing ``fw_run_if`` whose ``run()`` does more than hand off
  (it writes or reads component state or pins, or uses time; crossing
  methods and API-port calls are hand-off) is ``FW-E-EMIT-RUN``; ``fw_time`` or a
  waiting domain operation in a block, or a function it calls, is
  ``FW-E-EMIT-TIME``.

Clock and reset domains are not set here: they come from the domains the
written interfaces resolve to, which the caller knows.
"""
from typing import Dict, List, Optional

import pyslang

from zuspec.ir.core.domain import DomainRef
from zuspec.ir.core.expr import (ExprAttribute, ExprAwait, ExprCall, ExprHierarchical,
                                 ExprHierarchicalElem)
from zuspec.ir.core.fields import SignalDirection
from zuspec.ir.core.proc_block import ExprInReset, ProcBlock, ProcKind, SyncXingUse
from zuspec.ir.core.stmt import Arg, StmtAssign
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.stmt_mapper import StmtMapper

_EK = pyslang.ast.ExpressionKind
_SK = pyslang.ast.SymbolKind

STEP_IF = "fw_step_if"
COMB_IF = "fw_comb_if"
RESET_STATE_IF = "fw_reset_state_if"
RUN_IF = "fw_run_if"

_MACRO = {"__fw_step_": ProcKind.STEP, "__fw_comb_": ProcKind.COMB}
_DOMAIN_WAIT = ("tick", "wait_reset_done", "wait_assert")
_XING = ("fw_to_sync", "fw_from_sync")
_EP = ("fw_port", "fw_export", "fw_socket")
_DIR = {pyslang.ast.ArgumentDirection.In: SignalDirection.INPUT,
        pyslang.ast.ArgumentDirection.Out: SignalDirection.OUTPUT,
        pyslang.ast.ArgumentDirection.InOut: SignalDirection.INOUT}


def _is_task(sub) -> bool:
    return getattr(sub, "subroutineKind", None) == pyslang.ast.SubroutineKind.Task


def _get_if(e) -> Optional[List[str]]:
    """The path of API endpoint ``p`` if *e* is ``p.get_if()``, else None."""
    if e is None or e.kind != _EK.Call or getattr(e.subroutine, "name", "") != "get_if" \
            or _owner(e.subroutine) not in _EP:
        return None
    tc = getattr(e, "thisClass", None)
    return _path(tc) if tc is not None else None


def _api_local(sym) -> bool:
    """True if *sym* is a local of an interface-class (API) type."""
    t = getattr(sym, "type", None)
    t = getattr(t, "canonicalType", t)
    return sym.kind == _SK.Variable and bool(getattr(t, "isInterface", False))


def _base(cls):
    b = cls.baseClass
    return b.canonicalType if b is not None and b.canonicalType.isClass else None


def implemented(cls) -> List[str]:
    """The interface classes *cls* and its bases implement, by name."""
    names = []
    c = cls
    while c is not None:
        for i in c.implementedInterfaces:
            g = getattr(i, "genericClass", None)
            n = g.name if g is not None else i.name
            if n not in names:
                names.append(n)
        c = _base(c)
    return names


def field_index(cls) -> Dict[str, int]:
    """Class properties of *cls* and its bases (base first), name -> index;
    the macros' block instances are left out."""
    chain = []
    c = cls
    while c is not None:
        chain.insert(0, c)
        c = _base(c)
    idx: Dict[str, int] = {}
    for c in chain:
        for m in c:
            if m.kind == _SK.ClassProperty and not m.name.startswith("__fw") \
                    and m.name not in idx:
                idx[m.name] = len(idx)
    return idx


def _path(e) -> Optional[List[str]]:
    """The component path *e* names (``[]`` for ``this``), or None."""
    k = e.kind
    if k == _EK.NamedValue:
        if e.symbol.kind == _SK.ClassProperty:
            return [e.symbol.name]
        return [] if e.symbol.name == "this" else None
    if k == _EK.MemberAccess:
        base = _path(e.value)
        return None if base is None else base + [e.member.name]
    if k == _EK.Conversion:
        return _path(e.operand)
    return None


def _target_path(e) -> Optional[List[str]]:
    """The path an assignment to *e* writes: a select writes its whole base."""
    while e.kind in (_EK.ElementSelect, _EK.RangeSelect, _EK.Conversion):
        e = e.value if e.kind != _EK.Conversion else e.operand
    return _path(e)


def _owner(sub) -> str:
    """The class a subroutine is declared in, by name without parameters
    (which may themselves hold ``::``: ``fw_to_sync#(p::c::req_t)::take``)."""
    hp, out, depth = str(getattr(sub, "hierarchicalPath", "")), [], 0
    for ch in hp:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    parts = "".join(out).split("::")
    return parts[-2].split("#")[0] if len(parts) > 1 else ""


def _local_root(e) -> bool:
    """True if *e* is a member or element of a block's local variable."""
    while e.kind in (_EK.MemberAccess, _EK.ElementSelect, _EK.RangeSelect, _EK.Conversion):
        e = e.value if e.kind != _EK.Conversion else e.operand
    return e.kind == _EK.NamedValue and e.symbol.kind == _SK.Variable


def _chain(cls) -> List[str]:
    names = []
    c = cls
    while c is not None:
        names.append(c.name)
        c = _base(c)
    return names


class _Scan:
    """What a function body does, with the component functions it calls:
    the paths it writes and reads, unattributable writes, time operations."""

    def __init__(self, cls):
        self.cls = cls
        self.chain = _chain(cls)
        self.writes: List[List[str]] = []
        self.reads: List[List[str]] = []
        self.bad: List[str] = []
        self.time: List[str] = []
        self.xing: List[tuple] = []          # (path, method), first use first
        self._seen: List[str] = []

    def _w(self, p):
        if p not in self.writes:
            self.writes.append(list(p))

    def sub(self, sub) -> "_Scan":
        if sub is None or sub.body is None or sub.name in self._seen:
            return self
        self._seen.append(sub.name)
        calls = []

        def v(n):
            k = getattr(n, "kind", None)
            if k == pyslang.ast.StatementKind.VariableDeclaration:
                # the visitor does not enter a local's initializer
                init = getattr(n.symbol, "initializer", None)
                if init is not None:
                    init.visit(v)
            elif k == _EK.Assignment:
                left = n.left
                p = _target_path(left)
                if p is not None:
                    self._w(p)
                elif left.kind == _EK.MemberAccess and not _local_root(left):
                    self.bad.append(str(left.syntax).strip())
            elif k == _EK.Call:
                s = n.subroutine
                name = getattr(s, "name", "")
                owner = _owner(s)
                tc = getattr(n, "thisClass", None)
                if owner in _XING and tc is not None:
                    p = _path(tc)
                    if p is not None and (p, name) not in self.xing:
                        self.xing.append((p, name))
                elif name == "write" and tc is not None and owner.startswith("fw_"):
                    p = _path(tc)
                    if p is not None:
                        self._w(p)
                    else:
                        self.bad.append(str(n.syntax).strip())
                elif name == "read" and tc is not None and owner.startswith("fw_"):
                    p = _path(tc)
                    if p is not None and p not in self.reads:
                        self.reads.append(p)
                elif owner == "fw_time" or (owner == "fw_domain" and name in _DOMAIN_WAIT):
                    self.time.append(str(n.syntax).strip())
                elif tc is None and owner in self.chain:
                    calls.append(name)

        sub.body.visit(v)
        for name in calls:
            f = self.cls.find(name)
            if f is not None and f.kind == _SK.Subroutine:
                self.sub(f)
        return self


def _uses(sc: _Scan) -> List[SyncXingUse]:
    return [SyncXingUse(path=list(p), method=m) for p, m in sc.xing]


def _user_tasks(cls) -> list:
    """The tasks of class *cls* and its bases outside fw_hdl_pkg, most
    derived first, one per name."""
    out, names = [], set()
    c = cls
    while c is not None:
        for m in c:
            if m.kind == _SK.Subroutine and m.subroutineKind == pyslang.ast.SubroutineKind.Task \
                    and m.name not in names \
                    and not str(getattr(m, "hierarchicalPath", "")).startswith("fw_hdl_pkg::"):
                names.add(m.name)
                out.append(m)
        c = _base(c)
    return out


def task_writes(cls) -> List[List[str]]:
    """The paths ``run()`` of class *cls* writes, with the component
    functions and tasks it calls (``[]`` without ``fw_run_if``)."""
    if RUN_IF not in implemented(cls):
        return []
    run = cls.find("run")
    if run is None or run.kind != _SK.Subroutine:
        return []
    return _Scan(cls).sub(run).writes


class _ProcExprMapper(ExprMapper):
    """``read()`` as the signal, ``in_reset`` as ``ExprInReset``; signal
    and field paths as attribute chains on the self field."""

    def __init__(self, config, reporter, owner: "ProcMapper"):
        super().__init__(config, reporter)
        self._owner = owner

    def _args(self, sv_expr):
        args = []
        for a in sv_expr.arguments:
            if a.kind == _EK.Assignment:        # an output argument: its target
                a = a.left
            m = self.map_expression(a)
            if m is None:
                return None
            args.append(m)
        return args

    def _map_call(self, sv_expr):
        sub = sv_expr.subroutine
        name = getattr(sub, "name", "")
        tc = getattr(sv_expr, "thisClass", None)
        o = self._owner
        if o._aliases is not None:
            # Task code: a call through an API port, on the endpoint.
            ep = None
            if tc is not None:
                ep = _get_if(tc)
                if ep is None and tc.kind == _EK.NamedValue:
                    ep = o._aliases.get(tc.symbol.name)
            if ep is not None:
                args = self._args(sv_expr)
                if args is None:
                    return None
                call = ExprCall(func=ExprAttribute(value=ExprHierarchical(
                    elements=[ExprHierarchicalElem(name=n) for n in ep]), attr=name), args=args)
                return ExprAwait(value=call) if _is_task(sub) else call
            if _get_if(sv_expr) is not None:
                self.error_reporter.error(
                    f"'{sv_expr.syntax}': a port's get_if() only as a local's "
                    f"value or a call's receiver")
                return None
        if name == "read" and tc is not None and _path(tc) is not None \
                and not sv_expr.arguments:
            return self.map_expression(tc)
        if name == "write" and tc is not None and _path(tc) is not None:
            # Only as a statement (ProcStmtMapper); elsewhere an error.
            self.error_reporter.error(
                f"'{sv_expr.syntax}': a signal write is a statement")
            return None
        if _owner(sub) in _XING and tc is not None and _path(tc) is not None:
            x = self.map_expression(tc)
            args = self._args(sv_expr)
            if x is None or args is None:
                return None
            call = ExprCall(func=ExprAttribute(value=x, attr=name), args=args)
            return ExprAwait(value=call) if _is_task(sub) else call
        if name == "in_reset":
            # e.g. fw_hdl_pkg::fw_domain#(fw_reset_kind)::in_reset
            owner = str(getattr(sub, "hierarchicalPath", "")).split("::")[-2:-1]
            if owner and owner[0].split("#")[0] == "fw_domain":
                args = list(sv_expr.arguments)
                tgt = _path(args[0]) if args else []
                if tgt is None:
                    self.error_reporter.error(
                        f"'{sv_expr.syntax}': in_reset of an object that is not "
                        f"the component or one of its interfaces")
                    return None
                return ExprInReset(reset=DomainRef(kind="fw_reset_kind", target=tgt))
        return super()._map_call(sv_expr)


class _ProcStmtMapper(StmtMapper):
    """``s.write(v)`` as ``s = v``; records each written path."""

    def __init__(self, config, reporter, em, owner: "ProcMapper"):
        super().__init__(config, reporter, em)
        self._owner = owner

    def _map_expression_statement(self, sv_stmt):
        e = getattr(sv_stmt, "expr", None)
        if e is not None and e.kind == _EK.Call \
                and getattr(e.subroutine, "name", "") == "write":
            tc = getattr(e, "thisClass", None)
            p = _path(tc) if tc is not None else None
            if p is not None and len(e.arguments) == 1:
                tgt = self.expr_mapper.map_expression(tc)
                val = self.expr_mapper.map_expression(e.arguments[0])
                if tgt is None or val is None:
                    return None
                self._owner._wrote(p)
                return StmtAssign(targets=[tgt], value=val)
        return super()._map_expression_statement(sv_stmt)

    def _map_assignment(self, sv_expr):
        al = self._owner._aliases
        if al is not None and sv_expr.left.kind == _EK.NamedValue \
                and _api_local(sv_expr.left.symbol):
            ep = _get_if(sv_expr.right)
            if ep is None:
                self.error_reporter.error(
                    f"'{sv_expr.syntax}': an API local takes only a port's get_if()")
                return None
            al[sv_expr.left.symbol.name] = ep
            return None
        p = _path(sv_expr.left)
        if p is not None:
            self._owner._wrote(p)
        return super()._map_assignment(sv_expr)

    def _map_var_decl(self, sv_stmt):
        al = self._owner._aliases
        sym = sv_stmt.symbol
        if al is not None and _api_local(sym):
            # i = p.get_if(): calls on i are calls on p.
            if sym.initializer is not None:
                ep = _get_if(sym.initializer)
                if ep is None:
                    self.error_reporter.error(
                        f"local '{sym.name}': an API local takes only a port's get_if()")
                    return None
                al[sym.name] = ep
            return None
        return super()._map_var_decl(sv_stmt)


class ProcMapper:
    """Maps the step/comb blocks and ``reset_state()`` of a class.

    :param fields: field name -> index of the component IR's fields (by
        default :func:`field_index` of the class)
    :param type_fn: maps the slang type of a block's local to an IR type
        (without it, a local is an error)
    """

    def __init__(self, config: Optional[SVToZuspecConfig] = None,
                 reporter: Optional[ErrorReporter] = None,
                 fields: Optional[Dict[str, int]] = None,
                 emit: bool = False, type_fn=None):
        self.emit = emit
        self.config = config or SVToZuspecConfig()
        self.reporter = reporter or ErrorReporter()
        self._fields = fields
        self._em = _ProcExprMapper(self.config, self.reporter, self)
        self._sm = _ProcStmtMapper(self.config, self.reporter, self._em, self)
        self._sm.type_fn = type_fn
        self._writes: List[List[str]] = []
        self._aliases: Optional[Dict[str, List[str]]] = None   # task code: API locals

    def _wrote(self, p: List[str]) -> None:
        if p not in self._writes:
            self._writes.append(list(p))

    def _body(self, sub):
        self._writes = []
        body = self._sm.map_statements(sub.body) if sub.body is not None else []
        return body, self._writes

    def _task(self, cls, sub):
        """The body and arguments of task *sub* (see the module); an empty
        body, with a warning, when its code does not map."""
        rep = ErrorReporter()
        saved = (self._em.error_reporter, self._sm.error_reporter, self._writes)
        self._em.error_reporter = self._sm.error_reporter = rep
        self._aliases, self._writes = {}, []
        try:
            body = self._sm.map_statements(sub.body) if sub.body is not None else []
            args = []
            for a in sub.arguments:
                dt = self._sm.type_fn(a.type) if self._sm.type_fn is not None else None
                if dt is None:
                    rep.error(f"argument '{a.name}': unsupported type {a.type}")
                args.append(Arg(arg=a.name, datatype=dt, direction=_DIR.get(a.direction)))
        except Exception as e:      # an unmapped construct: no body
            rep.error(str(e))
            body, args = [], []
        finally:
            self._em.error_reporter, self._sm.error_reporter, self._writes = saved
            self._aliases = None
        if rep.has_errors():
            self.reporter.warning(
                f"FW-W-TASK-MAP: {cls.name}.{sub.name}: task code not mapped to IR "
                f"({rep.errors[0].message})")
            return [], []
        return body, args

    def map(self, cls) -> List[ProcBlock]:
        """The process blocks of class *cls* (a pyslang ``ClassType``)."""
        self._em.set_field_context(self._fields if self._fields is not None
                                   else field_index(cls))
        ifs = implemented(cls)
        blocks: List[ProcBlock] = []

        def add(name, kind, func):
            sub = cls.find(func)
            if sub is None or sub.kind != _SK.Subroutine:
                self.reporter.error(f"{cls.name}: no function '{func}' for block '{name}'")
                return
            body, writes = self._body(sub)
            sc = _Scan(cls).sub(sub)
            for p in sc.writes:
                if p not in writes:
                    writes.append(p)
            for b in sc.bad:
                self.reporter.error(
                    f"FW-E-WRITE-PATH: {cls.name}.{name}: '{b}' writes a target "
                    f"that is not a path from the component")
            if self.emit and sc.time:
                self.reporter.error(
                    f"FW-E-EMIT-TIME: {cls.name}: block '{name}' uses "
                    f"'{sc.time[0]}', which is not lowered to RTL")
            blocks.append(ProcBlock(name=name, kind=kind, func=func, body=body,
                                    writes=writes, xing_uses=_uses(sc)))

        if STEP_IF in ifs:
            add("step", ProcKind.STEP, "step")
        if COMB_IF in ifs:
            add("comb", ProcKind.COMB, "comb")
        c = cls
        while c is not None:
            for m in c:
                if m.kind != _SK.ClassProperty:
                    continue
                for pre, kind in _MACRO.items():
                    if m.name.startswith(pre) and m.name.endswith("_i"):
                        fn = m.name[len(pre):-2]
                        if not any(b.name == fn for b in blocks):
                            add(fn, kind, fn)
            c = _base(c)
        if RUN_IF in ifs:
            run = cls.find("run")
            sc = _Scan(cls).sub(run if run is not None and run.kind == _SK.Subroutine
                                else None)
            # A run() that only hands off (crossing methods and the
            # component's API ports; no state, pins or time) is dropped
            # from the RTL like other task code: it is the bridge's.
            if self.emit and (sc.writes or sc.reads or sc.time or sc.bad
                              or run is None or run.kind != _SK.Subroutine):
                self.reporter.error(
                    f"FW-E-EMIT-RUN: {cls.name}: implements fw_run_if; run() is "
                    f"not lowered to RTL")
            if run is not None and run.kind == _SK.Subroutine:
                body, args = self._task(cls, run)
                blocks.append(ProcBlock(name="run", kind=ProcKind.TASK, func="run",
                                        body=body, args=args,
                                        writes=sc.writes, reads=sc.reads,
                                        xing_uses=_uses(sc)))
        for t in _user_tasks(cls):
            if any(b.func == t.name for b in blocks):
                continue
            sc = _Scan(cls).sub(t)
            if sc.xing:
                body, args = self._task(cls, t)
                blocks.append(ProcBlock(name=t.name, kind=ProcKind.TASK, func=t.name,
                                        body=body, args=args,
                                        writes=sc.writes, reads=sc.reads,
                                        xing_uses=_uses(sc)))
        self._write_multi(cls, blocks)
        if RESET_STATE_IF in ifs:
            self._reset_state(cls, blocks)
        return blocks

    def _write_multi(self, cls, blocks: List[ProcBlock]) -> None:
        """FW-E-WRITE-MULTI: a path (or a path and one under it) written by
        two step or comb blocks (task blocks: ``check_xing``)."""
        writers = [(b.name, b.writes) for b in blocks if b.kind != ProcKind.TASK]
        seen = set()
        for i, (na, wa) in enumerate(writers):
            for nb, wb in writers[i + 1:]:
                for pa in wa:
                    for pb in wb:
                        n = min(len(pa), len(pb))
                        if pa[:n] != pb[:n]:
                            continue
                        key = ".".join(pa if len(pa) <= len(pb) else pb)
                        if key in seen:
                            continue
                        seen.add(key)
                        self.reporter.error(
                            f"FW-E-WRITE-MULTI: {cls.name}.{key}: written by "
                            f"'{na}' and '{nb}'")

    def _reset_state(self, cls, blocks: List[ProcBlock]) -> None:
        sub = cls.find("reset_state")
        if sub is None or sub.body is None:
            return
        steps = [b for b in blocks if b.kind == ProcKind.STEP]
        stmts = sub.body
        items = list(stmts.list) if stmts.kind == pyslang.ast.StatementKind.List \
            else [stmts]
        if items and items[0].kind == pyslang.ast.StatementKind.Block:
            items = [items[0].body]
        for s in items:
            if s.kind == pyslang.ast.StatementKind.List:
                parts = list(s.list)
            else:
                parts = [s]
            for part in parts:
                self._writes = []
                ir = self._sm.map_statement(part)
                if ir is None:
                    continue
                ir = ir if isinstance(ir, list) else [ir]
                w = self._writes
                owner = [b for b in steps if all(p in b.writes for p in w)]
                if len(owner) != 1 and len(steps) == 1:
                    owner = steps
                if len(owner) != 1:
                    self.reporter.error(
                        f"{cls.name}: reset_state() statement '{part.syntax}' writes "
                        f"{['.'.join(p) for p in w]}, not the registers of one step block")
                    continue
                owner[0].reset_body.extend(ir)
