"""Class mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.dataclasses.ir.data_type import DataTypeClass, DataTypeComponent, Process
from zuspec.dataclasses.ir.fields import Field
from zuspec.dataclasses.ir.data_type import DataTypeInt
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.type_mapper import TypeMapper

# Library-generated members to skip when mapping components
_SKIP_MEMBER_NAMES = frozenset({
    'new', 'create',
    'randomize', 'pre_randomize', 'post_randomize',
    'get_randstate', 'set_randstate', 'srandom',
    'rand_mode', 'constraint_mode',
    'm_parent', 'm_children', 'clock_d', 'm_clock_domains',
})

# Known zsp base class names — we don't map these themselves
_ZSP_BASE_NAMES = frozenset({'zsp_component', 'zsp_object', 'zsp_component_root'})


class ClassMapper:
    """Maps SystemVerilog classes to Zuspec IR."""

    def __init__(
        self,
        config: SVToZuspecConfig,
        error_reporter: ErrorReporter,
        type_mapper: TypeMapper,
        function_mapper=None,
    ):
        self.config = config
        self.error_reporter = error_reporter
        self.type_mapper = type_mapper
        self.function_mapper = function_mapper

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    def _is_zsp_component_subclass(self, class_sym) -> bool:
        """True if class_sym (user class) extends zsp_component transitively."""
        bc = getattr(class_sym, 'baseClass', None)
        while bc is not None:
            if bc.name in _ZSP_BASE_NAMES:
                return True
            bc = getattr(bc, 'baseClass', None)
        return False

    def _is_zsp_reg_field(self, prop_sym) -> bool:
        """True if the property's type is a specialization of zsp_reg_c."""
        gc = getattr(prop_sym.type, 'genericClass', None)
        return gc is not None and gc.name == 'zsp_reg_c'

    # ------------------------------------------------------------------
    # Component mapping
    # ------------------------------------------------------------------

    def map_component(self, class_sym) -> Optional[DataTypeComponent]:
        """Map a zsp_component subclass to DataTypeComponent."""
        try:
            class_name = class_sym.name

            # --- Pass 1: collect register fields and build field-index map ---
            fields: List[Field] = []
            field_index_map: dict = {}

            for sym in class_sym:
                if sym.name in _SKIP_MEMBER_NAMES:
                    continue
                if sym.kind == pyslang.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.SymbolKind.ClassProperty:
                    if self._is_zsp_reg_field(sym):
                        bits, signed = self.type_mapper.map_zsp_reg_width(sym)
                        f = Field(
                            name=sym.name,
                            datatype=DataTypeInt(bits=bits, signed=signed),
                            is_reg=True,
                            reset_value=0,
                        )
                        field_index_map[sym.name] = len(fields)
                        fields.append(f)

            # Inject field context into expr_mapper so body expressions resolve correctly
            if self.function_mapper:
                self.function_mapper.expr_mapper.set_field_context(field_index_map)

            # --- Pass 2: map subroutines ---
            proc_processes: List[Process] = []

            for sym in class_sym:
                if sym.name in _SKIP_MEMBER_NAMES:
                    continue
                if sym.kind == pyslang.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.SymbolKind.Subroutine:
                    if (sym.subroutineKind == pyslang.SubroutineKind.Task
                            and sym.name == 'run'):
                        body = []
                        if self.function_mapper and sym.body is not None:
                            body = self.function_mapper.stmt_mapper.map_statements(sym.body)
                        proc_processes.append(Process(name=sym.name, body=body))

            comp = DataTypeComponent(name=class_name, super=None)
            comp.fields = fields
            comp.proc_processes = proc_processes
            return comp

        except Exception as e:
            self.error_reporter.error(f"Error mapping component: {str(e)}")
            return None

    # ------------------------------------------------------------------
    # Generic class mapping
    # ------------------------------------------------------------------

    def map_class(self, class_symbol) -> Optional[DataTypeClass]:
        """Map a SystemVerilog class to Zuspec IR, routing components appropriately."""
        try:
            # Delegate zsp_component subclasses to map_component
            if self._is_zsp_component_subclass(class_symbol):
                return self.map_component(class_symbol)

            class_name = class_symbol.name
            ir_class = DataTypeClass(name=class_name, super=None)

            fields = self._map_fields(class_symbol)
            if fields is not None:
                ir_class.fields = fields

            if self.function_mapper:
                functions = self.function_mapper.map_functions_from_class(class_symbol)
                if functions:
                    ir_class.functions = functions

            return ir_class

        except Exception as e:
            self.error_reporter.error(f"Error mapping class: {str(e)}")
            return None

    def _map_fields(self, class_symbol) -> Optional[List[Field]]:
        """Map plain class fields."""
        fields = []
        try:
            for sym in class_symbol:
                if sym.kind == pyslang.SymbolKind.ClassProperty:
                    field = self._map_property_field(sym)
                    if field:
                        fields.append(field)
            return fields
        except Exception as e:
            self.error_reporter.error(f"Error mapping fields: {str(e)}")
            return None

    def _map_property_field(self, property_symbol) -> Optional[Field]:
        """Map a single class property to a Field."""
        try:
            field_name = property_symbol.name
            var_type = property_symbol.type

            if hasattr(var_type, 'isFourState') and var_type.isFourState:
                self.error_reporter.error(
                    f"Field '{field_name}' uses 4-state type",
                    suggestion="Use 2-state types (bit, int, byte, etc.)",
                )
                return None

            ir_type = self._map_type(var_type)
            if ir_type is None:
                return None

            return Field(name=field_name, datatype=ir_type)

        except Exception as e:
            self.error_reporter.error(f"Error mapping field: {str(e)}")
            return None

    def _map_type(self, sv_type):
        """Map a slang type to Zuspec IR type."""
        try:
            if isinstance(sv_type, pyslang.IntegralType):
                width = int(sv_type.bitWidth) if hasattr(sv_type, 'bitWidth') else None
                signed = bool(sv_type.isSigned) if hasattr(sv_type, 'isSigned') else None
                type_str = str(sv_type)
                type_name = type_str.split('[')[0].strip() if '[' in type_str else type_str
                return self.type_mapper.map_builtin_type(
                    type_name=type_name, width=width, signed=signed,
                )
            self.error_reporter.error(f"Unsupported type: {sv_type}")
            return None
        except Exception as e:
            self.error_reporter.error(f"Error mapping type: {str(e)}")
            return None
