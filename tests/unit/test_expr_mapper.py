"""Unit tests for expression mapper."""
import pytest
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.dataclasses.ir.expr import ExprBin, ExprUnary, ExprConstant, ExprRefLocal, BinOp, UnaryOp


@pytest.fixture
def mapper():
    """Create an expression mapper with default config."""
    config = SVToZuspecConfig()
    error_reporter = ErrorReporter()
    return ExprMapper(config, error_reporter), error_reporter


def test_map_binary_add(mapper):
    """Test mapping binary addition."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function int add(int a, int b);
            return a + b;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    # Find the function and its return statement
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            kind_str = str(symbol.kind)
            if kind_str == 'ExpressionKind.BinaryOp':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(found_expr) > 0
    
    # Map the expression
    ir_expr = expr_mapper.map_expression(found_expr[0])
    
    assert ir_expr is not None
    assert isinstance(ir_expr, ExprBin)
    assert ir_expr.op == BinOp.Add
    assert not error_reporter.has_errors()


def test_map_binary_subtract(mapper):
    """Test mapping binary subtraction."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function int sub(int a, int b);
            return a - b;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'ExpressionKind.BinaryOp':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(found_expr) > 0
    ir_expr = expr_mapper.map_expression(found_expr[0])
    
    assert ir_expr is not None
    assert isinstance(ir_expr, ExprBin)
    assert ir_expr.op == BinOp.Sub


def test_map_binary_multiply(mapper):
    """Test mapping binary multiplication."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function int mul(int a, int b);
            return a * b;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'ExpressionKind.BinaryOp':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(found_expr) > 0
    ir_expr = expr_mapper.map_expression(found_expr[0])
    
    assert ir_expr is not None
    assert isinstance(ir_expr, ExprBin)
    assert ir_expr.op == BinOp.Mult


def test_map_bitwise_and(mapper):
    """Test mapping bitwise AND."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function int bitand(int a, int b);
            return a & b;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'ExpressionKind.BinaryOp':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(found_expr) > 0
    ir_expr = expr_mapper.map_expression(found_expr[0])
    
    assert ir_expr is not None
    assert isinstance(ir_expr, ExprBin)
    assert ir_expr.op == BinOp.BitAnd


def test_map_integer_literal(mapper):
    """Test mapping integer literals."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function int get_const();
            return 42;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            kind_str = str(symbol.kind)
            if 'Literal' in kind_str or 'Constant' in kind_str:
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    # Should find at least one literal
    assert len(found_expr) > 0


def test_map_named_value(mapper):
    """Test mapping variable references."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        int x;
        function int get_x();
            return x;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'ExpressionKind.NamedValue':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    assert len(found_expr) > 0
    
    ir_expr = expr_mapper.map_expression(found_expr[0])
    assert ir_expr is not None
    assert isinstance(ir_expr, ExprRefLocal)


def test_reject_4state_equality(mapper):
    """Test that 4-state equality operators are rejected."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function bit check(int a, int b);
            return a === b;
        endfunction
    endclass
    """
    
    parser.parse_text(code)
    root = parser.get_root()
    
    found_expr = []
    def visitor(symbol):
        if hasattr(symbol, 'kind'):
            if str(symbol.kind) == 'ExpressionKind.BinaryOp':
                found_expr.append(symbol)
        return True
    
    root.visit(visitor)
    
    if found_expr:
        ir_expr = expr_mapper.map_expression(found_expr[0])
        
        # Should generate an error
        assert error_reporter.has_errors()
        errors = error_reporter.get_errors()
        assert any('4-state' in e.message for e in errors)
