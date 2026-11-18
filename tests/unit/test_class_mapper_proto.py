"""Unit tests for ClassMapper: extends, implements and interface classes (K5.2b)."""
import pyslang
import pytest

from zuspec.fe.sv.mapper import SVMapper
from zuspec.ir.core.data_type import (
    DataTypeParameterized, DataTypeProtocol, DataTypeRef, DataTypeSpecialized,
    TemplateParamKind)

SRC = """
package p;
  interface class api_if #(type T = int);
    pure virtual task put(input T t);
    pure virtual function bit try_get(output T t);
  endclass
  interface class plain_if;
    pure virtual function int peek();
  endclass
  class base_c; int x; endclass
  class gen_c #(type T = bit [7:0]) extends base_c implements api_if#(T), plain_if;
    T data;
    virtual task put(input T t); endtask
    virtual function bit try_get(output T t); return 0; endfunction
    virtual function int peek(); return 0; endfunction
  endclass
  class d_c extends gen_c#(bit [3:0]); endclass
  class e_c implements plain_if;
    virtual function int peek(); return 1; endfunction
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


def test_protocols_collected(mapped):
    names = sorted(p.name for p in mapped.get_protocols())
    assert names == ["api_if", "plain_if"]
    assert all(isinstance(p, DataTypeProtocol) for p in mapped.get_protocols())


def test_protocols_not_classes_or_templates(mapped):
    cls = {c.name for c in mapped.get_classes()}
    assert "plain_if" not in cls and "api_if" not in cls
    assert [t.name for t in mapped.get_templates()] == ["gen_c"]


def test_protocol_methods(mapped):
    protos = {p.name: p for p in mapped.get_protocols()}
    assert [f.name for f in protos["api_if"].methods] == ["put", "try_get"]
    assert [f.name for f in protos["plain_if"].methods] == ["peek"]


def test_template_super_and_implements(mapped):
    body = next(t for t in mapped.get_templates() if t.name == "gen_c").body
    assert isinstance(body.super, DataTypeRef) and body.super.ref_name == "base_c"
    assert body.implements == ["api_if", "plain_if"]


def test_extends_specialization(mapped):
    d = _cls(mapped, "d_c")
    assert isinstance(d.super, DataTypeSpecialized)
    assert d.super.base_template.name == "gen_c"
    assert d.implements == []


def test_plain_implements(mapped):
    e = _cls(mapped, "e_c")
    assert e.super is None
    assert e.implements == ["plain_if"]


def _generic(m, name):
    found = []

    def visitor(sym):
        if sym.kind == pyslang.ast.SymbolKind.GenericClassDef and sym.name == name:
            found.append(sym)
        return True
    m.parser.get_root().visit(visitor)
    return found[0]


def test_generic_api(mapped):
    tmpl = mapped.class_mapper.map_generic_api(_generic(mapped, "api_if"))
    assert isinstance(tmpl, DataTypeParameterized) and tmpl.name == "api_if"
    assert [p.name for p in tmpl.template_params] == ["T"]
    assert tmpl.template_params[0].kind == TemplateParamKind.TYPE
    assert isinstance(tmpl.body, DataTypeProtocol)
    assert [f.name for f in tmpl.body.methods] == ["put", "try_get"]
    arg = tmpl.body.methods[0].args.args[0]
    assert isinstance(arg.datatype, DataTypeRef) and arg.datatype.ref_name == "T"
    assert mapped.class_mapper.map_generic_api(_generic(mapped, "api_if")) is tmpl
    # map_generic_class keeps mapping it to the plain protocol
    assert isinstance(mapped.class_mapper.map_generic_class(
        _generic(mapped, "api_if")), DataTypeProtocol)


SPEC_SRC = """
package scenario;
  typedef struct packed { bit [7:0] a; } test_pkt_t;
  interface class test_put_if #(type T = int);
    pure virtual task put(input T t);
  endclass
  interface class mem_if #(type ADDR = logic [31:0], type DATA = logic [7:0]);
    pure virtual task wr(input ADDR a, input DATA d);
  endclass
  class c;
    test_put_if#(byte) a;
    test_put_if#(test_pkt_t) b;
    test_put_if#(logic [3:0]) l;
  endclass
endpackage
"""


@pytest.fixture(scope="module")
def spec_mapped():
    m = SVMapper()
    assert m.map_text(SPEC_SRC, "s.sv"), m.get_error_report()
    return m


def _member_types(m):
    out = {}

    def visitor(sym):
        if sym.kind == pyslang.ast.SymbolKind.ClassProperty:
            out[sym.name] = sym.type
        return True
    m.parser.get_root().visit(visitor)
    return out


def test_specialization_maps_actual_args(spec_mapped):
    """F8a: a specialization's methods take its actual argument types, not
    the type parameter's alias (``scenario::test_put_if::T``, one name per
    generic, so ``#(byte)`` and ``#(test_pkt_t)`` collided)."""
    from zuspec.ir.core.data_type import DataTypeInt
    cm = spec_mapped.class_mapper
    ts = _member_types(spec_mapped)
    arg = {}
    for n in ("a", "b", "l"):
        p = cm.map_protocol(ts[n], str(ts[n]))
        arg[n] = p.methods[0].args.args[0].datatype
    assert arg["a"] == DataTypeInt(bits=8, signed=True)
    assert isinstance(arg["b"], DataTypeRef) and arg["b"].ref_name == "scenario::test_pkt_t"
    assert arg["l"].bits == 4 and arg["l"].four_state is True
    assert not any(k.endswith("::T") for k in spec_mapped.get_types())
    assert not spec_mapped.has_errors(), spec_mapped.get_error_report()


def test_generic_api_four_state_defaults(spec_mapped):
    """F8a: ``type ADDR = logic [31:0]`` keeps its default, 4-state."""
    tmpl = spec_mapped.class_mapper.map_generic_api(_generic(spec_mapped, "mem_if"))
    d = {p.name: p.default_value for p in tmpl.template_params}
    assert d["ADDR"].bits == 32 and d["ADDR"].four_state is True
    assert d["DATA"].bits == 8 and d["DATA"].four_state is True
    args = tmpl.body.methods[0].args.args
    assert [a.datatype.ref_name for a in args] == ["ADDR", "DATA"]
    assert not spec_mapped.has_errors(), spec_mapped.get_error_report()
