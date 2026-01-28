"""Configuration for SystemVerilog to Zuspec IR translation."""
from dataclasses import dataclass, field
from typing import List


@dataclass
class SVToZuspecConfig:
    """Configuration for SV to Zuspec translation.
    
    Note: Some features are always errors regardless of configuration:
    - 4-state types (logic, reg, integer) 
    - 4-state operators (===, !==)
    - Modules
    - Interfaces
    """
    
    strict_mode: bool = True
    """Error on unsupported constructs (always true for 4-state/modules)"""
    
    ignore_covergroups: bool = True
    """Skip covergroup definitions"""
    
    ignore_assertions: bool = False
    """Skip assertion statements"""
    
    ignore_list: List[str] = field(default_factory=list)
    """Patterns to ignore (identifiers, not constructs)"""
    
    warn_only: bool = False
    """Warn instead of error (doesn't apply to critical features like 4-state)"""
