"""Class mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import sys
import os
import pyslang

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../../packages/zuspec-dataclasses/src'))

from zuspec.ir.core.data_type import DataTypeClass, DataTypeComponent, DataTypeAction, Process
from zuspec.ir.core.fields import Field, SignalDirection
from zuspec.ir.core.data_type import DataTypeInt
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.constraint_mapper import ConstraintMapper

# Library-generated members to skip when mapping components/actions
_SKIP_MEMBER_NAMES = frozenset({
    'new', 'create',
    'randomize', 'pre_randomize', 'post_randomize',
    'get_randstate', 'set_randstate', 'srandom',
    'rand_mode', 'constraint_mode',
    'm_parent', 'm_children', 'clock_d', 'm_clock_domains',
    'm_comp',  # zsp_action_c base-class field — not a user field
})

# Known zsp component base class names — we don't map these themselves
_ZSP_BASE_NAMES = frozenset({'zsp_component', 'zsp_object', 'zsp_component_root'})

# zsp_action_c base class name — marks action classes
_ZSP_ACTION_BASE_NAMES = frozenset({'zsp_action_c'})


class ClassMapper:
    """Maps SystemVerilog classes to Zuspec IR."""

    def __init__(
        self,
        config: SVToZuspecConfig,
        error_reporter: ErrorReporter,
        type_mapper: TypeMapper,
        function_mapper=None,
        compilation=None,
    ):
        self.config = config
        self.error_reporter = error_reporter
        self.type_mapper = type_mapper
        self.function_mapper = function_mapper
        # pyslang.Compilation — needed for attribute queries (zsp_output etc.)
        self.compilation = compilation

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

    def _is_zsp_action_subclass(self, class_sym) -> bool:
        """True if class_sym extends zsp_action_c (possibly via specialization)."""
        bc = getattr(class_sym, 'baseClass', None)
        while bc is not None:
            gc = getattr(bc, 'genericClass', None)
            bc_name = gc.name if gc is not None else getattr(bc, 'name', '')
            if bc_name in _ZSP_ACTION_BASE_NAMES:
                return True
            bc = getattr(bc, 'baseClass', None)
        return False

    def _get_action_comp_type_name(self, class_sym) -> Optional[str]:
        """Return the component type name T from `extends zsp_action_c #(T)`."""
        bc = getattr(class_sym, 'baseClass', None)
        while bc is not None:
            gc = getattr(bc, 'genericClass', None)
            bc_name = gc.name if gc is not None else getattr(bc, 'name', '')
            if bc_name in _ZSP_ACTION_BASE_NAMES:
                # Extract the type argument from hierarchicalPath, e.g.
                # "zsp_pkg::zsp_action_c#(my_comp_c)" → "my_comp_c"
                try:
                    path = getattr(bc, 'hierarchicalPath', None)
                    if path and '#(' in path and path.endswith(')'):
                        type_arg = path[path.index('#(') + 2 : -1].strip()
                        # Strip any leading package qualifier
                        return type_arg.split('::')[-1]
                except Exception:
                    pass
                # Fallback: look at m_comp member type in the specialization
                try:
                    for member in bc:
                        if getattr(member, 'name', None) == 'm_comp':
                            comp_type = getattr(member, 'type', None)
                            if comp_type is not None:
                                n = getattr(comp_type, 'name', None)
                                if n and n != 'T':
                                    return n
                except Exception:
                    pass
                return None
            bc = getattr(bc, 'baseClass', None)
        return None

    def _is_zsp_reg_field(self, prop_sym) -> bool:
        """True if the property's type is a specialization of zsp_reg_c."""
        gc = getattr(prop_sym.type, 'genericClass', None)
        return gc is not None and gc.name == 'zsp_reg_c'

    def _field_is_rand(self, prop_sym) -> bool:
        """True if the property has ``rand`` qualifier."""
        try:
            return prop_sym.randMode == pyslang.RandMode.Rand
        except Exception:
            return False

    def _field_is_output(self, prop_sym) -> bool:
        """True if the property carries a ``(*zsp_output*)`` attribute."""
        if self.compilation is None:
            return False
        try:
            attrs = self.compilation.getAttributes(prop_sym)
            return any(getattr(a, 'name', None) == 'zsp_output' for a in attrs)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Action mapping
    # ------------------------------------------------------------------

    def map_action(self, class_sym) -> Optional[DataTypeAction]:
        """Map a zsp_action_c subclass to DataTypeAction."""
        try:
            class_name = class_sym.name
            comp_type_name = self._get_action_comp_type_name(class_sym)

            fields: List[Field] = []
            for sym in class_sym:
                if sym.name in _SKIP_MEMBER_NAMES:
                    continue
                if sym.kind == pyslang.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.SymbolKind.ClassProperty:
                    f = self._map_action_property(sym)
                    if f is not None:
                        fields.append(f)

            action = DataTypeAction(
                name=class_name,
                super=None,
                comp_type_name=comp_type_name,
            )
            action.fields = fields

            # Extract constraint structure for Phase 4 (synthesis pipeline)
            try:
                cmapper = ConstraintMapper(compilation=self.compilation)
                action.constraint_set = cmapper.map(class_sym, comp_type_name)
            except Exception:
                pass  # constraint extraction is best-effort; don't fail mapping

            return action

        except Exception as e:
            self.error_reporter.error(f"Error mapping action: {str(e)}")
            return None

    def _map_action_property(self, prop_sym) -> Optional[Field]:
        """Map a single action property; allows 4-state (logic) types."""
        try:
            field_name = prop_sym.name
            var_type = prop_sym.type

            ir_type = self._map_type_allow_4state(var_type)
            if ir_type is None:
                return None

            is_rand = self._field_is_rand(prop_sym)
            is_out = self._field_is_output(prop_sym)

            direction: Optional[SignalDirection] = None
            rand_kind: Optional[str] = None
            if is_rand:
                rand_kind = 'rand'
                direction = SignalDirection.OUTPUT if is_out else None
            else:
                direction = SignalDirection.INPUT

            return Field(
                name=field_name,
                datatype=ir_type,
                rand_kind=rand_kind,
                direction=direction,
            )

        except Exception as e:
            self.error_reporter.error(f"Error mapping action field: {str(e)}")
            return None

    def _map_type_allow_4state(self, sv_type):
        """Map a slang type to Zuspec IR type, accepting 4-state (logic) types."""
        try:
            if isinstance(sv_type, pyslang.IntegralType):
                width = int(sv_type.bitWidth) if hasattr(sv_type, 'bitWidth') else None
                signed = bool(sv_type.isSigned) if hasattr(sv_type, 'isSigned') else False
                return DataTypeInt(bits=width or 1, signed=signed)
            self.error_reporter.error(f"Unsupported action field type: {sv_type}")
            return None
        except Exception as e:
            self.error_reporter.error(f"Error mapping action type: {str(e)}")
            return None

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
        """Map a SystemVerilog class to Zuspec IR, routing components/actions appropriately."""
        try:
            # Route zsp_component subclasses
            if self._is_zsp_component_subclass(class_symbol):
                return self.map_component(class_symbol)

            # Route zsp_action_c subclasses
            if self._is_zsp_action_subclass(class_symbol):
                return self.map_action(class_symbol)

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
