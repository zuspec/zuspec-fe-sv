"""Main SV to Zuspec IR mapper orchestrator."""
from typing import List, Optional
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.ir.core.data_type import DataTypeClass, DataTypeComponent, DataTypeAction
from zuspec.fe.sv.parser import SVParser
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.expr_mapper import ExprMapper
from zuspec.fe.sv.stmt_mapper import StmtMapper
from zuspec.fe.sv.function_mapper import FunctionMapper
from zuspec.fe.sv.class_mapper import ClassMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.error import ErrorReporter


def _is_library_class(sym) -> bool:
    """True if this class should be skipped (library / template specialization)."""
    try:
        name = sym.name
    except UnicodeDecodeError:
        return True  # internal / mangled symbol — skip
    if not isinstance(name, str):
        return True
    if name.startswith('zsp_'):
        return True
    # Template specializations have a genericClass set
    if getattr(sym, 'genericClass', None) is not None:
        return True
    return False


class SVMapper:
    """Main orchestrator for mapping SystemVerilog to Zuspec IR."""

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

    def _collect_and_map(self, root) -> bool:
        # Thread the Compilation reference so ClassMapper can query attributes
        self.class_mapper.compilation = self.parser.compilation

        sv_classes = []

        def visitor(symbol):
            if symbol.kind == pyslang.SymbolKind.ClassType:
                if not _is_library_class(symbol):
                    sv_classes.append(symbol)
            return True

        root.visit(visitor)

        for sv_class in sv_classes:
            ir_class = self.class_mapper.map_class(sv_class)
            if ir_class:
                self.classes.append(ir_class)

        return not self.error_reporter.has_errors()

    def map_text(self, text: str, file_name: str = "<string>") -> bool:
        if not self.parser.parse_text(text, file_name):
            return False
        root = self.parser.get_root()
        if not root:
            self.error_reporter.error("Failed to get compilation root")
            return False
        return self._collect_and_map(root)

    def map_files(self, file_paths: List[str]) -> bool:
        if not self.parser.parse_files(file_paths):
            return False
        root = self.parser.get_root()
        if not root:
            self.error_reporter.error("Failed to get compilation root")
            return False
        return self._collect_and_map(root)

    def get_classes(self) -> List[DataTypeClass]:
        return self.classes

    def get_components(self) -> List[DataTypeComponent]:
        return [c for c in self.classes if isinstance(c, DataTypeComponent)]

    def get_actions(self) -> List[DataTypeAction]:
        return [c for c in self.classes if isinstance(c, DataTypeAction)]

    def get_error_report(self) -> str:
        return self.error_reporter.report()

    def has_errors(self) -> bool:
        return self.error_reporter.has_errors()
