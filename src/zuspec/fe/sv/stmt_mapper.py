"""Statement mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.stmt import (
    Stmt,
    StmtAssign,
    StmtIf,
    StmtFor,
    StmtWhile,
    StmtReturn,
    StmtBreak,
    StmtContinue,
    StmtExpr,
)
from zuspec.dataclasses.ir.expr import ExprRefLocal
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.expr_mapper import ExprMapper


class StmtMapper:
    """Maps SystemVerilog statements to Zuspec IR.
    
    Handles:
    - Assignments (blocking and non-blocking)
    - Conditionals (if/else)
    - Loops (for, while, foreach)
    - Return statements
    - Break/continue
    - Expression statements
    """
    
    def __init__(
        self,
        config: SVToZuspecConfig,
        error_reporter: ErrorReporter,
        expr_mapper: ExprMapper,
    ):
        self.config = config
        self.error_reporter = error_reporter
        self.expr_mapper = expr_mapper
    
    def map_statement(self, sv_stmt) -> Optional[Stmt]:
        """Map a SystemVerilog statement to Zuspec IR.
        
        Args:
            sv_stmt: The slang statement object
            
        Returns:
            Zuspec IR Stmt or None on error
        """
        if sv_stmt is None:
            return None
        
        try:
            # Get statement kind
            stmt_kind = str(sv_stmt.kind) if hasattr(sv_stmt, 'kind') else None
            
            if not stmt_kind:
                self.error_reporter.error(f"Unknown statement type: {type(sv_stmt)}")
                return None
            
            # Assignment statement
            if stmt_kind == 'StatementKind.ExpressionStatement':
                return self._map_expression_statement(sv_stmt)
            
            # Return statement
            elif stmt_kind == 'StatementKind.Return':
                return self._map_return(sv_stmt)
            
            # Conditional statement
            elif stmt_kind == 'StatementKind.Conditional':
                return self._map_conditional(sv_stmt)
            
            # For loop
            elif stmt_kind == 'StatementKind.ForLoop':
                return self._map_for_loop(sv_stmt)
            
            # While/do-while loop
            elif stmt_kind in ['StatementKind.WhileLoop', 'StatementKind.DoWhileLoop']:
                return self._map_while_loop(sv_stmt)
            
            # Break statement
            elif stmt_kind == 'StatementKind.Break':
                return StmtBreak()
            
            # Continue statement
            elif stmt_kind == 'StatementKind.Continue':
                return StmtContinue()
            
            # Block (list of statements)
            elif stmt_kind == 'StatementKind.Block':
                # Return list of mapped statements
                return self._map_block(sv_stmt)
            
            # Statement list
            elif stmt_kind == 'StatementKind.List':
                return self._map_statement_list(sv_stmt)
            
            else:
                self.error_reporter.error(f"Unsupported statement kind: {stmt_kind}")
                return None
                
        except Exception as e:
            self.error_reporter.error(f"Error mapping statement: {str(e)}")
            return None
    
    def map_statements(self, sv_stmts) -> List[Stmt]:
        """Map a list of SystemVerilog statements.
        
        Args:
            sv_stmts: List or iterator of slang statement objects
            
        Returns:
            List of Zuspec IR Stmt objects
        """
        stmts = []
        
        try:
            if sv_stmts is None:
                return stmts
            
            # Handle different types of statement containers
            if hasattr(sv_stmts, '__iter__'):
                for sv_stmt in sv_stmts:
                    mapped = self.map_statement(sv_stmt)
                    if mapped:
                        # If it's a list, extend; otherwise append
                        if isinstance(mapped, list):
                            stmts.extend(mapped)
                        else:
                            stmts.append(mapped)
            else:
                # Single statement
                mapped = self.map_statement(sv_stmts)
                if mapped:
                    if isinstance(mapped, list):
                        stmts.extend(mapped)
                    else:
                        stmts.append(mapped)
        
        except Exception as e:
            self.error_reporter.error(f"Error mapping statements: {str(e)}")
        
        return stmts
    
    def _map_expression_statement(self, sv_stmt) -> Optional[Stmt]:
        """Map expression statement (including assignments)."""
        try:
            # Get the expression
            if not hasattr(sv_stmt, 'expr'):
                return None
            
            sv_expr = sv_stmt.expr
            expr_kind = str(sv_expr.kind) if hasattr(sv_expr, 'kind') else None
            
            # Check if it's an assignment
            if expr_kind == 'ExpressionKind.Assignment':
                return self._map_assignment(sv_expr)
            
            # Otherwise, it's just an expression statement
            expr = self.expr_mapper.map_expression(sv_expr)
            if expr:
                return StmtExpr(expr=expr)
            
            return None
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping expression statement: {str(e)}")
            return None
    
    def _map_assignment(self, sv_expr) -> Optional[StmtAssign]:
        """Map assignment expression."""
        try:
            # Get left side (target)
            if not hasattr(sv_expr, 'left'):
                self.error_reporter.error("Assignment missing left side")
                return None
            
            target_expr = self.expr_mapper.map_expression(sv_expr.left)
            if not target_expr:
                return None
            
            # Get right side (value)
            if not hasattr(sv_expr, 'right'):
                self.error_reporter.error("Assignment missing right side")
                return None
            
            value_expr = self.expr_mapper.map_expression(sv_expr.right)
            if not value_expr:
                return None
            
            # Create assignment
            return StmtAssign(targets=[target_expr], value=value_expr)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping assignment: {str(e)}")
            return None
    
    def _map_return(self, sv_stmt) -> Optional[StmtReturn]:
        """Map return statement."""
        try:
            # Get return value expression
            value_expr = None
            if hasattr(sv_stmt, 'expr') and sv_stmt.expr:
                value_expr = self.expr_mapper.map_expression(sv_stmt.expr)
            
            return StmtReturn(value=value_expr)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping return: {str(e)}")
            return None
    
    def _map_conditional(self, sv_stmt) -> Optional[StmtIf]:
        """Map if/else statement."""
        try:
            # Get condition
            if not hasattr(sv_stmt, 'conditions') or not sv_stmt.conditions:
                self.error_reporter.error("Conditional missing condition")
                return None
            
            # Get first condition (TODO: handle multiple conditions for else-if)
            conditions = list(sv_stmt.conditions)
            if not conditions:
                return None
            
            first_cond = conditions[0]
            
            # Map test expression
            test_expr = self.expr_mapper.map_expression(first_cond.expr)
            if not test_expr:
                return None
            
            # Map then body
            body = []
            if hasattr(first_cond, 'stmt') and first_cond.stmt:
                body = self.map_statements(first_cond.stmt)
            
            # Map else body
            orelse = []
            if hasattr(sv_stmt, 'elseClause') and sv_stmt.elseClause:
                orelse = self.map_statements(sv_stmt.elseClause)
            
            return StmtIf(test=test_expr, body=body, orelse=orelse)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping conditional: {str(e)}")
            return None
    
    def _map_for_loop(self, sv_stmt) -> Optional[StmtFor]:
        """Map for loop."""
        try:
            # Get loop variable initialization
            # In SystemVerilog: for (int i = 0; i < n; i++)
            # We need to extract: target, iter, body
            
            # For now, create a simplified mapping
            # TODO: Properly handle SV for loop semantics
            
            # Create a dummy target and iter
            target = ExprRefLocal(name="i")  # Placeholder
            iter_expr = ExprRefLocal(name="range")  # Placeholder
            
            # Map body
            body = []
            if hasattr(sv_stmt, 'body') and sv_stmt.body:
                body = self.map_statements(sv_stmt.body)
            
            return StmtFor(target=target, iter=iter_expr, body=body, orelse=[])
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping for loop: {str(e)}")
            return None
    
    def _map_while_loop(self, sv_stmt) -> Optional[StmtWhile]:
        """Map while loop."""
        try:
            # Get condition
            if not hasattr(sv_stmt, 'cond'):
                self.error_reporter.error("While loop missing condition")
                return None
            
            test_expr = self.expr_mapper.map_expression(sv_stmt.cond)
            if not test_expr:
                return None
            
            # Map body
            body = []
            if hasattr(sv_stmt, 'body') and sv_stmt.body:
                body = self.map_statements(sv_stmt.body)
            
            return StmtWhile(test=test_expr, body=body, orelse=[])
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping while loop: {str(e)}")
            return None
    
    def _map_block(self, sv_stmt) -> List[Stmt]:
        """Map block statement (begin/end)."""
        try:
            body = []
            if hasattr(sv_stmt, 'body') and sv_stmt.body:
                body = self.map_statements(sv_stmt.body)
            return body
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping block: {str(e)}")
            return []
    
    def _map_statement_list(self, sv_stmt) -> List[Stmt]:
        """Map statement list."""
        try:
            stmts = []
            if hasattr(sv_stmt, 'list'):
                for s in sv_stmt.list:
                    mapped = self.map_statement(s)
                    if mapped:
                        if isinstance(mapped, list):
                            stmts.extend(mapped)
                        else:
                            stmts.append(mapped)
            return stmts
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping statement list: {str(e)}")
            return []
