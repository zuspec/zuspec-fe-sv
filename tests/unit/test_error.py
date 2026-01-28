"""Unit tests for error reporting module."""
import pytest
from zuspec.fe.sv.error import ErrorReporter, ErrorSeverity, TranslationError


def test_error_reporter_empty():
    """Test empty error reporter."""
    reporter = ErrorReporter()
    
    assert not reporter.has_errors()
    assert len(reporter.get_errors()) == 0
    assert len(reporter.get_warnings()) == 0


def test_error_reporter_error():
    """Test reporting an error."""
    reporter = ErrorReporter()
    reporter.error("Test error", file_path="test.sv", line=10, column=5)
    
    assert reporter.has_errors()
    assert len(reporter.get_errors()) == 1
    assert len(reporter.get_warnings()) == 0
    
    error = reporter.get_errors()[0]
    assert error.severity == ErrorSeverity.ERROR
    assert error.message == "Test error"
    assert error.file_path == "test.sv"
    assert error.line == 10
    assert error.column == 5


def test_error_reporter_warning():
    """Test reporting a warning."""
    reporter = ErrorReporter()
    reporter.warning("Test warning")
    
    assert not reporter.has_errors()
    assert len(reporter.get_errors()) == 0
    assert len(reporter.get_warnings()) == 1
    
    warning = reporter.get_warnings()[0]
    assert warning.severity == ErrorSeverity.WARNING
    assert warning.message == "Test warning"


def test_error_reporter_mixed():
    """Test mixed errors and warnings."""
    reporter = ErrorReporter()
    reporter.error("Error 1")
    reporter.warning("Warning 1")
    reporter.error("Error 2")
    
    assert reporter.has_errors()
    assert len(reporter.get_errors()) == 2
    assert len(reporter.get_warnings()) == 1


def test_error_reporter_clear():
    """Test clearing errors."""
    reporter = ErrorReporter()
    reporter.error("Test error")
    reporter.warning("Test warning")
    
    assert reporter.has_errors()
    
    reporter.clear()
    
    assert not reporter.has_errors()
    assert len(reporter.get_errors()) == 0
    assert len(reporter.get_warnings()) == 0


def test_translation_error_str():
    """Test error string formatting."""
    error = TranslationError(
        severity=ErrorSeverity.ERROR,
        message="4-state logic not supported",
        file_path="test.sv",
        line=10,
        column=5,
        context="logic [31:0] data",
        suggestion="Use 2-state types: bit [31:0] data",
    )
    
    error_str = str(error)
    assert "ERROR" in error_str
    assert "4-state logic not supported" in error_str
    assert "test.sv:10:5" in error_str
    assert "logic [31:0] data" in error_str
    assert "Use 2-state types" in error_str


def test_error_reporter_report():
    """Test generating a report."""
    reporter = ErrorReporter()
    reporter.error("Error 1", suggestion="Fix 1")
    reporter.warning("Warning 1")
    
    report = reporter.report()
    assert "ERROR: Error 1" in report
    assert "WARNING: Warning 1" in report
    assert "Fix 1" in report
