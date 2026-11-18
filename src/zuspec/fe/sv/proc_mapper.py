"""Map a fw-hdl component class's processes to process-block IR ("Z8").

The blocks of a class (spec ``semantics/processes.md``, "Process kinds"):

- ``step()`` of a class implementing ``fw_step_if`` and each
  `` `fw_step(name) `` block: a ``ProcKind.STEP`` block;
- ``comb()`` of a class implementing ``fw_comb_if`` and each
  `` `fw_comb(name) `` block: a ``ProcKind.COMB`` block;
- ``reset_state()`` (``fw_reset_state_if``) is split over the step blocks
  by what each of its statements writes (``reset_body``).

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
- a path written by two blocks, or by a block and ``run()``:
  ``FW-E-WRITE-MULTI``;
- with ``emit=True`` (a back end lowering the class to RTL): a class
  implementing ``fw_run_if`` is ``FW-E-EMIT-RUN``; ``fw_time`` or a
  waiting domain operation in a block, or a function it calls, is
  ``FW-E-EMIT-TIME``.

Clock and reset domains are not set here: they come from the domains the
written interfaces resolve to, which the caller knows. ``run()`` has no IR.
"""
from typing import Dict, List, Optional

import pyslang

from zuspec.ir.core.domain import DomainRef
from zuspec.ir.core.proc_block import ExprInReset, ProcBlock, ProcKind
from zuspec.ir.core.stmt import StmtAssign
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
    """The class a subroutine is declared in, by name without parameters."""
    parts = str(getattr(sub, "hierarchicalPath", "")).split("::")
    return parts[-2].split("#")[0] if len(parts) > 1 else ""


def _chain(cls) -> List[str]:
    names = []
    c = cls
    while c is not None:
        names.append(c.name)
        c = _base(c)
    return names


class _Scan:
    """What a function body does, with the component functions it calls:
    the paths it writes, unattributable writes, time operations."""

    def __init__(self, cls):
        self.cls = cls
        self.chain = _chain(cls)
        self.writes: List[List[str]] = []
        self.bad: List[str] = []
        self.time: List[str] = []
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
            if k == _EK.Assignment:
                left = n.left
                p = _target_path(left)
                if p is not None:
                    self._w(p)
                elif left.kind == _EK.MemberAccess:
                    self.bad.append(str(left.syntax).strip())
            elif k == _EK.Call:
                s = n.subroutine
                name = getattr(s, "name", "")
                owner = _owner(s)
                tc = getattr(n, "thisClass", None)
                if name == "write" and tc is not None and owner.startswith("fw_"):
                    p = _path(tc)
                    if p is not None:
                        self._w(p)
                    else:
                        self.bad.append(str(n.syntax).strip())
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


class _ProcExprMapper(ExprMapper):
    """``read()`` as the signal, ``in_reset`` as ``ExprInReset``; signal
    and field paths as attribute chains on the self field."""

    def __init__(self, config, reporter, owner: "ProcMapper"):
        super().__init__(config, reporter)
        self._owner = owner

    def _map_call(self, sv_expr):
        sub = sv_expr.subroutine
        name = getattr(sub, "name", "")
        tc = getattr(sv_expr, "thisClass", None)
        if name == "read" and tc is not None and _path(tc) is not None \
                and not sv_expr.arguments:
            return self.map_expression(tc)
        if name == "write" and tc is not None and _path(tc) is not None:
            # Only as a statement (ProcStmtMapper); elsewhere an error.
            self.error_reporter.error(
                f"'{sv_expr.syntax}': a signal write is a statement")
            return None
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
        p = _path(sv_expr.left)
        if p is not None:
            self._owner._wrote(p)
        return super()._map_assignment(sv_expr)


class ProcMapper:
    """Maps the step/comb blocks and ``reset_state()`` of a class.

    :param fields: field name -> index of the component IR's fields (by
        default :func:`field_index` of the class)
    """

    def __init__(self, config: Optional[SVToZuspecConfig] = None,
                 reporter: Optional[ErrorReporter] = None,
                 fields: Optional[Dict[str, int]] = None,
                 emit: bool = False):
        self.emit = emit
        self.config = config or SVToZuspecConfig()
        self.reporter = reporter or ErrorReporter()
        self._fields = fields
        self._em = _ProcExprMapper(self.config, self.reporter, self)
        self._sm = _ProcStmtMapper(self.config, self.reporter, self._em, self)
        self._writes: List[List[str]] = []

    def _wrote(self, p: List[str]) -> None:
        if p not in self._writes:
            self._writes.append(list(p))

    def _body(self, sub):
        self._writes = []
        body = self._sm.map_statements(sub.body) if sub.body is not None else []
        return body, self._writes

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
                                    writes=writes))

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
        if RUN_IF in ifs and self.emit:
            self.reporter.error(
                f"FW-E-EMIT-RUN: {cls.name}: implements fw_run_if; run() is "
                f"not lowered to RTL")
        self._write_multi(cls, blocks, ifs)
        if RESET_STATE_IF in ifs:
            self._reset_state(cls, blocks)
        return blocks

    def _write_multi(self, cls, blocks: List[ProcBlock], ifs: List[str]) -> None:
        """FW-E-WRITE-MULTI: a path (or a path and one under it) written by
        two blocks, or by a block and run()."""
        writers = [(b.name, b.writes) for b in blocks]
        if RUN_IF in ifs:
            run = cls.find("run")
            if run is not None and run.kind == _SK.Subroutine:
                writers.append(("run()", _Scan(cls).sub(run).writes))
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
