"""Unit tests for class mapper."""
import pytest
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.class_mapper import ClassMapper
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.dataclasses.ir.data_type import DataTypeClass, DataTypeInt


@pytest.fixture
def mappers():
    """Create parser and mappers with default config."""
    config = SVToZuspecConfig()
    error_reporter = ErrorReporter()
    parser = SVParser(config, error_reporter)
    type_mapper = TypeMapper(config, error_reporter)
    class_mapper = ClassMapper(config, error_reporter, type_mapper)
    return parser, class_mapper, error_reporter


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
