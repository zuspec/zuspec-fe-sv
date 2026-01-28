"""SystemVerilog parser wrapper using slang library."""
from typing import List, Optional
import pyslang
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter


class SVParser:
    """Wrapper around slang SystemVerilog parser.
    
    Handles parsing SystemVerilog files and provides access to the AST.
    Macros are expanded by slang's preprocessor.
    """
    
    def __init__(self, config: SVToZuspecConfig, error_reporter: ErrorReporter):
        self.config = config
        self.error_reporter = error_reporter
        self.compilation: Optional[pyslang.Compilation] = None
    
    def parse_files(self, file_paths: List[str]) -> bool:
        """Parse SystemVerilog files.
        
        Args:
            file_paths: List of file paths to parse
            
        Returns:
            True if parsing succeeded, False otherwise
        """
        try:
            # Create compilation with default options
            self.compilation = pyslang.Compilation()
            
            # Add files to compilation
            for file_path in file_paths:
                self.compilation.addSyntaxTree(
                    pyslang.SyntaxTree.fromFile(file_path)
                )
            
            # Check for diagnostics (errors/warnings)
            diagnostics = self.compilation.getAllDiagnostics()
            
            for diag in diagnostics:
                # Get location info
                location = diag.location
                file_path = None
                line = None
                column = None
                
                if location:
                    if hasattr(location, 'buffer') and location.buffer:
                        file_path = str(location.buffer.name)
                
                # Get message - just use code for now since formattedMessage doesn't exist
                message = f"Diagnostic code: {diag.code}"
                
                if diag.isError():
                    self.error_reporter.error(
                        message=message,
                        file_path=file_path,
                        line=line,
                        column=column,
                    )
                else:
                    self.error_reporter.warning(
                        message=message,
                        file_path=file_path,
                        line=line,
                        column=column,
                    )
            
            return not self.error_reporter.has_errors()
            
        except Exception as e:
            self.error_reporter.error(f"Parser error: {str(e)}")
            return False
    
    def parse_text(self, text: str, file_name: str = "<string>") -> bool:
        """Parse SystemVerilog text.
        
        Args:
            text: SystemVerilog source code
            file_name: Name to use for error reporting
            
        Returns:
            True if parsing succeeded, False otherwise
        """
        try:
            # Create compilation
            self.compilation = pyslang.Compilation()
            
            # Add source text
            tree = pyslang.SyntaxTree.fromText(text, file_name)
            self.compilation.addSyntaxTree(tree)
            
            # Check for diagnostics
            diagnostics = self.compilation.getAllDiagnostics()
            
            for diag in diagnostics:
                message = f"Diagnostic code: {diag.code}"
                
                if diag.isError():
                    self.error_reporter.error(message=message, file_path=file_name)
                else:
                    self.error_reporter.warning(message=message, file_path=file_name)
            
            return not self.error_reporter.has_errors()
            
        except Exception as e:
            self.error_reporter.error(f"Parser error: {str(e)}")
            return False
    
    def get_root(self) -> Optional[pyslang.RootSymbol]:
        """Get the root symbol of the compilation.
        
        Returns:
            Root symbol or None if not parsed
        """
        if self.compilation:
            return self.compilation.getRoot()
        return None
