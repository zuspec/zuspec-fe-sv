"""Tracing: Sym values, traced loops and branches, taint (interp/trace.py)."""
import zuspec.ir.core as ir
from zuspec.fe.sv.interp import Intrinsics, NOT_HANDLED
from zuspec.fe.sv.interp.trace import leaves
from zuspec.fe.sv.interp.values import Sym, SymList, strip

N = ir.ExprRefParam(name="n")


def text(e) -> str:
    """A compact rendering of a traced expression, for the assertions."""
    if e is None:
        return "<taint>"
    if isinstance(e, ir.ExprConstant):
        return repr(e.value) if isinstance(e.value, str) else str(e.value)
    if isinstance(e, (ir.ExprRefParam, ir.ExprRefLocal, ir.ExprRefUnresolved)):
        return e.name
    if isinstance(e, ir.ExprBin):
        op = {ir.BinOp.Add: "+", ir.BinOp.Sub: "-", ir.BinOp.Mult: "*"}[e.op]
        return f"({text(e.lhs)}{op}{text(e.rhs)})"
    if isinstance(e, ir.ExprCompare):
        op = {ir.CmpOp.Lt: "<", ir.CmpOp.Eq: "==", ir.CmpOp.Gt: ">"}[e.ops[0]]
        return f"({text(e.left)}{op}{text(e.comparators[0])})"
    if isinstance(e, ir.ExprBool):
        return "(" + ("&&" if e.op == ir.BoolOp.And else "||").join(map(text, e.values)) + ")"
    if isinstance(e, ir.ExprIfExp):
        return f"({text(e.test)}?{text(e.body)}:{text(e.orelse)})"
    if isinstance(e, ir.ExprCall):
        return f"{e.func.name}(" + ",".join(map(text, e.args)) + ")"
    if isinstance(e, ir.ExprSubscript):
        return f"{text(e.value)}[{text(e.slice)}]"
    if isinstance(e, ir.ExprAttribute):
        return f"{text(e.value)}.{e.attr}"
    raise AssertionError(type(e))


class Rec(Intrinsics):
    """Records each call of ``note``: its argument and the active frames."""

    def __init__(self):
        self.notes = []
        self.frames = []

    def call(self, interp, sub, this, args, named):
        if sub.name == "note":
            self.notes.append((args[0], [(f.kind, f.iteration, f.taken) for f in interp.ctl]))
            return None
        return NOT_HANDLED

    def ctl_end(self, interp, frame):
        self.frames.append(frame)


PKG = """
package p;
  function void note(int x); endfunction
  function int arith(int n); return 2 * n + 1; endfunction
  function int tern(int n, bit b); return (b && n > 1) ? 2 * n : n; endfunction
  function string name(int i); return $sformatf("u[%0d]", i); endfunction
  function void loops(int n);
    int a[];
    a = new[n];
    foreach (a[i]) note(i * 10);
    for (int i = 0; i + 1 < n; i++) note(i);
    for (int i = 0; i < 3; i++) note(i);       // not traced: a constant bound
  endfunction
  function int tainted(int n);
    int s = 0;
    for (int i = 0; i < n; i++) s = s + i;
    return s;
  endfunction
  function void branch(int n);
    if (n > 2) note(1); else note(2);
  endfunction
  function int size_of(int n);
    int a[];
    a = new[n];
    return a.size() + $size(a);
  endfunction
  class item; int v; endclass
  class holder;
    item h[];
    function void make(int n); h = new[n]; foreach (h[i]) begin h[i] = new; h[i].v = 5; end endfunction
    function void walk(int n); for (int i = 0; i + 1 < n; i++) touch(h[i + 1]); endfunction
    function void touch(item x); endfunction
  endclass
endpackage
"""


def test_arith_and_ternary(sv):
    s = sv(PKG)
    r = s.run("arith", Sym(3, N))
    assert isinstance(r, Sym) and r.value == 7 and text(r.expr) == "((2*n)+1)"
    r = s.run("tern", Sym(3, N), 1)
    assert r.value == 6 and text(r.expr) == "((1&&(n>1))?(2*n):n)"
    r = s.run("tern", 5, 0)
    assert r == 5                              # nothing traced: a plain value


def test_sformatf_traced(sv):
    s = sv(PKG)
    r = s.run("name", Sym(2, ir.ExprRefLocal(name="i")))
    assert r.value == "u[2]" and text(r.expr) == "$sformatf('u[%0d]',i)"




def test_loops(sv):
    rec = Rec()
    s = sv(PKG, intrinsics=rec)
    s.run("loops", Sym(3, N))
    loops = [f for f in rec.frames if f.kind == "loop"]
    assert len(loops) == 2                     # the constant-bound loop is not traced
    fe, fo = loops
    assert text(fe.cond) == "(i<n)" and text(fe.start) == "0"
    assert text(fo.cond) == "((i+1)<n)"
    traced = [(text(a.expr), ctx) for a, ctx in rec.notes if isinstance(a, Sym)]
    assert traced[:3] == [("(i*10)", [("loop", k, None)]) for k in range(3)]
    assert traced[3:] == [("i", [("loop", k, None)]) for k in range(2)]
    plain = [a for a, ctx in rec.notes if not isinstance(a, Sym)]
    assert plain == [0, 1, 2]


def test_zero_iterations_unexplored(sv):
    rec = Rec()
    s = sv(PKG, intrinsics=rec)
    s.run("loops", Sym(1, N))
    fe, fo = [f for f in rec.frames if f.kind == "loop"]
    assert not fe.unexplored                   # one iteration ran
    assert fo.unexplored                       # none ran, and the body calls note()


def test_write_in_traced_loop_taints(sv):
    s = sv(PKG)
    r = s.run("tainted", Sym(3, N))
    assert isinstance(r, Sym) and r.value == 3 and r.expr is None


def test_branch(sv):
    rec = Rec()
    s = sv(PKG, intrinsics=rec)
    s.run("branch", Sym(3, N))
    (f,) = rec.frames
    assert f.kind == "if" and f.taken and text(f.cond) == "(n>2)" and f.unexplored
    assert rec.notes == [(1, [("if", 0, True)])]


def test_size_traced(sv):
    s = sv(PKG)
    r = s.run("size_of", Sym(4, N))
    assert r.value == 8 and text(r.expr) == "(n+n)"


def test_handle_subscript(sv):
    from zuspec.fe.sv import find_class
    seen = []

    class H(Intrinsics):
        def call(self, interp, sub, this, args, named):
            if sub.name == "touch":
                seen.append(args[0])
                return None
            return NOT_HANDLED
    s = sv(PKG, intrinsics=H())
    cls = find_class(s.comp, "p::holder")
    o = s.it.construct(cls)
    s.it.call_method(o, "make", [Sym(3, N)])
    assert isinstance(o.get("h"), SymList)
    assert strip(o.get("h")[0].get("v")) == 5   # written in the loop: a handle's field
    s.it.call_method(o, "walk", [Sym(3, N)])
    assert [text(a.expr) for a in seen] == ["h[(i+1)]", "h[(i+1)]"]
    assert [a.value for a in seen] == [o.get("h")[1], o.get("h")[2]]
    assert [x.name for x in leaves(seen[0].expr)] == ["h", "i"]
