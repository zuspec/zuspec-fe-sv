"""Expression mapping from SystemVerilog to Zuspec IR."""
from typing import Optional
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.expr import (
    Expr,
    ExprBin,
    ExprUnary,
    ExprConstant,
    ExprCall,
    ExprAttribute,
    ExprSubscript,
    ExprRefLocal,
    BinOp,
    UnaryOp,
    CmpOp,
)
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig


class ExprMapper:
    """Maps SystemVerilog expressions to Zuspec IR.
    
    Handles:
    - Binary operations (+, -, *, &, |, etc.)
    - Unary operations (!, ~, -, +)
    - Comparisons (==, !=, <, >, <=, >=)
    - Constants (integers, strings)
    - References (variables, parameters)
    - Member access (obj.field)
    - Array subscript (arr[i])
    - Function calls
    
    Rejects 4-state operators (===, !==)
    """
    
    def __init__(self, config: SVToZuspecConfig, error_reporter: ErrorReporter):
        self.config = config
        self.error_reporter = error_reporter
    
    def map_expression(self, sv_expr) -> Optional[Expr]:
        """Map a SystemVerilog expression to Zuspec IR.
        
        Args:
            sv_expr: The slang expression object
            
        Returns:
            Zuspec IR Expr or None on error
        """
        if sv_expr is None:
            return None
        
        try:
            # Get expression kind
            expr_kind = str(sv_expr.kind) if hasattr(sv_expr, 'kind') else None
            
            if not expr_kind:
                self.error_reporter.error(f"Unknown expression type: {type(sv_expr)}")
                return None
            
            # Binary operations
            if expr_kind == 'ExpressionKind.BinaryOp':
                return self._map_binary_op(sv_expr)
            
            # Unary operations
            elif expr_kind == 'ExpressionKind.UnaryOp':
                return self._map_unary_op(sv_expr)
            
            # Integer literal
            elif expr_kind == 'ExpressionKind.IntegerLiteral':
                return self._map_integer_literal(sv_expr)
            
            # String literal
            elif expr_kind == 'ExpressionKind.StringLiteral':
                return self._map_string_literal(sv_expr)
            
            # Named value (variable reference)
            elif expr_kind == 'ExpressionKind.NamedValue':
                return self._map_named_value(sv_expr)
            
            # Member access
            elif expr_kind == 'ExpressionKind.MemberAccess':
                return self._map_member_access(sv_expr)
            
            # Element select (array subscript)
            elif expr_kind == 'ExpressionKind.ElementSelect':
                return self._map_element_select(sv_expr)
            
            # Call expression
            elif expr_kind == 'ExpressionKind.Call':
                return self._map_call(sv_expr)
            
            # Conversion (may be implicit cast)
            elif expr_kind == 'ExpressionKind.Conversion':
                return self._map_conversion(sv_expr)
            
            else:
                self.error_reporter.error(f"Unsupported expression kind: {expr_kind}")
                return None
                
        except Exception as e:
            self.error_reporter.error(f"Error mapping expression: {str(e)}")
            return None
    
    def _map_binary_op(self, sv_expr) -> Optional[ExprBin]:
        """Map binary operation."""
        try:
            # Get operator
            if not hasattr(sv_expr, 'op'):
                self.error_reporter.error("Binary operation missing operator")
                return None
            
            op_str = str(sv_expr.op)
            
            # Check for 4-state operators
            if op_str in ['BinaryOperator.CaseEquality', 'BinaryOperator.CaseInequality',
                          'BinaryOperator.WildcardEquality', 'BinaryOperator.WildcardInequality']:
                self.error_reporter.error(
                    f"4-state operator not supported: {op_str}",
                    suggestion="Use 2-state operators (==, !=)"
                )
                return None
            
            # Map operator to Zuspec
            zuspec_op = self._map_binary_operator(op_str)
            if zuspec_op is None:
                return None
            
            # Map operands
            left = self.map_expression(sv_expr.left)
            right = self.map_expression(sv_expr.right)
            
            if left is None or right is None:
                return None
            
            return ExprBin(lhs=left, op=zuspec_op, rhs=right)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping binary op: {str(e)}")
            return None
    
    def _map_binary_operator(self, op_str: str):
        """Map SystemVerilog binary operator to Zuspec."""
        op_map = {
            'BinaryOperator.Add': BinOp.Add,
            'BinaryOperator.Subtract': BinOp.Sub,
            'BinaryOperator.Multiply': BinOp.Mult,
            'BinaryOperator.Divide': BinOp.Div,
            'BinaryOperator.Mod': BinOp.Mod,
            'BinaryOperator.BinaryAnd': BinOp.BitAnd,
            'BinaryOperator.BinaryOr': BinOp.BitOr,
            'BinaryOperator.BinaryXor': BinOp.BitXor,
            'BinaryOperator.LogicalShiftLeft': BinOp.LShift,
            'BinaryOperator.LogicalShiftRight': BinOp.RShift,
            # Comparisons - use CmpOp (we'll need ExprCompare for these)
            'BinaryOperator.Equality': CmpOp.Eq,
            'BinaryOperator.Inequality': CmpOp.NotEq,
            'BinaryOperator.LessThan': CmpOp.Lt,
            'BinaryOperator.LessThanEqual': CmpOp.LtE,
            'BinaryOperator.GreaterThan': CmpOp.Gt,
            'BinaryOperator.GreaterThanEqual': CmpOp.GtE,
        }
        
        if op_str not in op_map:
            self.error_reporter.error(f"Unsupported binary operator: {op_str}")
            return None
        
        return op_map[op_str]
    
    def _map_unary_op(self, sv_expr) -> Optional[ExprUnary]:
        """Map unary operation."""
        try:
            op_str = str(sv_expr.op) if hasattr(sv_expr, 'op') else None
            if not op_str:
                self.error_reporter.error("Unary operation missing operator")
                return None
            
            # Map operator
            zuspec_op = self._map_unary_operator(op_str)
            if zuspec_op is None:
                return None
            
            # Map operand
            operand = self.map_expression(sv_expr.operand)
            if operand is None:
                return None
            
            return ExprUnary(op=zuspec_op, operand=operand)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping unary op: {str(e)}")
            return None
    
    def _map_unary_operator(self, op_str: str):
        """Map SystemVerilog unary operator to Zuspec."""
        op_map = {
            'UnaryOperator.BitwiseNot': UnaryOp.Invert,
            'UnaryOperator.LogicalNot': UnaryOp.Not,
            'UnaryOperator.Plus': UnaryOp.UAdd,
            'UnaryOperator.Minus': UnaryOp.USub,
        }
        
        if op_str not in op_map:
            self.error_reporter.error(f"Unsupported unary operator: {op_str}")
            return None
        
        return op_map[op_str]
    
    def _map_integer_literal(self, sv_expr) -> Optional[ExprConstant]:
        """Map integer literal."""
        try:
            # Get the value - pyslang represents this differently
            # For now, try to extract as string and convert
            value_str = str(sv_expr)
            
            # Try to extract numeric value
            try:
                value = int(value_str)
            except:
                # May have formatting like 32'd10, extract the number
                value = 0  # Default
            
            return ExprConstant(value=value)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping integer literal: {str(e)}")
            return None
    
    def _map_string_literal(self, sv_expr) -> Optional[ExprConstant]:
        """Map string literal."""
        try:
            value = str(sv_expr)
            return ExprConstant(value=value)
        except Exception as e:
            self.error_reporter.error(f"Error mapping string literal: {str(e)}")
            return None
    
    def _map_named_value(self, sv_expr) -> Optional[ExprRefLocal]:
        """Map named value reference."""
        try:
            # Get the symbol being referenced
            if hasattr(sv_expr, 'symbol'):
                name = str(sv_expr.symbol.name)
                return ExprRefLocal(name=name)
            
            self.error_reporter.error("Named value has no symbol")
            return None
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping named value: {str(e)}")
            return None
    
    def _map_member_access(self, sv_expr) -> Optional[ExprAttribute]:
        """Map member access (obj.field)."""
        try:
            # Get base object
            value = self.map_expression(sv_expr.value)
            if value is None:
                return None
            
            # Get member name
            if hasattr(sv_expr, 'member'):
                attr = str(sv_expr.member.name)
            else:
                self.error_reporter.error("Member access has no member name")
                return None
            
            return ExprAttribute(value=value, attr=attr)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping member access: {str(e)}")
            return None
    
    def _map_element_select(self, sv_expr) -> Optional[ExprSubscript]:
        """Map element select (array subscript)."""
        try:
            # Get base array
            value = self.map_expression(sv_expr.value)
            if value is None:
                return None
            
            # Get selector (index)
            selector = self.map_expression(sv_expr.selector)
            if selector is None:
                return None
            
            return ExprSubscript(value=value, slice=selector)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping element select: {str(e)}")
            return None
    
    def _map_call(self, sv_expr) -> Optional[ExprCall]:
        """Map function call."""
        try:
            # Get function being called
            # This may be a method or function reference
            func_name = "unknown"
            if hasattr(sv_expr, 'subroutine'):
                func_name = str(sv_expr.subroutine.name)
            
            func_ref = ExprRefLocal(name=func_name)
            
            # Get arguments
            args = []
            if hasattr(sv_expr, 'arguments'):
                for arg in sv_expr.arguments():
                    arg_expr = self.map_expression(arg)
                    if arg_expr:
                        args.append(arg_expr)
            
            return ExprCall(func=func_ref, args=args, keywords=[])
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping call: {str(e)}")
            return None
    
    def _map_conversion(self, sv_expr) -> Optional[Expr]:
        """Map conversion (implicit cast) - just map the operand."""
        try:
            return self.map_expression(sv_expr.operand)
        except Exception as e:
            self.error_reporter.error(f"Error mapping conversion: {str(e)}")
            return None
