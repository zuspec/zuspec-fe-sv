"""Main SV to Zuspec IR mapper orchestrator."""
from typing import List, Optional
import pyslang


from zuspec.ir.core.data_type import (
    DataTypeClass, DataTypeComponent, DataTypeAction, DataTypeParameterized,
    DataTypeProtocol,
)
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
        #: Parameterized classes (``DataTypeParameterized``), each with its body.
        self.templates: List[DataTypeParameterized] = []
        #: Interface classes (``DataTypeProtocol``), parameterized or not.
        self.protocols: List[DataTypeProtocol] = []

    def _collect_and_map(self, root) -> bool:
        # Thread the Compilation reference so ClassMapper can query attributes
        self.class_mapper.compilation = self.parser.compilation
        self.function_mapper.compilation = self.parser.compilation

        sv_classes = []
        sv_generics = []
        sv_typedefs = []

        def visitor(symbol):
            if symbol.kind == pyslang.ast.SymbolKind.ClassType:
                if not _is_library_class(symbol):
                    sv_classes.append(symbol)
            elif symbol.kind == pyslang.ast.SymbolKind.GenericClassDef:
                if not _is_library_class(symbol):
                    sv_generics.append(symbol)
            elif symbol.kind == pyslang.ast.SymbolKind.TypeAlias:
                # ``pkg::name``: package scope (not class-local)
                if (symbol.hierarchicalPath.count("::") == 1
                        and not _is_library_class(symbol)):
                    sv_typedefs.append(symbol)
            return True

        root.visit(visitor)

        # Package-scope typedefs, used or not, in declaration order (a
        # typedef's own dependencies are mapped first).
        for td in sv_typedefs:
            self.type_mapper.map_type(td)

        for sv_generic in sv_generics:
            tmpl = self.class_mapper.map_generic_class(sv_generic)
            if isinstance(tmpl, DataTypeProtocol):
                self._add_protocol(tmpl)
            elif tmpl is not None and all(t is not tmpl for t in self.templates):
                self.templates.append(tmpl)

        for sv_class in sv_classes:
            ir_class = self.class_mapper.map_class(sv_class)
            if isinstance(ir_class, DataTypeProtocol):
                self._add_protocol(ir_class)
            elif ir_class:
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

    def get_templates(self) -> List[DataTypeParameterized]:
        return self.templates

    def get_protocols(self) -> List[DataTypeProtocol]:
        return self.protocols

    def get_types(self):
        """The named types (typedefs), by qualified name (``p::s_t``), each
        after the types it uses: package-scope typedefs and those the mapped
        classes use (see ``TypeMapper.types``)."""
        return dict(self.type_mapper.types)

    def _add_protocol(self, proto):
        if all(p is not proto for p in self.protocols):
            self.protocols.append(proto)

    def get_specializations(self):
        """The specializations the mapped classes use, by their SV name."""
        return dict(self.class_mapper.specializations)

    def get_components(self) -> List[DataTypeComponent]:
        return [c for c in self.classes if isinstance(c, DataTypeComponent)]

    def get_actions(self) -> List[DataTypeAction]:
        return [c for c in self.classes if isinstance(c, DataTypeAction)]

    def get_error_report(self) -> str:
        return self.error_reporter.report()

    def has_errors(self) -> bool:
        return self.error_reporter.has_errors()
