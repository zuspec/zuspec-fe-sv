"""Type mapping from SystemVerilog to Zuspec IR.

This module handles mapping of SystemVerilog types to Zuspec IR types.
Only 2-state types are supported.
"""
from typing import Optional
import sys
import os

# Add packages to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.data_type import (
    DataTypeInt,
    DataTypeString,
    DataTypeEnum,
    DataTypeStruct,
)
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig


class TypeMapper:
    """Maps SystemVerilog types to Zuspec IR types.
    
    Only supports 2-state types:
    - bit, int, byte, shortint, longint (supported)
    - logic, reg, integer (error - 4-state)
    """
    
    def __init__(self, config: SVToZuspecConfig, error_reporter: ErrorReporter):
        self.config = config
        self.error_reporter = error_reporter
    
    def map_builtin_type(
        self,
        type_name: str,
        width: Optional[int] = None,
        signed: Optional[bool] = None,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ) -> Optional[DataTypeInt]:
        """Map a SystemVerilog builtin type to Zuspec IR.
        
        Args:
            type_name: The SV type name (e.g., 'bit', 'int', 'logic')
            width: Optional width override for bit vectors
            signed: Optional signed override
            file_path: Source file for error reporting
            line: Line number for error reporting
            column: Column number for error reporting
            
        Returns:
            DataTypeInt if successful, None on error
        """
        type_name_lower = type_name.lower()
        
        # Check for 4-state types (always error)
        if type_name_lower in ['logic', 'reg', 'integer']:
            self.error_reporter.error(
                f"4-state type '{type_name}' not supported",
                file_path=file_path,
                line=line,
                column=column,
                context=f"{type_name}",
                suggestion=self._suggest_2state_replacement(type_name_lower),
            )
            return None
        
        # Map 2-state types
        if type_name_lower == 'bit':
            bits = width if width is not None else 1
            is_signed = signed if signed is not None else False
            return DataTypeInt(bits=bits, signed=is_signed)
        
        elif type_name_lower == 'byte':
            return DataTypeInt(bits=8, signed=True)
        
        elif type_name_lower == 'shortint':
            return DataTypeInt(bits=16, signed=True)
        
        elif type_name_lower == 'int':
            return DataTypeInt(bits=32, signed=True)
        
        elif type_name_lower == 'longint':
            return DataTypeInt(bits=64, signed=True)
        
        # Unsupported type
        self.error_reporter.error(
            f"Unsupported type '{type_name}'",
            file_path=file_path,
            line=line,
            column=column,
        )
        return None
    
    def _suggest_2state_replacement(self, type_name: str) -> str:
        """Suggest a 2-state replacement for a 4-state type."""
        replacements = {
            'logic': 'Use 2-state type: bit',
            'reg': 'Use 2-state type: bit',
            'integer': 'Use 2-state type: int',
        }
        return replacements.get(type_name, 'Use a 2-state type (bit, int, byte, etc.)')
    
    def is_4state_type(self, type_name: str) -> bool:
        """Check if a type name is a 4-state type."""
        return type_name.lower() in ['logic', 'reg', 'integer']
