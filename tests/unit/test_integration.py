"""Integration tests for the complete SV to IR mapper."""
import pytest
from zuspec.fe.sv.mapper import SVMapper
from zuspec.dataclasses.ir.data_type import DataTypeClass, DataTypeInt


def test_map_simple_uvm_like_class():
    """Test mapping a UVM-like transaction class."""
    mapper = SVMapper()
    
    code = """
    class transaction;
        bit [31:0] addr;
        bit [7:0] data;
        bit write;
        
        function new();
        endfunction
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    assert not mapper.has_errors()
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    trans = classes[0]
    assert trans.name == 'transaction'
    assert len(trans.fields) == 3
    
    # Check fields
    assert trans.fields[0].name == 'addr'
    assert trans.fields[0].datatype.bits == 32
    
    assert trans.fields[1].name == 'data'
    assert trans.fields[1].datatype.bits == 8
    
    assert trans.fields[2].name == 'write'
    assert trans.fields[2].datatype.bits == 1


def test_map_multiple_classes():
    """Test mapping multiple classes."""
    mapper = SVMapper()
    
    code = """
    class my_packet;
        int length;
    endclass
    
    class my_seq;
        int id;
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    assert not mapper.has_errors()
    
    classes = mapper.get_classes()
    assert len(classes) == 2
    
    class_names = [c.name for c in classes]
    assert 'my_packet' in class_names
    assert 'my_seq' in class_names


def test_map_with_4state_error():
    """Test that 4-state types generate proper errors."""
    mapper = SVMapper()
    
    code = """
    class bad_class;
        logic [31:0] data;
    endclass
    """
    
    result = mapper.map_text(code)
    
    # Should fail due to 4-state type
    assert result is False
    assert mapper.has_errors()
    
    report = mapper.get_error_report()
    assert '4-state' in report


def test_map_mixed_2state_types():
    """Test mapping various 2-state types."""
    mapper = SVMapper()
    
    code = """
    class my_types;
        bit enable;
        byte b;
        shortint si;
        int i;
        longint li;
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    assert not mapper.has_errors()
    
    classes = mapper.get_classes()
    assert len(classes) == 1
    
    cls = classes[0]
    assert len(cls.fields) == 5
    
    # Verify all types are correctly mapped
    assert cls.fields[0].datatype.bits == 1   # bit
    assert cls.fields[1].datatype.bits == 8   # byte
    assert cls.fields[2].datatype.bits == 16  # shortint
    assert cls.fields[3].datatype.bits == 32  # int
    assert cls.fields[4].datatype.bits == 64  # longint


def test_map_inheritance_chain():
    """Test mapping class inheritance."""
    mapper = SVMapper()
    
    code = """
    class base;
        int x;
    endclass
    
    class derived extends base;
        int y;
    endclass
    
    class further extends derived;
        int z;
    endclass
    """
    
    result = mapper.map_text(code)
    
    assert result is True
    assert not mapper.has_errors()
    
    classes = mapper.get_classes()
    assert len(classes) == 3
    
    # All classes should be mapped
    class_names = [c.name for c in classes]
    assert 'base' in class_names
    assert 'derived' in class_names
    assert 'further' in class_names


def test_empty_input():
    """Test with empty input."""
    mapper = SVMapper()
    
    result = mapper.map_text("")
    
    assert result is True
    assert not mapper.has_errors()
    assert len(mapper.get_classes()) == 0


def test_syntax_error_handling():
    """Test that syntax errors are properly reported."""
    mapper = SVMapper()
    
    code = """
    class broken
        int x;
    endclass
    """
    
    result = mapper.map_text(code)
    
    # Should fail due to syntax error
    assert result is False or mapper.has_errors()
