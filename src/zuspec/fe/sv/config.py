"""Configuration for SystemVerilog to Zuspec IR translation."""
from dataclasses import dataclass, field
from typing import List


@dataclass
class SVToZuspecConfig:
    """Configuration for SV to Zuspec translation.
    
    Note: Some features are always errors regardless of configuration:
    - Modules
    - Interfaces

    4-state types (logic, reg, integer) map to ``DataTypeInt`` with
    ``four_state`` set.
    """
    
    strict_mode: bool = True
    """Error on unsupported constructs (always true for modules)"""
    
    ignore_covergroups: bool = True
    """Skip covergroup definitions"""
    
    ignore_assertions: bool = False
    """Skip assertion statements"""
    
    ignore_list: List[str] = field(default_factory=list)
    """Patterns to ignore (identifiers, not constructs)"""
    
    warn_only: bool = False
    """Warn instead of error (doesn't apply to modules)"""

    include_dirs: List[str] = field(default_factory=list)
    """Additional include directories for SV preprocessing"""
