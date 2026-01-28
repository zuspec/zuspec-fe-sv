"""Unit tests for parser module."""
import pytest
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter


@pytest.fixture
def parser():
    """Create a parser with default config."""
    config = SVToZuspecConfig()
    error_reporter = ErrorReporter()
    return SVParser(config, error_reporter), error_reporter


def test_parse_simple_class(parser):
    """Test parsing a simple class."""
    sv_parser, error_reporter = parser
    
    code = """
    class simple_class;
        int x;
        
        function new();
            x = 0;
        endfunction
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    assert result is True
    assert not error_reporter.has_errors()
    assert sv_parser.get_root() is not None


def test_parse_2state_types(parser):
    """Test parsing 2-state types."""
    sv_parser, error_reporter = parser
    
    code = """
    class test_class;
        bit b;
        byte by;
        shortint si;
        int i;
        longint li;
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    assert result is True
    assert not error_reporter.has_errors()


def test_parse_bit_vector(parser):
    """Test parsing bit vectors."""
    sv_parser, error_reporter = parser
    
    code = """
    class test_class;
        bit [7:0] data;
        bit [31:0] addr;
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    assert result is True
    assert not error_reporter.has_errors()


def test_parse_syntax_error(parser):
    """Test that syntax errors are reported."""
    sv_parser, error_reporter = parser
    
    code = """
    class test_class
        int x;
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    # Should fail due to syntax error
    assert result is False or error_reporter.has_errors()


def test_parse_inheritance(parser):
    """Test parsing class inheritance."""
    sv_parser, error_reporter = parser
    
    code = """
    class base_class;
        int x;
    endclass
    
    class derived_class extends base_class;
        int y;
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    assert result is True
    assert not error_reporter.has_errors()


def test_parse_function(parser):
    """Test parsing functions."""
    sv_parser, error_reporter = parser
    
    code = """
    class test_class;
        function int add(int a, int b);
            return a + b;
        endfunction
    endclass
    """
    
    result = sv_parser.parse_text(code)
    
    assert result is True
    assert not error_reporter.has_errors()


def test_get_root_before_parse(parser):
    """Test getting root before parsing."""
    sv_parser, error_reporter = parser
    
    root = sv_parser.get_root()
    
    assert root is None
