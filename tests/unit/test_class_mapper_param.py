"""Unit tests for ClassMapper: parameterized classes and specializations (K5.2a)."""
import pytest

from zuspec.fe.sv.mapper import SVMapper
from zuspec.ir.core.data_type import (
    DataTypeClass, DataTypeInt, DataTypeParameterized, DataTypeRef,
    DataTypeSpecialized, TemplateArgType, TemplateArgValue, TemplateParamKind,
    TemplateParamType, TemplateParamValue)
from zuspec.ir.core.expr import ExprConstant, ExprRefParam

SRC = """
package p;
  typedef struct packed { bit [3:0] a; bit b; } cfg_t;
  class base_c; int x; endclass
  class gen_c #(type T = bit [7:0], int W = 4, cfg_t C = '{a:1, b:0}) extends base_c;
    T data; bit [W-1:0] w; bit [7:0] k;
  endclass
  class user_c;
    gen_c #(bit [15:0], 8) g;
    gen_c #(bit [15:0], 8) g2;
    base_c b;
  endclass
endpackage
"""


@pytest.fixture(scope="module")
def mapped():
    m = SVMapper()
    assert m.map_text(SRC, "t.sv"), m.get_error_report()
    return m


def _cls(m, name):
    return next(c for c in m.get_classes() if c.name == name)


def test_template_found(mapped):
    names = [t.name for t in mapped.get_templates()]
    assert names == ["gen_c"]
    # A specialization is not mapped as a class of its own.
    assert "gen_c" not in [c.name for c in mapped.get_classes()]


def test_template_params(mapped):
    t = mapped.get_templates()[0]
    assert isinstance(t, DataTypeParameterized)
    T, W, C = t.template_params
    assert isinstance(T, TemplateParamType) and T.kind == TemplateParamKind.TYPE
    assert T.name == "T" and T.default_value == DataTypeInt(bits=8, signed=False)
    assert isinstance(W, TemplateParamValue) and W.kind == TemplateParamKind.VALUE
    assert W.default_value == ExprConstant(value=4)
    assert W.value_type.bits == 32 and W.value_type.signed
    # A struct-valued parameter: its type is the typedef, its value packed.
    assert C.value_type == DataTypeRef(ref_name="p::cfg_t")
    assert C.default_value == ExprConstant(value=0b10)


def test_template_body(mapped):
    body = mapped.get_templates()[0].body
    assert isinstance(body, DataTypeClass) and body.name == "gen_c"
    f = {x.name: x.datatype for x in body.fields}
    assert f["data"] == DataTypeRef(ref_name="T")
    assert f["w"].bits == 4 and f["w"].width_expr == ExprRefParam(name="W")
    assert f["k"].bits == 8 and f["k"].width_expr is None


def test_specialization(mapped):
    u = _cls(mapped, "user_c")
    f = {x.name: x.datatype for x in u.fields}
    sp = f["g"]
    assert isinstance(sp, DataTypeSpecialized)
    assert sp.base_template is mapped.get_templates()[0]
    assert sp.specialized_name == "gen_c#(bit[15:0],8,5'b10)"
    T = sp.get_template_arg("T")
    assert isinstance(T, TemplateArgType) and T.type_value.bits == 16
    W = sp.get_template_arg("W")
    assert isinstance(W, TemplateArgValue) and W.value_expr == ExprConstant(value=8)
    assert sp.get_template_arg_value("C") == ExprConstant(value=0b10)
    # The same specialization maps to one object.
    assert f["g2"] is sp
    assert list(mapped.get_specializations()) == [sp.specialized_name]


def test_plain_class_ref(mapped):
    u = _cls(mapped, "user_c")
    f = {x.name: x.datatype for x in u.fields}
    assert f["b"] == DataTypeRef(ref_name="base_c")
