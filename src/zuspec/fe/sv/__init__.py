"""SystemVerilog front end for Zuspec.

``compile`` / ``find_class`` / ``class_from_typedef`` / ``extern_stub_bodies``
come from :mod:`zuspec.fe.sv.compiler`; the class interpreter is
:mod:`zuspec.fe.sv.interp`.
"""
from .compiler import (CompileError, CompileResult, Diagnostic, Diagnostics,
                       class_from_typedef, compile, extern_stub_bodies, find_class)
