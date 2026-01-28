"""Function/method mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.data_type import Function
from zuspec.dataclasses.ir.stmt import Arguments, Arg
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.stmt_mapper import StmtMapper


class FunctionMapper:
    """Maps SystemVerilog functions and tasks to Zuspec IR.
    
    Handles:
    - Function definitions with parameters and return types
    - Task definitions (mapped to async functions)
    - Function bodies (statements)
    - Virtual functions
    - Constructor functions (new)
    """
    
    def __init__(
        self,
        config: SVToZuspecConfig,
        error_reporter: ErrorReporter,
        type_mapper: TypeMapper,
        expr_mapper: ExprMapper,
        stmt_mapper: StmtMapper,
    ):
        self.config = config
        self.error_reporter = error_reporter
        self.type_mapper = type_mapper
        self.expr_mapper = expr_mapper
        self.stmt_mapper = stmt_mapper
    
    def map_function(self, sv_func) -> Optional[Function]:
        """Map a SystemVerilog function or task to Zuspec IR.
        
        Args:
            sv_func: The slang subroutine symbol (function or task)
            
        Returns:
            Function IR object or None on error
        """
        try:
            # Get function name
            func_name = str(sv_func.name) if hasattr(sv_func, 'name') else "unknown"
            
            # Check if it's a task (async) or function (sync)
            is_async = False
            if hasattr(sv_func, 'subroutineKind'):
                kind_str = str(sv_func.subroutineKind)
                is_async = 'Task' in kind_str
            
            # Map return type
            returns = None
            if hasattr(sv_func, 'returnType') and sv_func.returnType:
                # Get the actual type
                ret_type = sv_func.returnType
                if hasattr(ret_type, 'isVoid') and ret_type.isVoid:
                    returns = None
                else:
                    # Map the return type
                    returns = self._map_return_type(ret_type)
            
            # Map arguments
            args = self._map_arguments(sv_func)
            
            # Map function body
            body = []
            if hasattr(sv_func, 'body') and sv_func.body:
                body = self.stmt_mapper.map_statements(sv_func.body)
            
            # Check for virtual modifier
            metadata = {}
            if hasattr(sv_func, 'flags'):
                flags_str = str(sv_func.flags)
                if 'Virtual' in flags_str:
                    metadata['virtual'] = True
            
            # Create Function
            return Function(
                name=func_name,
                args=args,
                body=body,
                returns=returns,
                is_async=is_async,
                metadata=metadata,
                is_invariant=False,
                process_kind=None,
                sensitivity_list=[],
            )
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping function: {str(e)}")
            return None
    
    def _map_arguments(self, sv_func) -> Optional[Arguments]:
        """Map function/task arguments.
        
        Args:
            sv_func: The slang subroutine symbol
            
        Returns:
            Arguments object or None
        """
        try:
            args_list = []
            
            # Iterate through formal arguments
            # In slang, arguments are members of the subroutine
            if hasattr(sv_func, 'arguments'):
                for arg in sv_func.arguments:
                    arg_name = str(arg.name) if hasattr(arg, 'name') else "unnamed"
                    
                    # Get argument type annotation
                    annotation = None
                    if hasattr(arg, 'getType'):
                        arg_type = arg.getType()
                        # For now, we'll skip detailed type annotation
                        # Just store the name
                    
                    args_list.append(Arg(arg=arg_name, annotation=annotation))
            
            # Create Arguments object
            # For simplicity, put all args in the 'args' list (positional)
            return Arguments(
                posonlyargs=[],
                args=args_list,
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            )
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping arguments: {str(e)}")
            return None
    
    def _map_return_type(self, sv_type):
        """Map return type to Zuspec IR DataType.
        
        Args:
            sv_type: The slang type object
            
        Returns:
            DataType or None
        """
        try:
            # Use type mapper for integral types
            if isinstance(sv_type, pyslang.IntegralType):
                type_str = str(sv_type)
                if '[' in type_str:
                    type_name = type_str.split('[')[0].strip()
                else:
                    type_name = type_str
                
                width = sv_type.bitWidth if hasattr(sv_type, 'bitWidth') else None
                signed = sv_type.isSigned if hasattr(sv_type, 'isSigned') else None
                
                return self.type_mapper.map_builtin_type(
                    type_name=type_name,
                    width=width,
                    signed=signed,
                )
            
            # For now, just skip unsupported types with a warning instead of error
            # This allows us to continue mapping other functions
            self.error_reporter.warning(f"Unsupported return type: {sv_type}")
            return None
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping return type: {str(e)}")
            return None
    
    def map_functions_from_class(self, class_symbol) -> List[Function]:
        """Map all functions/tasks from a class.
        
        Args:
            class_symbol: The slang class symbol
            
        Returns:
            List of Function objects
        """
        functions = []
        
        try:
            # Visit class members to find subroutines
            def visitor(symbol):
                if hasattr(symbol, 'kind'):
                    kind_str = str(symbol.kind)
                    if kind_str == 'SymbolKind.Subroutine':
                        func = self.map_function(symbol)
                        if func:
                            functions.append(func)
                return True
            
            # Visit the class to find functions
            class_symbol.visit(visitor)
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping functions from class: {str(e)}")
        
        return functions
