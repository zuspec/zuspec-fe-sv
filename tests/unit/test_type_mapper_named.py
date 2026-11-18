"""Unit tests for TypeMapper.map_type: packed structs, enums and typedefs."""
import pytest

from zuspec.fe.sv import compile
from zuspec.fe.sv.class_mapper import ClassMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.ir.core.data_type import (
    DataTypeEnum, DataTypeInt, DataTypeRef, DataTypeStruct)

SRC = """
package p;
  typedef enum bit [1:0] {A, B=2, C} e_t;
  typedef struct packed { bit [3:0] a; e_t e; } s_t;
  typedef bit [7:0] byte_t;
  typedef s_t s2_t;
  typedef logic [3:0] l_t;
  class c;
    typedef struct packed { bit x; bit [2:0] y; } loc_t;
    s_t f1; loc_t f2; byte_t f3; s2_t f4; e_t f5;
    enum bit {X, Y} f6;
    struct packed { bit q; bit [1:0] r; } f7;
    l_t f8;
  endclass
endpackage
"""


@pytest.fixture(scope="module")
def cls():
    res = compile(extra_text={"t.sv": SRC}).check()
    return res.compilation.getPackage("p").find("c")


@pytest.fixture
def tm():
    er = ErrorReporter()
    return TypeMapper(SVToZuspecConfig(), er), er


def _ftype(cls, name):
    return cls.find(name).type


def test_typedef_packed_struct(cls, tm):
    m, er = tm
    t = m.map_type(_ftype(cls, "f1"))
    assert t == DataTypeRef(ref_name="p::s_t")
    s = m.types["p::s_t"]
    assert isinstance(s, DataTypeStruct) and s.packed and s.name == "p::s_t"
    assert [f.name for f in s.fields] == ["a", "e"]
    assert s.fields[0].datatype.bits == 4
    # A member of a typedef'd enum type refers to it; it is mapped first.
    assert s.fields[1].datatype == DataTypeRef(ref_name="p::e_t")
    assert list(m.types) == ["p::e_t", "p::s_t"]
    assert not er.has_errors()


def test_typedef_enum(cls, tm):
    m, er = tm
    assert m.map_type(_ftype(cls, "f5")) == DataTypeRef(ref_name="p::e_t")
    e = m.types["p::e_t"]
    assert isinstance(e, DataTypeEnum)
    assert e.items == {"A": 0, "B": 2, "C": 3}
    assert list(e.items) == ["A", "B", "C"]
    assert e.width == 2
    assert e.base_type.bits == 2 and e.base_type.four_state is False


def test_class_local_typedef(cls, tm):
    m, er = tm
    assert m.map_type(_ftype(cls, "f2")) == DataTypeRef(ref_name="p::c::loc_t")
    s = m.types["p::c::loc_t"]
    assert s.packed and [f.datatype.bits for f in s.fields] == [1, 3]
    # The typedef symbol itself maps the same way (declared, unused).
    m2 = TypeMapper(SVToZuspecConfig(), ErrorReporter())
    assert m2.map_type(cls.find("loc_t")) == DataTypeRef(ref_name="p::c::loc_t")


def test_typedef_integral(cls, tm):
    m, er = tm
    assert m.map_type(_ftype(cls, "f3")) == DataTypeRef(ref_name="p::byte_t")
    t = m.types["p::byte_t"]
    assert isinstance(t, DataTypeInt) and t.name == "p::byte_t"
    assert t.bits == 8 and not t.signed


def test_typedef_of_typedef(cls, tm):
    m, er = tm
    assert m.map_type(_ftype(cls, "f4")) == DataTypeRef(ref_name="p::s2_t")
    assert m.types["p::s2_t"] == DataTypeRef(name="p::s2_t", ref_name="p::s_t")
    assert "p::s_t" in m.types


def test_anonymous_enum_and_struct(cls, tm):
    m, er = tm
    e = m.map_type(_ftype(cls, "f6"))
    assert isinstance(e, DataTypeEnum) and e.name is None
    assert e.items == {"X": 0, "Y": 1}
    s = m.map_type(_ftype(cls, "f7"))
    assert isinstance(s, DataTypeStruct) and s.packed and s.name is None
    assert [f.datatype.bits for f in s.fields] == [1, 2]
    assert m.types == {}


def test_four_state_typedef(cls, tm):
    m, er = tm
    assert m.map_type(_ftype(cls, "f8")) == DataTypeRef(ref_name="p::l_t")
    assert not er.has_errors()
    assert m.types["p::l_t"].four_state is True
    m2 = TypeMapper(SVToZuspecConfig(), ErrorReporter())
    assert m2.map_type(_ftype(cls, "f8"), allow_4state=False) is None
    assert m2.error_reporter.has_errors()


def test_class_fields_use_named_types():
    src = SRC.replace("    l_t f8;\n", "")
    res = compile(extra_text={"t.sv": src}).check()
    sym = res.compilation.getPackage("p").find("c")
    er = ErrorReporter()
    tmap = TypeMapper(SVToZuspecConfig(), er)
    ir = ClassMapper(SVToZuspecConfig(), er, tmap).map_class(sym)
    assert not er.has_errors(), er.report()
    ft = {f.name: f.datatype for f in ir.fields}
    assert ft["f1"] == DataTypeRef(ref_name="p::s_t")
    assert ft["f2"] == DataTypeRef(ref_name="p::c::loc_t")
    assert isinstance(ft["f7"], DataTypeStruct)
    assert set(tmap.types) == {"p::e_t", "p::s_t", "p::c::loc_t", "p::byte_t", "p::s2_t"}
