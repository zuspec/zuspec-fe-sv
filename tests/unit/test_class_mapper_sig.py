"""Unit tests for ClassMapper: method signatures and attributes (K5.2c)."""
import pytest

from zuspec.fe.sv.mapper import SVMapper
from zuspec.ir.core.data_type import DataTypeInt, DataTypeList, DataTypeRef
from zuspec.ir.core.fields import SignalDirection

SRC = """
package p;
  typedef struct packed { bit [3:0] a; bit b; } s_t;
  interface class api_if #(type T = int);
    (* fw_default *) pure virtual task put(input T t, output bit [3:0] o, inout s_t q[$]);
    pure virtual function bit [7:0] get();
  endclass
  class c;
    (* fw_opt *) bit [3:0] f;
    (* fw_opt = 2 *) bit g;
    bit h;
    function void m(input bit [3:0] qq[$], ref int r); endfunction
  endclass
endpackage
"""


@pytest.fixture(scope="module")
def mapped():
    m = SVMapper()
    assert m.map_text(SRC, "t.sv"), m.get_error_report()
    return m


def _proto_fn(m, name):
    p = next(p for p in m.get_protocols() if p.name == "api_if")
    return next(f for f in p.methods if f.name == name)


def _cls(m, name):
    return next(c for c in m.get_classes() if c.name == name)


def test_task_signature(mapped):
    f = _proto_fn(mapped, "put")
    assert f.is_async and f.returns is None
    args = f.args.args
    assert [a.arg for a in args] == ["t", "o", "q"]
    assert [a.direction for a in args] == [
        SignalDirection.INPUT, SignalDirection.OUTPUT, SignalDirection.INOUT]
    assert isinstance(args[0].datatype, DataTypeRef) and args[0].datatype.ref_name == "T"
    assert isinstance(args[1].datatype, DataTypeInt) and args[1].datatype.bits == 4
    assert isinstance(args[2].datatype, DataTypeList)
    assert args[2].datatype.element_type is not None


def test_function_signature(mapped):
    f = _proto_fn(mapped, "get")
    assert not f.is_async
    assert isinstance(f.returns, DataTypeInt) and f.returns.bits == 8


def test_method_attribute(mapped):
    f = _proto_fn(mapped, "put")
    assert [(a.type_qname, a.values) for a in f.annotations] == [
        ("fw_default", {"value": 1})]
    assert _proto_fn(mapped, "get").annotations == []


def test_field_attributes(mapped):
    fields = {f.name: f for f in _cls(mapped, "c").fields}
    assert [(a.type_qname, a.values) for a in fields["f"].annotations] == [
        ("fw_opt", {"value": 1})]
    assert fields["g"].annotations[0].values == {"value": 2}
    assert fields["h"].annotations == []


def test_queue_arg_and_ref(mapped):
    m = next(f for f in _cls(mapped, "c").functions if f.name == "m")
    qq, r = m.args.args
    assert isinstance(qq.datatype, DataTypeList)
    assert isinstance(qq.datatype.element_type, DataTypeInt)
    assert qq.datatype.element_type.bits == 4
    assert r.direction == SignalDirection.INOUT


def test_builtins_filtered(mapped):
    names = [f.name for f in _cls(mapped, "c").functions]
    assert names == ["m"]
