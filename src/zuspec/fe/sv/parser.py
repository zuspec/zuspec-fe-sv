"""SystemVerilog parser wrapper using slang library."""
from typing import List, Optional
import os
import pyslang
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter

_SHARE_DIR = os.path.join(os.path.dirname(__file__), 'share')
_LIB_FILES = [
    os.path.join(_SHARE_DIR, 'zsp_clock_if.sv'),
    os.path.join(_SHARE_DIR, 'zsp_reset_if.sv'),
    os.path.join(_SHARE_DIR, 'zsp_reg_if.sv'),
    os.path.join(_SHARE_DIR, 'zsp_pkg.sv'),
]


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
        """Parse SystemVerilog files, pre-loading the zsp library.
        
        Args:
            file_paths: List of file paths to parse
            
        Returns:
            True if parsing succeeded, False otherwise
        """
        try:
            sm = pyslang.SourceManager()
            sm.addUserDirectories(_SHARE_DIR)
            for d in self.config.include_dirs:
                sm.addUserDirectories(d)

            self.compilation = pyslang.Compilation()
            # Keep strong references to all SyntaxTree objects so they aren't
            # garbage-collected (the C++ compilation holds raw pointers into them).
            self._trees = []

            for lib_path in _LIB_FILES:
                t = pyslang.SyntaxTree.fromFile(lib_path, sm)
                self._trees.append(t)
                self.compilation.addSyntaxTree(t)

            for file_path in file_paths:
                t = pyslang.SyntaxTree.fromFile(file_path, sm)
                self._trees.append(t)
                self.compilation.addSyntaxTree(t)

            # Report errors only (suppress warnings for library code)
            for diag in self.compilation.getAllDiagnostics():
                if diag.isError():
                    self.error_reporter.error(f"SV parse error: {diag.code}")

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
            for diag in self.compilation.getAllDiagnostics():
                if diag.isError():
                    self.error_reporter.error(
                        message=f"Diagnostic code: {diag.code}",
                        file_path=file_name,
                    )
                else:
                    self.error_reporter.warning(
                        message=f"Diagnostic code: {diag.code}",
                        file_path=file_name,
                    )
            
            return not self.error_reporter.has_errors()
            
        except Exception as e:
            self.error_reporter.error(f"Parser error: {str(e)}")
            return False
    
    def get_root(self) -> Optional[pyslang.RootSymbol]:
        """Get the root symbol of the compilation."""
        if self.compilation:
            return self.compilation.getRoot()
        return None
