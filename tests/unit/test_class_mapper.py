"""Unit tests for class mapper."""
import pytest
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.class_mapper import ClassMapper
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.ir.core.data_type import DataTypeClass, DataTypeInt, DataTypeAction
from zuspec.ir.core.fields import SignalDirection


@pytest.fixture
def mappers():
    """Create parser and mappers with default config."""
    config = SVToZuspecConfig()
    error_reporter = ErrorReporter()
    parser = SVParser(config, error_reporter)
    type_mapper = TypeMapper(config, error_reporter)
    class_mapper = ClassMapper(config, error_reporter, type_mapper)
    return parser, class_mapper, error_reporter


def _find_class(root, name):
    """Helper: find a ClassType symbol by name via visitor."""
    found = []
    def visitor(sym):
        if hasattr(sym, 'kind') and str(sym.kind) == 'SymbolKind.ClassType':
            if str(sym.name) == name:
                found.append(sym)
        return True
    root.visit(visitor)
    return found[0] if found else None



def test_map_simple_class(mappers):
    """Test mapping a simple class."""
    parser, class_mapper, error_reporter = mappers
    
    code = """
    class simple_class;
        int x;
        bit [7:0] data;
    endclass
    """
    
    assert parser.parse_text(code)
    root = parser.get_root()
    assert root is not None
    
    # Find the class using visit
    classes = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'SymbolKind.ClassType':
                if str(symbol.name) == 'simple_class':
                    classes.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(classes) == 1
    ir_class = class_mapper.map_class(classes[0])
    
    assert ir_class is not None
    assert ir_class.name == 'simple_class'
    assert len(ir_class.fields) == 2
    
    # Check first field
    x_field = ir_class.fields[0]
    assert x_field.name == 'x'
    assert isinstance(x_field.datatype, DataTypeInt)
    assert x_field.datatype.bits == 32
    assert x_field.datatype.signed is True
    
    # Check second field
    data_field = ir_class.fields[1]
    assert data_field.name == 'data'
    assert isinstance(data_field.datatype, DataTypeInt)
    assert data_field.datatype.bits == 8
    assert data_field.datatype.signed is False
    
    assert not error_reporter.has_errors()


def test_map_class_with_inheritance(mappers):
    """Test mapping class with inheritance."""
    parser, class_mapper, error_reporter = mappers
    
    code = """
    class base_class;
        int x;
    endclass
    
    class derived_class extends base_class;
        int y;
    endclass
    """
    
    assert parser.parse_text(code)
    root = parser.get_root()
    
    # Find derived class using visit
    classes = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'SymbolKind.ClassType':
                if str(symbol.name) == 'derived_class':
                    classes.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(classes) == 1
    ir_class = class_mapper.map_class(classes[0])
    
    assert ir_class is not None
    assert ir_class.name == 'derived_class'
    # Note: base class info not stored yet (requires type resolution phase)
    assert len(ir_class.fields) >= 1
    assert not error_reporter.has_errors()


def test_map_class_with_logic_field_error(mappers):
    """Test that logic fields generate errors."""
    parser, class_mapper, error_reporter = mappers
    
    code = """
    class test_class;
        logic [31:0] data;
    endclass
    """
    
    assert parser.parse_text(code)
    root = parser.get_root()
    
    # Find the class using visit
    classes = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'SymbolKind.ClassType':
                if str(symbol.name) == 'test_class':
                    classes.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(classes) == 1
    ir_class = class_mapper.map_class(classes[0])
    
    # Should have error about 4-state type
    assert error_reporter.has_errors()
    errors = error_reporter.get_errors()
    assert any('4-state' in e.message for e in errors)


def test_map_empty_class(mappers):
    """Test mapping an empty class."""
    parser, class_mapper, error_reporter = mappers
    
    code = """
    class empty_class;
    endclass
    """
    
    assert parser.parse_text(code)
    root = parser.get_root()
    
    # Find the class using visit
    classes = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'SymbolKind.ClassType':
                if str(symbol.name) == 'empty_class':
                    classes.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(classes) == 1
    ir_class = class_mapper.map_class(classes[0])
    
    assert ir_class is not None
    assert ir_class.name == 'empty_class'
    assert len(ir_class.fields) == 0
    assert not error_reporter.has_errors()


def test_map_class_various_2state_types(mappers):
    """Test mapping class with various 2-state types."""
    parser, class_mapper, error_reporter = mappers
    
    code = """
    class test_class;
        bit b;
        byte by;
        shortint si;
        int i;
        longint li;
    endclass
    """
    
    assert parser.parse_text(code)
    root = parser.get_root()
    
    # Find the class using visit
    classes = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'SymbolKind.ClassType':
                if str(symbol.name) == 'test_class':
                    classes.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(classes) == 1
    ir_class = class_mapper.map_class(classes[0])
    
    assert ir_class is not None
    assert len(ir_class.fields) == 5
    assert not error_reporter.has_errors()
    
    # Verify types
    assert ir_class.fields[0].datatype.bits == 1   # bit
    assert ir_class.fields[1].datatype.bits == 8   # byte
    assert ir_class.fields[2].datatype.bits == 16  # shortint
    assert ir_class.fields[3].datatype.bits == 32  # int
    assert ir_class.fields[4].datatype.bits == 64  # longint

# ---------------------------------------------------------------------------
# Action class tests (Phase 2)
# ---------------------------------------------------------------------------

def test_map_action_class_basic(mappers):
    """map_class() routes zsp_action_c subclasses to DataTypeAction."""
    parser, class_mapper, error_reporter = mappers

    # Use the zsp_pkg library (loaded automatically by SVParser.parse_files)
    import os, tempfile
    src = """`include "zsp_macros.svh"
package test_pkg;
    import zsp_pkg::*;

    class my_comp_c extends zsp_component;
        `zsp_component_utils(my_comp_c)
    endclass

    class my_act_c extends zsp_action_c #(my_comp_c);
        logic [31:0] instr;   // non-rand → input
        rand logic [6:0] op;  // rand
        rand logic [3:0] alu; // rand
        `zsp_action_utils(my_act_c, my_comp_c)
    endclass
endpackage
"""
    with tempfile.NamedTemporaryFile(suffix='.sv', mode='w', delete=False) as f:
        f.write(src)
        tmp = f.name

    try:
        ok = parser.parse_files([tmp])
        assert ok, error_reporter.report()

        class_mapper.compilation = parser.compilation
        root = parser.get_root()
        sym = _find_class(root, 'my_act_c')
        assert sym is not None

        ir = class_mapper.map_class(sym)
        assert ir is not None
        assert isinstance(ir, DataTypeAction)
        assert ir.name == 'my_act_c'
        assert ir.comp_type_name == 'my_comp_c'
    finally:
        os.unlink(tmp)


def test_map_action_class_field_directions(mappers):
    """rand fields get rand_kind='rand'; non-rand get direction=INPUT."""
    parser, class_mapper, error_reporter = mappers

    import os, tempfile
    src = """`include "zsp_macros.svh"
package test_pkg2;
    import zsp_pkg::*;

    class core_c extends zsp_component;
        `zsp_component_utils(core_c)
    endclass

    class decode_c extends zsp_action_c #(core_c);
        logic [31:0] instr;
        rand logic [6:0] opcode;
        `zsp_action_utils(decode_c, core_c)
    endclass
endpackage
"""
    with tempfile.NamedTemporaryFile(suffix='.sv', mode='w', delete=False) as f:
        f.write(src)
        tmp = f.name

    try:
        ok = parser.parse_files([tmp])
        assert ok, error_reporter.report()
        class_mapper.compilation = parser.compilation
        root = parser.get_root()
        sym = _find_class(root, 'decode_c')
        assert sym is not None
        ir = class_mapper.map_class(sym)
        assert isinstance(ir, DataTypeAction)

        by_name = {f.name: f for f in ir.fields}
        assert by_name['instr'].direction == SignalDirection.INPUT
        assert by_name['instr'].rand_kind is None
        assert by_name['opcode'].rand_kind == 'rand'
        assert by_name['opcode'].direction != SignalDirection.INPUT
    finally:
        os.unlink(tmp)


def test_map_action_subclass_inherits(mappers):
    """A class that extends an action subclass is still an action."""
    parser, class_mapper, error_reporter = mappers

    import os, tempfile
    src = """`include "zsp_macros.svh"
package test_pkg3;
    import zsp_pkg::*;

    class root_c extends zsp_component;
        `zsp_component_utils(root_c)
    endclass

    class base_act_c extends zsp_action_c #(root_c);
        rand logic [6:0] opcode;
        `zsp_action_utils(base_act_c, root_c)
    endclass

    class ext_act_c extends base_act_c;
        rand logic extra;
        `zsp_action_utils(ext_act_c, root_c)
    endclass
endpackage
"""
    with tempfile.NamedTemporaryFile(suffix='.sv', mode='w', delete=False) as f:
        f.write(src)
        tmp = f.name

    try:
        ok = parser.parse_files([tmp])
        assert ok, error_reporter.report()
        class_mapper.compilation = parser.compilation
        root = parser.get_root()
        sym = _find_class(root, 'ext_act_c')
        assert sym is not None
        ir = class_mapper.map_class(sym)
        assert isinstance(ir, DataTypeAction), f"Expected DataTypeAction, got {type(ir)}"
    finally:
        os.unlink(tmp)
