"""ProcMapper: step/comb/reset_state bodies of fw-hdl classes to ProcBlock IR."""
import os

import pytest

from zuspec.fe.sv import class_from_typedef, compile, extern_stub_bodies
from zuspec.fe.sv.proc_mapper import ProcMapper, field_index
from zuspec.ir.core.expr import ExprBin, ExprRefField
from zuspec.ir.core.proc_block import ExprInReset, ProcKind
from zuspec.ir.core.stmt import StmtAssign, StmtIf

SPEC = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                     "fw-hdl-spec"))
API = os.path.join(SPEC, "bindings", "sv")

pytestmark = pytest.mark.skipif(not os.path.isdir(API),
                                reason=f"fw-hdl-spec not found at {SPEC}")

SRC = '''`include "fw_hdl_macros.svh"
package p;
  import fw_hdl_pkg::*;
  class base_c extends fw_component;
    fw_output #(bit [7:0]) q;
  endclass
  class cnt extends base_c implements fw_step_if, fw_reset_state_if;
    `fw_component_utils(cnt)
    virtual function void reset_state();
      q.write(8'h5a);
    endfunction
    virtual function void step();
      if (!fw_domain #(fw_reset_kind)::in_reset(this))
        q.write(q.read() + 8'd1);
    endfunction
  endclass
  class two extends fw_component implements fw_comb_if, fw_reset_state_if;
    `fw_component_utils(two)
    fw_output #(bit) a, b, y;
    int n;
    virtual function void comb(); y.write(a.read() & b.read()); endfunction
    function void wr(); a.write(1'b1); endfunction
    function void rd(); b.write(1'b0); n = n + 1; endfunction
    virtual function void reset_state(); a.write(1'b0); b.write(1'b1); n = 0; endfunction
    `fw_step(wr)
    `fw_step(rd)
  endclass
  class multi extends fw_component implements fw_step_if, fw_comb_if;
    `fw_component_utils(multi)
    fw_output #(bit) a, y;
    function void h(int k); a.write(1'b1); if (k > 0) h(k - 1); endfunction
    virtual function void step(); h(2); endfunction
    virtual function void comb(); a.write(1'b0); y.write(1'b1); endfunction
  endclass
  class badw extends fw_component implements fw_step_if;
    `fw_component_utils(badw)
    fw_output #(bit) a;
    virtual function void step();
      fw_output #(bit) x;
      x = a;
      x.write(1'b1);
    endfunction
  endclass
  class tm extends fw_component implements fw_step_if, fw_run_if;
    `fw_component_utils(tm)
    fw_output #(bit) a, b;
    function void g(); if (fw_time::now() > 0) a.write(1'b1); endfunction
    virtual function void step(); g(); endfunction
    virtual task run(); b.write(1'b1); endtask
  endclass
endpackage
module __top; typedef p::cnt C; typedef p::two T; typedef p::multi M;
  typedef p::badw B; typedef p::tm TM; endmodule
'''


@pytest.fixture(scope="module")
def comp():
    files = [os.path.join(API, "fw_hdl_pkg.sv")]
    first = compile(files, incdirs=[API], extra_text={"p.sv": SRC})
    stubs = extern_stub_bodies(first.trees, ["fw_hdl_pkg"])["fw_hdl_pkg"]
    return compile(files, incdirs=[API],
                   defines={"SV_WEAVE_INCLUDE_fw_hdl_pkg": '"fw_stub_bodies.svh"'},
                   include_text={"fw_stub_bodies.svh": stubs},
                   extra_text={"p.sv": SRC}).check().compilation


def _map(comp, t):
    m = ProcMapper()
    cls = class_from_typedef(comp, "__top", t)
    blocks = m.map(cls)
    assert not m.reporter.has_errors(), m.reporter.report()
    return cls, blocks


def test_step_and_reset_state(comp):
    cls, blocks = _map(comp, "C")
    assert field_index(cls) == {"q": 0}
    (b,) = blocks
    assert (b.name, b.kind, b.func, b.writes) == ("step", ProcKind.STEP, "step", [["q"]])
    (s,) = b.body
    assert isinstance(s, StmtIf), s
    assert isinstance(s.test.operand, ExprInReset), s.test
    assert s.test.operand.reset.target == []
    (a,) = s.body
    assert isinstance(a, StmtAssign) and isinstance(a.targets[0], ExprRefField)
    assert isinstance(a.value, ExprBin)
    (r,) = b.reset_body
    assert isinstance(r, StmtAssign) and r.targets[0].index == 0


def test_macro_blocks_split_reset_state(comp):
    cls, blocks = _map(comp, "T")
    by = {b.name: b for b in blocks}
    assert sorted(by) == ["comb", "rd", "wr"]
    assert by["comb"].kind == ProcKind.COMB and by["comb"].writes == [["y"]]
    assert by["wr"].kind == ProcKind.STEP and by["wr"].writes == [["a"]]
    assert by["rd"].writes == [["b"], ["n"]]
    assert len(by["wr"].reset_body) == 1
    assert len(by["rd"].reset_body) == 2
    assert by["comb"].reset_body == []


def _errs(comp, t, **kw):
    m = ProcMapper(**kw)
    blocks = m.map(class_from_typedef(comp, "__top", t))
    return blocks, [e.message for e in m.reporter.get_errors()]


def test_callee_writes_and_write_multi(comp):
    blocks, errs = _errs(comp, "M")
    by = {b.name: b for b in blocks}
    assert by["step"].writes == [["a"]]
    assert by["comb"].writes == [["a"], ["y"]]
    assert len(errs) == 1 and errs[0].startswith("FW-E-WRITE-MULTI: multi.a:"), errs


def test_unattributable_write(comp):
    _, errs = _errs(comp, "B")
    errs = [e for e in errs if e.startswith("FW-E-")]
    assert errs == ["FW-E-WRITE-PATH: badw.step: 'x.write(1'b1)' writes a target "
                    "that is not a path from the component"], errs


def test_emit_run_and_time(comp):
    blocks, errs = _errs(comp, "TM")
    assert errs == [], errs
    assert blocks[0].writes == [["a"]]
    _, errs = _errs(comp, "TM", emit=True)
    codes = sorted(e.split(":")[0] for e in errs)
    assert codes == ["FW-E-EMIT-RUN", "FW-E-EMIT-TIME"], errs
