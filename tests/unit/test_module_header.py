"""module_header: port widths as IR expressions over the module's
parameters, two/four-state, type-parameter ports, and the typedefs a set of
files declares in packages (core-layer1.md M8(b))."""
import zuspec.ir.core as ir

from zuspec.fe.sv.module_header import package_types, read_header

SRC = """\
package tp;
  typedef enum logic [1:0] {A, B} e_t;
  typedef struct packed { bit [3:0] x; e_t m; } s_t;
endpackage
module m import tp::*; #(parameter int W = 8, parameter int D = W*2,
                         localparam int L = $clog2(D), parameter type T = logic) (
  output logic [W-1:0] a, output logic [L:0] b, output s_t c, output T d,
  output bit [3:0] e, output logic f, output logic [W:1] g);
endmodule
"""


def _ev(e, env):
    if isinstance(e, ir.ExprConstant):
        return e.value
    if isinstance(e, ir.ExprRefParam):
        return env[e.name]
    if isinstance(e, ir.ExprBin):
        a, b = _ev(e.lhs, env), _ev(e.rhs, env)
        return {ir.BinOp.Add: a + b, ir.BinOp.Sub: a - b, ir.BinOp.Mult: a * b}[e.op]
    if isinstance(e, ir.ExprCall):
        return max(0, (_ev(e.args[0], env) - 1).bit_length())
    raise TypeError(e)


def test_width_exprs(tmp_path):
    f = tmp_path / "m.sv"
    f.write_text(SRC)
    h = read_header([str(f)], "m", overrides={"W": "5"})
    p = {x.name: x for x in h.ports}
    assert p["a"].width_expr == ir.ExprRefParam(name="W")
    assert p["e"].four_state is False and p["a"].four_state is True
    assert p["c"].width_expr.value == 6 and p["f"].width_expr.value == 1
    assert p["d"].type_param == "T" and p["d"].width_expr is None
    assert p["g"].width_expr is None                     # an offset range over W
    # The initializers, so a width over a localparam can be followed back.
    env = {"W": 5}
    env["D"] = _ev(h.param_exprs["D"], env)
    env["L"] = _ev(h.param_exprs["L"], env)
    assert _ev(p["b"].width_expr, env) == p["b"].width == 5


def test_package_types(tmp_path):
    f = tmp_path / "m.sv"
    f.write_text(SRC)
    got = package_types([str(f)], ["tp::s_t", "tp::e_t", "tp::nope", "x::y::z"])
    assert set(got) == {"tp::s_t", "tp::e_t"}
    assert got["tp::e_t"] in got["tp::s_t"]              # the member's signature
    assert set(package_types([str(f)])) == {"tp::s_t", "tp::e_t"}
