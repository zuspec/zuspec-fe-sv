"""Unit tests for expression mapper."""
import pytest
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter
from zuspec.ir.core.expr import ExprBin, ExprUnary, ExprConstant, ExprRefLocal, BinOp, UnaryOp


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


@pytest.mark.parametrize("op,cmp", [("===", "CaseEq"), ("!==", "CaseNotEq")])
def test_map_case_equality(mapper, op, cmp):
    """``===``/``!==`` map to ``CmpOp.CaseEq``/``CaseNotEq``."""
    from zuspec.ir.core import CmpOp
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    parser.parse_text(f"""
    class test;
        function bit check(logic [3:0] a, logic [3:0] b);
            return a {op} b;
        endfunction
    endclass
    """)
    found = []
    parser.get_root().visit(lambda s: (str(getattr(s, 'kind', '')) == 'ExpressionKind.BinaryOp'
                                        and found.append(s)) or True)
    assert found
    ir_expr = expr_mapper.map_expression(found[0])
    assert not error_reporter.has_errors()
    assert ir_expr.ops == [getattr(CmpOp, cmp)]


def test_reject_4state_equality(mapper):
    """Test that wildcard equality operators are rejected."""
    expr_mapper, error_reporter = mapper
    parser = SVParser(SVToZuspecConfig(), error_reporter)
    
    code = """
    class test;
        function bit check(int a, int b);
            return a ==? b;
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


def _first(text, kind):
    reporter = ErrorReporter()
    parser = SVParser(SVToZuspecConfig(), reporter)
    parser.parse_text(text)
    found = []

    def visitor(symbol):
        if str(getattr(symbol, 'kind', '')) == kind:
            found.append(symbol)
        return True
    parser.get_root().visit(visitor)
    assert found
    return found[0]


def test_map_logical_and_or(mapper):
    """`&&` and `||` map to ExprBool."""
    from zuspec.ir.core.expr import BoolOp, ExprBool
    expr_mapper, error_reporter = mapper
    e = _first("""
    class test;
        function bit f(bit a, bit b, bit c);
            return a && (b || c);
        endfunction
    endclass
    """, 'ExpressionKind.BinaryOp')
    ir_expr = expr_mapper.map_expression(e)
    assert not error_reporter.has_errors()
    assert isinstance(ir_expr, ExprBool) and ir_expr.op == BoolOp.And
    assert isinstance(ir_expr.values[1], ExprBool) and ir_expr.values[1].op == BoolOp.Or


def test_map_conditional(mapper):
    """`c ? a : b` maps to ExprIfExp."""
    from zuspec.ir.core.expr import ExprIfExp
    expr_mapper, error_reporter = mapper
    e = _first("""
    class test;
        function int f(bit c, int a, int b);
            return c ? a : b;
        endfunction
    endclass
    """, 'ExpressionKind.ConditionalOp')
    ir_expr = expr_mapper.map_expression(e)
    assert not error_reporter.has_errors()
    assert isinstance(ir_expr, ExprIfExp)
    assert [x.name for x in (ir_expr.test, ir_expr.body, ir_expr.orelse)] == ["c", "a", "b"]
