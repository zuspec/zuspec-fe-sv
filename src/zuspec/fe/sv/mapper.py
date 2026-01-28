"""Main SV to Zuspec IR mapper orchestrator."""
from typing import List, Optional
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.data_type import DataTypeClass
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.stmt_mapper import StmtMapper
from zuspec.fe.sv.function_mapper import FunctionMapper
from zuspec.fe.sv.class_mapper import ClassMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter


class SVMapper:
    """Main orchestrator for mapping SystemVerilog to Zuspec IR.
    
    Coordinates the parsing and mapping of SystemVerilog constructs to
    Zuspec IR, focusing on 2-state types and class-based designs.
    """
    
    def __init__(self, config: Optional[SVToZuspecConfig] = None):
        self.config = config or SVToZuspecConfig()
        self.error_reporter = ErrorReporter()
        self.parser = SVParser(self.config, self.error_reporter)
        self.type_mapper = TypeMapper(self.config, self.error_reporter)
        self.expr_mapper = ExprMapper(self.config, self.error_reporter)
        self.stmt_mapper = StmtMapper(self.config, self.error_reporter, self.expr_mapper)
        self.function_mapper = FunctionMapper(
            self.config, self.error_reporter, self.type_mapper,
            self.expr_mapper, self.stmt_mapper
        )
        self.class_mapper = ClassMapper(
            self.config, self.error_reporter, self.type_mapper, self.function_mapper
        )
        self.classes: List[DataTypeClass] = []
    
    def map_text(self, text: str, file_name: str = "<string>") -> bool:
        """Map SystemVerilog text to Zuspec IR.
        
        Args:
            text: SystemVerilog source code
            file_name: Name for error reporting
            
        Returns:
            True if mapping succeeded, False otherwise
        """
        # Parse the text
        if not self.parser.parse_text(text, file_name):
            return False
        
        # Get the root and find all classes
        root = self.parser.get_root()
        if not root:
            self.error_reporter.error("Failed to get compilation root")
            return False
        
        # Collect all classes
        sv_classes = []
        def visitor(symbol):
            if hasattr(symbol, 'kind'):
                if str(symbol.kind) == 'SymbolKind.ClassType':
                    sv_classes.append(symbol)
            return True
        
        root.visit(visitor)
        
        # Map each class
        for sv_class in sv_classes:
            ir_class = self.class_mapper.map_class(sv_class)
            if ir_class:
                self.classes.append(ir_class)
        
        return not self.error_reporter.has_errors()
    
    def map_files(self, file_paths: List[str]) -> bool:
        """Map SystemVerilog files to Zuspec IR.
        
        Args:
            file_paths: List of file paths to parse
            
        Returns:
            True if mapping succeeded, False otherwise
        """
        # Parse the files
        if not self.parser.parse_files(file_paths):
            return False
        
        # Get the root and find all classes
        root = self.parser.get_root()
        if not root:
            self.error_reporter.error("Failed to get compilation root")
            return False
        
        # Collect all classes
        sv_classes = []
        def visitor(symbol):
            if hasattr(symbol, 'kind'):
                if str(symbol.kind) == 'SymbolKind.ClassType':
                    sv_classes.append(symbol)
            return True
        
        root.visit(visitor)
        
        # Map each class
        for sv_class in sv_classes:
            ir_class = self.class_mapper.map_class(sv_class)
            if ir_class:
                self.classes.append(ir_class)
        
        return not self.error_reporter.has_errors()
    
    def get_classes(self) -> List[DataTypeClass]:
        """Get all mapped classes."""
        return self.classes
    
    def get_error_report(self) -> str:
        """Get error report."""
        return self.error_reporter.report()
    
    def has_errors(self) -> bool:
        """Check if there were any errors."""
        return self.error_reporter.has_errors()
