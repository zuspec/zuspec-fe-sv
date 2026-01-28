"""Class mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.data_type import DataTypeClass
from zuspec.dataclasses.ir.fields import Field
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.type_mapper import TypeMapper


class ClassMapper:
    """Maps SystemVerilog classes to Zuspec IR DataTypeClass.
    
    Handles:
    - Class definitions
    - Class inheritance
    - Class members (fields)
    - Virtual classes
    """
    
    def __init__(
        self,
        config: SVToZuspecConfig,
        error_reporter: ErrorReporter,
        type_mapper: TypeMapper,
        function_mapper=None,  # Optional to avoid circular dependency
    ):
        self.config = config
        self.error_reporter = error_reporter
        self.type_mapper = type_mapper
        self.function_mapper = function_mapper
    
    def map_class(self, class_symbol: pyslang.ClassType) -> Optional[DataTypeClass]:
        """Map a SystemVerilog class to Zuspec IR.
        
        Args:
            class_symbol: The slang class symbol
            
        Returns:
            DataTypeClass or None on error
        """
        try:
            # Get class name
            class_name = str(class_symbol.name)
            
            # Create DataTypeClass
            ir_class = DataTypeClass(name=class_name, super=None)
            
            # Note: base class resolution would happen in a later phase
            # For now, super remains None
            # TODO: implement type resolution to set super to the actual DataTypeClass reference
            
            # Check if virtual class
            # Note: virtual/abstract info would be useful but DataTypeClass doesn't have metadata
            # For now, we skip this information
            # TODO: extend IR to support class modifiers or use a different approach
            
            # Map class members (fields) by visiting
            fields = self._map_fields(class_symbol)
            if fields is not None:
                ir_class.fields = fields
            
            # Map functions if function_mapper is available
            if self.function_mapper:
                functions = self.function_mapper.map_functions_from_class(class_symbol)
                if functions:
                    ir_class.functions = functions
            
            return ir_class
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping class: {str(e)}")
            return None
    
    def _map_fields(self, class_symbol: pyslang.ClassType) -> Optional[List[Field]]:
        """Map class fields/members.
        
        Args:
            class_symbol: The slang class symbol
            
        Returns:
            List of Field objects or None on error
        """
        fields = []
        
        try:
            # Collect class properties via visitor
            def visitor(symbol):
                if hasattr(symbol, 'kind'):
                    kind_str = str(symbol.kind)
                    # Only process ClassProperty symbols
                    if kind_str == 'SymbolKind.ClassProperty':
                        field = self._map_property_field(symbol)
                        if field:
                            fields.append(field)
                return True
            
            # Visit the class scope
            class_symbol.visit(visitor)
            
            return fields
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping fields: {str(e)}")
            return None
    
    def _map_property_field(self, property_symbol) -> Optional[Field]:
        """Map a class property to a Field.
        
        Args:
            property_symbol: The slang ClassPropertySymbol
            
        Returns:
            Field object or None on error
        """
        try:
            field_name = str(property_symbol.name)
            
            # Get the type
            var_type = property_symbol.type
            
            # Check if it's a 4-state type
            if hasattr(var_type, 'isFourState') and var_type.isFourState:
                self.error_reporter.error(
                    f"Field '{field_name}' uses 4-state type",
                    suggestion=f"Use 2-state types (bit, int, byte, etc.)",
                )
                return None
            
            # Map the type
            ir_type = self._map_type(var_type)
            if ir_type is None:
                return None
            
            # Create field
            field = Field(name=field_name, datatype=ir_type)
            
            # Note: rand qualifier would be stored in Field metadata if it existed
            # For now, we skip this since Field doesn't have a metadata attribute
            
            return field
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping field: {str(e)}")
            return None
    
    def _map_type(self, sv_type):
        """Map a slang type to Zuspec IR type.
        
        Args:
            sv_type: The slang type object
            
        Returns:
            Zuspec IR type or None on error
        """
        try:
            # Handle integral types
            if isinstance(sv_type, pyslang.IntegralType):
                # Get bit width
                width = sv_type.bitWidth if hasattr(sv_type, 'bitWidth') else None
                
                # Get signedness
                signed = sv_type.isSigned if hasattr(sv_type, 'isSigned') else None
                
                # For builtin types, use the type string
                type_str = str(sv_type)
                # Extract base type name (e.g., "int" from "int", "bit" from "bit[7:0]")
                if '[' in type_str:
                    type_name = type_str.split('[')[0].strip()
                else:
                    type_name = type_str
                
                return self.type_mapper.map_builtin_type(
                    type_name=type_name,
                    width=width,
                    signed=signed,
                )
            
            # For now, report other types as unsupported
            type_str = str(sv_type)
            self.error_reporter.error(f"Unsupported type: {type_str}")
            return None
            
        except Exception as e:
            self.error_reporter.error(f"Error mapping type: {str(e)}")
            return None
