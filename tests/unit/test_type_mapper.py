"""Unit tests for type mapper."""
import pytest
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.ir.core.data_type import DataTypeInt


@pytest.fixture
def mapper():
    """Create a type mapper with default config."""
    config = SVToZuspecConfig()
    error_reporter = ErrorReporter()
    return TypeMapper(config, error_reporter), error_reporter


def test_bit_type(mapper):
    """Test mapping of bit type."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('bit')
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 1
    assert result.signed is False
    assert not error_reporter.has_errors()


def test_bit_vector(mapper):
    """Test mapping of bit vector."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('bit', width=32)
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 32
    assert result.signed is False
    assert not error_reporter.has_errors()


def test_byte_type(mapper):
    """Test mapping of byte type."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('byte')
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 8
    assert result.signed is True
    assert not error_reporter.has_errors()


def test_shortint_type(mapper):
    """Test mapping of shortint type."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('shortint')
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 16
    assert result.signed is True
    assert not error_reporter.has_errors()


def test_int_type(mapper):
    """Test mapping of int type."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('int')
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 32
    assert result.signed is True
    assert not error_reporter.has_errors()


def test_longint_type(mapper):
    """Test mapping of longint type."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('longint')
    
    assert result is not None
    assert isinstance(result, DataTypeInt)
    assert result.bits == 64
    assert result.signed is True
    assert not error_reporter.has_errors()


def test_logic_type_four_state(mapper):
    """``logic`` maps to a 4-state DataTypeInt (F8a)."""
    type_mapper, error_reporter = mapper

    result = type_mapper.map_builtin_type('logic', width=32, file_path='test.sv', line=10)

    assert result == DataTypeInt(bits=32, signed=False, four_state=True)
    assert type_mapper.map_builtin_type('logic').bits == 1
    assert not error_reporter.has_errors()


def test_reg_type_four_state(mapper):
    """``reg`` maps to a 4-state DataTypeInt."""
    type_mapper, error_reporter = mapper

    result = type_mapper.map_builtin_type('reg')

    assert result == DataTypeInt(bits=1, signed=False, four_state=True)
    assert not error_reporter.has_errors()


def test_integer_type_four_state(mapper):
    """``integer`` maps to a 32-bit signed 4-state DataTypeInt."""
    type_mapper, error_reporter = mapper

    result = type_mapper.map_builtin_type('integer')

    assert result == DataTypeInt(bits=32, signed=True, four_state=True)
    assert not error_reporter.has_errors()


def test_case_insensitive(mapper):
    """Test that type names are case insensitive."""
    type_mapper, error_reporter = mapper
    
    result1 = type_mapper.map_builtin_type('BIT')
    result2 = type_mapper.map_builtin_type('Bit')
    result3 = type_mapper.map_builtin_type('INT')
    
    assert result1 is not None
    assert result2 is not None
    assert result3 is not None
    assert not error_reporter.has_errors()


def test_unsupported_type(mapper):
    """Test unsupported type generates error."""
    type_mapper, error_reporter = mapper
    
    result = type_mapper.map_builtin_type('unknown_type')
    
    assert result is None
    assert error_reporter.has_errors()
    
    errors = error_reporter.get_errors()
    assert len(errors) == 1
    assert 'Unsupported type' in errors[0].message


def test_is_4state_type(mapper):
    """Test 4-state type detection."""
    type_mapper, _ = mapper
    
    assert type_mapper.is_4state_type('logic')
    assert type_mapper.is_4state_type('reg')
    assert type_mapper.is_4state_type('integer')
    assert type_mapper.is_4state_type('LOGIC')
    
    assert not type_mapper.is_4state_type('bit')
    assert not type_mapper.is_4state_type('int')
    assert not type_mapper.is_4state_type('byte')
