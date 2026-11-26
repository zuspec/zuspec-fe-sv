"""Struct-valued class parameters, parameterized interface classes and
method order through SV -> IR -> SV (F8b steps 1-3)."""
import pytest

from zuspec.fe.sv.mapper import SVMapper
from zuspec.ir.core.data_type import DataTypeParameterized, DataTypeProtocol
from zuspec.ir.core.expr import (
    ExprConstant, ExprRefPackage, ExprStructLiteral)

SRC = """
package p;
  typedef struct packed { int unsigned A; int unsigned B; } prm_t;
  typedef struct packed { prm_t in; bit [7:0] C; } nest_t;
  localparam prm_t DEF = '{A: 4, B: 8};
  interface class api_if #(prm_t P = DEF, type T = int);
    pure virtual task put(input T t);
    pure virtual function bit can_put();
  endclass
  class c #(prm_t P = DEF, int W = 3, prm_t Q = '{A: 1, B: 2},
            nest_t N = '{in: '{A: 5, B: 6}, C: 7});
    bit [W-1:0] x;
    virtual task t1(); endtask
    virtual function int f1(); return 0; endfunction
    virtual task t2(); endtask
  endclass
endpackage
"""


@pytest.fixture(scope="module")
def mapped():
    m = SVMapper()
    assert m.map_text(SRC, "t.sv"), m.get_error_report()
    return m


def _params(m, name):
    t = next(t for t in m.get_templates() + m.get_api_templates() if t.name == name)
    return {tp.name: tp.default_value for tp in t.template_params}


def _fields(lit):
    return {f.name: f.value for f in lit.fields}


def test_named_struct_default_is_a_reference(mapped):
    d = _params(mapped, "c")["P"]
    assert d == ExprRefPackage(package="p", name="DEF")


def test_literal_struct_default(mapped):
    d = _params(mapped, "c")["Q"]
    assert isinstance(d, ExprStructLiteral)
    assert _fields(d) == {"A": ExprConstant(value=1), "B": ExprConstant(value=2)}


def test_nested_struct_default(mapped):
    d = _fields(_params(mapped, "c")["N"])
    assert _fields(d["in"]) == {"A": ExprConstant(value=5), "B": ExprConstant(value=6)}
    assert d["C"] == ExprConstant(value=7)


def test_integral_default_unchanged(mapped):
    assert _params(mapped, "c")["W"] == ExprConstant(value=3)


def test_parameterized_interface_class_keeps_params(mapped):
    api = next(t for t in mapped.get_api_templates() if t.name == "api_if")
    assert isinstance(api, DataTypeParameterized)
    assert [tp.name for tp in api.template_params] == ["P", "T"]
    assert isinstance(api.body, DataTypeProtocol)
    assert any(p is api.body for p in mapped.get_protocols())


def test_decls_once(mapped):
    names = [d.name for d in mapped.get_decls()]
    assert sorted(names) == ["api_if", "c"]


def test_round_trip_text():
    pytest.importorskip("zuspec.be.sv")
    from zuspec.be.sv.ir.core_to_sv import translate_ir_package
    from zuspec.be.sv.ir.sv_emit import SVEmitter
    m = SVMapper()
    assert m.map_text(SRC, "t.sv"), m.get_error_report()
    (pkg,) = m.get_packages()
    sv = SVEmitter().emit_one(translate_ir_package(pkg, m.get_types(), m.get_decls()))
    assert "localparam prm_t DEF = '{A: 4, B: 8};" in sv
    assert "interface class api_if #(" in sv
    assert "P = DEF" in sv
    assert "Q = '{A: 1, B: 2}" in sv
    assert "N = '{in: '{A: 5, B: 6}, C: 7}" in sv
    # methods keep source order: task, function, task
    assert sv.index("task t1") < sv.index("function int f1") < sv.index("task t2")
    assert sv.index("task put") < sv.index("function bit can_put")
