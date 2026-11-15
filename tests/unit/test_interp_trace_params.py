"""Tracing class parameters and types as written (interp/trace.py): the
``symbolic_param`` and ``ctl_force`` hooks, member reads of a shape,
``localparam`` initializers, and :class:`SynTracer` reading class arguments
at a call site (``C #(bit [W-1:0])::f()``)."""
import zuspec.ir.core as ir
from zuspec.fe.sv.interp import Intrinsics, NOT_HANDLED
from zuspec.fe.sv.interp.trace import TAINTED, SynTracer, TType
from zuspec.fe.sv.interp.values import Sym


def text(e) -> str:
    """A compact rendering of a traced expression."""
    if e is None:
        return "<taint>"
    if isinstance(e, ir.ExprConstant):
        return str(e.value)
    if isinstance(e, (ir.ExprRefParam, ir.ExprRefLocal, ir.ExprRefUnresolved)):
        return e.name
    if isinstance(e, ir.ExprBin):
        op = {ir.BinOp.Add: "+", ir.BinOp.Sub: "-", ir.BinOp.Mult: "*"}[e.op]
        return f"({text(e.lhs)}{op}{text(e.rhs)})"
    if isinstance(e, ir.ExprCompare):
        op = {ir.CmpOp.Lt: "<", ir.CmpOp.Eq: "==", ir.CmpOp.Gt: ">"}[e.ops[0]]
        return f"({text(e.left)}{op}{text(e.comparators[0])})"
    if isinstance(e, ir.ExprCall):
        return f"{e.func.name}(" + ",".join(map(text, e.args)) + ")"
    if isinstance(e, ir.ExprAttribute):
        return f"{text(e.value)}.{e.attr}"
    raise AssertionError(type(e))

PKG = """
package p;
  typedef struct packed { int data_w; int depth; } shape_t;
  class sig #(type T = bit);
    static function int create(string n); return $bits(T); endfunction
  endclass
  class rv #(type T = bit); endclass
  class wrap #(type B = int);
    static function int create(string n); return 0; endfunction
  endclass
  class foo #(int W = 8, shape_t S = '{data_w: 16, depth: 4}, type U = bit [3:0]);
    typedef bit [W*2-1:0] dw_t;
    localparam int LW = W + 1;
    int got[$];
    function void build();
      got.push_back(W);
      got.push_back($clog2(W));
      got.push_back(S.data_w);
      got.push_back(LW);
      got.push_back(sig #(bit [W-1:0])::create("a"));
      got.push_back(wrap #(rv #(logic signed [S.data_w-1:0]))::create("b"));
      got.push_back(sig #(.T(dw_t))::create("c"));
      got.push_back(sig #(U)::create("d"));
      got.push_back(sig #(bit [7:0])::create("e"));
    endfunction
    function void branch();
      if (W > 4) got.push_back(1); else got.push_back(2);
    endfunction
    function void assign_only();
      int x;
      if (W > 4) got.push_back(1); else x = 3;
    endfunction
  endclass
  typedef foo#(8) foo8;
endpackage
"""


class Host(Intrinsics):
    """Traces foo#(8)'s class parameters; records the class of each
    ``create`` call as written."""

    def __init__(self, force=None):
        self.cls = None
        self.leaves = {}
        self.created = {}
        self.frames = []
        self.force = force

    def leaf(self, name):
        return self.leaves.setdefault(name, ir.ExprRefParam(name=name))

    def symbolic_param(self, interp, sym):
        m = self.cls.find(sym.name)
        if m is None or not (m is sym or m == sym) or m.isLocalParam:
            return None
        if type(sym).__name__ == "TypeParameterSymbol":
            return self.leaf(sym.name)
        from zuspec.fe.sv.interp.values import const_to_py
        return Sym(const_to_py(sym.value, sym.type), self.leaf(sym.name))

    def call(self, interp, sub, this, args, named):
        if sub.name == "create":
            owner = interp.owner_class(sub)
            t = SynTracer(interp).call_class(interp.call_expr, interp.stack[-1].sub, owner)
            self.created[args[0]] = t
        return NOT_HANDLED

    def ctl_end(self, interp, frame):
        self.frames.append(frame)

    def ctl_force(self, interp, frame):
        return self.force


def _run(sv, method, force=None):
    from zuspec.fe.sv import class_from_typedef  # noqa: F401
    h = Host(force)
    s = sv(PKG, intrinsics=h)
    cls = s.pkg.find("foo8").targetType.type.canonicalType
    h.cls = cls
    o = s.it.construct(cls)
    s.it.call_method(o, method)
    return h, o


def test_class_parameters_traced(sv):
    h, o = _run(sv, "build")
    w, clog, dw, lw = o.get("got")[:4]
    assert isinstance(w, Sym) and w.value == 8 and w.expr is h.leaves["W"]
    assert clog.value == 3 and text(clog.expr) == "clog2(W)"
    # A member of a struct-valued parameter (packed): an attribute of its leaf.
    assert dw.value == 16 and text(dw.expr) == "S.data_w"
    # A localparam: its initializer, traced.
    assert lw.value == 9 and text(lw.expr) == "(W+1)"


def test_class_arguments_as_written(sv):
    h, _o = _run(sv, "build")
    a = h.created["a"].args["T"]
    assert isinstance(a, TType) and not a.tainted
    assert a.ir.bits == 8 and not a.ir.signed and a.ir.width_expr is h.leaves["W"]
    # A class argument inside a class argument; a shape member; signed.
    b = h.created["b"].args["B"]
    t = b.args["T"]
    assert t.ir.signed and text(t.ir.width_expr) == "S.data_w"
    # A class-scope typedef, named: its own syntax, in its own scope.
    c = h.created["c"].args["T"]
    assert c.ir.bits == 16 and text(c.ir.width_expr) == "(W*2)"
    # A type parameter: a reference to it, by its leaf.
    d = h.created["d"].args["T"]
    assert isinstance(d.ir, ir.DataTypeRef) and d.ir.ref_name == "U" and d.leaf is h.leaves["U"]
    # A constant type: nothing traced.
    e = h.created["e"].args["T"]
    assert e.ir is None and not e.tainted and not e.traced


def test_branch_forced(sv):
    h, o = _run(sv, "branch")
    (f,) = h.frames
    assert f.taken and not f.forced and text(f.cond) == "(W>4)" and f.key is not None
    assert [x for x in o.get("got")] == [1]
    h, o = _run(sv, "branch", force=False)
    (f,) = h.frames
    assert f.forced and not f.taken and o.get("got") == [2]


def test_branch_assignment_is_an_effect(sv):
    h, _o = _run(sv, "assign_only")
    (f,) = h.frames
    assert f.taken and f.unexplored            # the else arm only assigns


def test_tainted_type_parameter(sv):
    class Taint(Host):
        def symbolic_param(self, interp, sym):
            if sym.name == "U":
                return TAINTED
            return super().symbolic_param(interp, sym)
    h = Taint()
    s = sv(PKG, intrinsics=h)
    cls = s.pkg.find("foo8").targetType.type.canonicalType
    h.cls = cls
    s.it.call_method(s.it.construct(cls), "build")
    assert h.created["d"].args["T"].tainted


DFLT = """
package p;
  function int note(); return 0; endfunction
  function int callee(int a = note()); return a; endfunction
  function int caller(); return callee(); endfunction
endpackage
"""


def test_default_argument_runs_in_the_callee(sv):
    """A default argument is evaluated with the callee's frame on the stack,
    so a host sees which call it belongs to."""
    seen = []

    class H(Intrinsics):
        def call(self, interp, sub, this, args, named):
            if sub.name == "note":
                seen.append([f.sub.name for f in interp.stack])
                return 0
            return NOT_HANDLED
    s = sv(DFLT, intrinsics=H())
    assert s.run("caller") == 0
    assert seen == [["caller", "callee"]]
