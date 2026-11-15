"""Integration smoke: run fw-hdl-spec's smoke_pkg classes on the spec's
declarations-only fw_hdl_pkg, driven by a tiny emulated host.

The spec's API has only ``extern`` prototypes, so the test generates stub
bodies (``extern_stub_bodies``) and splices them into fw_hdl_pkg through its
``SV_WEAVE_INCLUDE_fw_hdl_pkg`` hook. The host implements the API methods as
intrinsics (as fw-hdl-fe-sv's FeRt will) and walks the phases itself:
create the top, build top-down, connect, then extern_map each extern.

Skipped when packages/fw-hdl-spec is not checked out next to this package.
"""
import os

import pytest

from zuspec.fe.sv import class_from_typedef, compile, extern_stub_bodies
from zuspec.fe.sv.interp import NOT_HANDLED, Cell, Interpreter, Intrinsics, Obj

SPEC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                     "fw-hdl-spec"))
API = os.path.join(SPEC, "bindings", "sv")
SMOKE = os.path.join(SPEC, "tests", "sv", "smoke", "smoke_pkg.sv")

pytestmark = pytest.mark.skipif(not os.path.isfile(SMOKE),
                                reason=f"fw-hdl-spec not found at {SPEC}")


def _compile(top_text):
    files = [os.path.join(API, "fw_hdl_pkg.sv"), SMOKE]
    first = compile(files, incdirs=[API])
    stubs = extern_stub_bodies(first.trees, ["fw_hdl_pkg"])["fw_hdl_pkg"]
    res = compile(files, incdirs=[API],
                  defines={"SV_WEAVE_INCLUDE_fw_hdl_pkg": '"fw_stub_bodies.svh"'},
                  include_text={"fw_stub_bodies.svh": stubs},
                  extra_text={"top.sv": top_text})
    return res.check()


def _generic(cls):
    g = cls.genericClass
    return g.name if g is not None else cls.name


def _derives(cls, name):
    c = cls
    while c is not None:
        if _generic(c) == name:
            return True
        b = c.baseClass
        c = b.canonicalType if b is not None and b.canonicalType.isClass else None
    return False


def _type_param(cls, name):
    return cls.find(name).targetType.type.canonicalType


class Node:
    def __init__(self, name, parent, kind):
        self.name, self.parent, self.kind = name, parent, kind
        self.children = []

    @property
    def path(self):
        return self.name if self.parent is None else f"{self.parent.path}.{self.name}"


class MiniHost(Intrinsics):
    """A tiny stand-in for fw-hdl-fe-sv's FeRt: records the structure."""

    def __init__(self):
        self.log = []
        self.nodes = []
        self.connects = []
        self.maps = {}
        self.ctor_singletons = {}

    def _node(self, obj, name, parent, kind):
        n = Node(name, parent.host if parent is not None else None, kind)
        if n.parent is not None:
            n.parent.children.append(obj)
        obj.host = n
        self.nodes.append(obj)
        return obj

    def call(self, it, sub, this, args, named):
        owner = it.owner_class(sub)
        gname = _generic(owner) if owner is not None else None
        self.log.append((gname, sub.name))
        name = sub.name
        if name == "create" and this is None and owner is not None \
                and _derives(owner, "fw_object"):
            obj = it.allocate(owner)
            if _derives(owner, "fw_component"):
                p = args[2]
                init = it.find_method(owner, "init_params")
                if init is not None:
                    c = Cell(p)
                    it.call(init, None, (c,))
                    p = c.get()
                if obj.has("params"):
                    obj.set("params", p)
                self._node(obj, args[0], args[1], "component")
                it.run_ctor(obj)
            else:
                kind = "bundle" if _derives(owner, "fw_bundle_base") else "signal"
                self._node(obj, args[0], args[1], kind)
                it.run_ctor(obj)
                if kind == "bundle":
                    it.call_method(obj, "build")       # the bundle creates its members
            return obj
        if gname == "fw_ctor_of" and name == "get":
            o = self.ctor_singletons.get(owner)
            if o is None:
                o = self.ctor_singletons[owner] = it.construct(owner)
            return o
        if gname == "fw_ctor_of" and name == "create":
            d = _type_param(this.cls, "D")
            return it.call(it.find_method(d, "create"), None, args)
        if name in ("connect", "connect_up") and gname in (
                "fw_signal", "fw_bundle", "fw_mirror", "fw_monitor"):
            self.connects.append((name, this.host.path, args[0].host.path))
            return None
        if gname == "fw_component" and name == "connect_all":
            for a, b in args[0]:
                self.connects.append(("connect", a.host.path, b.host.path))
            return None
        if gname == "fw_value" and name == "of":     # a typed module parameter value
            o = it.allocate(owner)
            o.host = args[0]
            return o
        if gname == "fw_extern_map":
            m = self.maps.setdefault(this.host, [])
            if name == "param":
                m.append(("param", args[0], args[1].host))
                return None
            if name == "prefix":
                rule = it.construct(it.find_method(this.cls, "prefix").returnValVar.type
                                    .canonicalType)
                rule.host = {"bundle": args[0].host.path, "prefix": args[1], "renames": []}
                m.append(("prefix", rule.host))
                return rule
            m.append((name,) + tuple(args))
            return None
        if gname == "fw_pin_rule" and name == "rename":
            this.host["renames"].append((args[0], args[1]))
            return this
        if gname == "fw_param_override":
            self.log.append(("override", name, args[1]))
            return None
        return NOT_HANDLED

    # -- the phase walker (the host's business) ------------------------------
    def elaborate(self, it, top_cls):
        top = it.call(it.find_method(top_cls, "create"), None, ("top", None))
        comps = [top]
        i = 0
        while i < len(comps):                     # build, top-down
            c = comps[i]
            it.call_method(c, "build")
            comps.extend(x for x in c.host.children if x.host.kind == "component")
            i += 1
        for c in comps:
            it.call_method(c, "connect")
        for c in comps:
            if it.find_method(c.cls, "extern_map") is not None and \
                    it.find_method(c.cls, "extern_module") is not None:
                m = it.construct(_find_pkg_class(it, "fw_extern_map"))
                m.host = c.host
                it.call_method(c, "extern_map", (m,))
                self.maps.setdefault(c.host, []).insert(
                    0, ("module", it.call_method(c, "extern_module")))
        return top, comps


def _find_pkg_class(it, name):
    return it.comp.getPackage("fw_hdl_pkg").find(name)


def test_fifo_chain_elaborates():
    res = _compile("module __top; import smoke_pkg::*; "
                   "typedef fifo_chain #(bit[7:0]) T; typedef rv_fifo #(bit[7:0]) R; endmodule")
    comp = res.compilation
    host = MiniHost()
    it = Interpreter(comp, host)
    top_cls = class_from_typedef(comp, "__top", "T")
    top, comps = host.elaborate(it, top_cls)

    assert isinstance(top, Obj) and str(top.cls) == "fifo_chain#(bit[7:0])"
    params = top.get("params")
    assert (params.n, params.depth, params.deep_tail) == (2, 2, 0)
    # init_params (a `ref params_t` static) chose rv_fifo through fw_ctor_of::get
    assert _generic(params.fifo_h.cls) == "fw_ctor_of"
    assert _generic(_type_param(params.fifo_h.cls, "D")) == "rv_fifo"

    paths = sorted(o.host.path for o in host.nodes)
    for p in ("top", "top.clk", "top.in", "top.in.valid", "top.in.ready", "top.in.data",
              "top.out.data", "top.u[0]", "top.u[1]", "top.u[1].in.valid", "top.mon",
              "top.mon.clk"):
        assert p in paths, p
    u = top.get("u")
    # (the ClassType prints through the alias it was reached by, but it is the
    # one specialization rv_fifo#(bit[7:0]))
    assert len(u) == 2 and all(_generic(x.cls) == "rv_fifo" for x in u)
    assert all(x.cls is class_from_typedef(comp, "__top", "R") for x in u)
    assert [x.get("params").depth for x in u] == [2, 2]
    # mixin role: the mirror *is* an fw_rv_if; its members were made by build()
    assert top.get("in").get("valid").host.path == "top.in.valid"

    assert ("connect_up", "top.u[0].in", "top.in") in host.connects
    assert ("connect", "top.u[0].out", "top.u[1].in") in host.connects
    assert ("connect_up", "top.u[1].out", "top.out") in host.connects
    assert any(a.startswith("top.mon.") and b.startswith("top.u[0].out")
               for _, a, b in host.connects)
    assert ("connect", "top.u[1].clk", "top.clk") in host.connects
    assert ("connect", "top.mon.rst", "top.rst") in host.connects

    m0 = host.maps[u[0].host]
    assert m0[0] == ("module", "fw_rv_fifo")
    assert ("param", "WIDTH", 8) in m0 and ("param", "DEPTH", 2) in m0
    assert ("prefix", {"bundle": "top.u[0].in", "prefix": "in_",
                       "renames": [("valid", "vld"), ("ready", "rdy")]}) in m0
    assert host.maps[top.get("mon").host][0] == ("module", "fw_valid_check")


def test_soc_named_params_and_overrides():
    res = _compile("module __top; import smoke_pkg::*; typedef soc T; endmodule")
    comp = res.compilation
    host = MiniHost()
    it = Interpreter(comp, host)
    top, comps = host.elaborate(it, class_from_typedef(comp, "__top", "T"))
    chain = top.get("chain")
    p = chain.get("params")
    assert (p.n, p.depth, p.deep_tail) == (3, 2, 1)
    # deep_tail: the last FIFO gets twice the depth (the ternary in build())
    assert [x.get("params").depth for x in chain.get("u")] == [2, 2, 4]
    assert ("override", "set", "chain.u[0]") in host.log
    assert ("override", "edit", "**") in host.log
    m = host.maps[chain.get("u")[0].host]
    assert ("param", "WIDTH", 32) in m
