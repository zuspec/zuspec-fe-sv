"""Error reporting for SystemVerilog to Zuspec IR translation."""
from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional


class ErrorSeverity(Enum):
    """Severity level for translation errors."""
    WARNING = auto()
    ERROR = auto()


@dataclass
class TranslationError:
    """Represents a translation error or warning."""
    
    severity: ErrorSeverity
    message: str
    file_path: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    context: Optional[str] = None
    suggestion: Optional[str] = None
    
    def __str__(self) -> str:
        """Format error message for display."""
        severity_str = "ERROR" if self.severity == ErrorSeverity.ERROR else "WARNING"
        
        location = ""
        if self.file_path:
            location = f" at {self.file_path}"
            if self.line is not None:
                location += f":{self.line}"
                if self.column is not None:
                    location += f":{self.column}"
        
        msg = f"{severity_str}: {self.message}{location}"
        
        if self.context:
            msg += f"\n  Context: {self.context}"
        
        if self.suggestion:
            msg += f"\n  Suggestion: {self.suggestion}"
        
        return msg


class ErrorReporter:
    """Collects and reports translation errors."""
    
    def __init__(self):
        self.errors: List[TranslationError] = []
    
    def error(
        self,
        message: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
        context: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        """Report an error."""
        self.errors.append(
            TranslationError(
                severity=ErrorSeverity.ERROR,
                message=message,
                file_path=file_path,
                line=line,
                column=column,
                context=context,
                suggestion=suggestion,
            )
        )
    
    def warning(
        self,
        message: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
        context: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        """Report a warning."""
        self.errors.append(
            TranslationError(
                severity=ErrorSeverity.WARNING,
                message=message,
                file_path=file_path,
                line=line,
                column=column,
                context=context,
                suggestion=suggestion,
            )
        )
    
    def has_errors(self) -> bool:
        """Check if any errors were reported."""
        return any(e.severity == ErrorSeverity.ERROR for e in self.errors)
    
    def get_errors(self) -> List[TranslationError]:
        """Get all errors."""
        return [e for e in self.errors if e.severity == ErrorSeverity.ERROR]
    
    def get_warnings(self) -> List[TranslationError]:
        """Get all warnings."""
        return [e for e in self.errors if e.severity == ErrorSeverity.WARNING]
    
    def clear(self) -> None:
        """Clear all errors and warnings."""
        self.errors.clear()
    
    def report(self) -> str:
        """Generate a report of all errors and warnings."""
        if not self.errors:
            return "No errors or warnings"
        
        lines = []
        for error in self.errors:
            lines.append(str(error))
        
        return "\n\n".join(lines)
