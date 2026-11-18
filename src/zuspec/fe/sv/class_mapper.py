"""Class mapping from SystemVerilog to Zuspec IR."""
from typing import Optional, List
import pyslang


from zuspec.ir.core.data_type import DataTypeClass, DataTypeComponent, DataTypeAction, Process
from zuspec.ir.core.fields import Field, SignalDirection
from zuspec.ir.core.data_type import (
    DataTypeInt, DataTypeRef, DataTypeParameterized, DataTypeSpecialized,
    DataTypeProtocol, DataTypeList,
    TemplateParamKind, TemplateParamType, TemplateParamValue,
    TemplateArgType, TemplateArgValue,
)
from zuspec.ir.core.expr import ExprConstant
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.type_mapper import TypeMapper
from zuspec.fe.sv.constraint_mapper import ConstraintMapper
from zuspec.fe.sv.function_mapper import sv_annotations

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
        if function_mapper is not None:
            function_mapper.type_fn = self._map_sig_type
        # pyslang.ast.Compilation — needed for attribute queries (zsp_output etc.)
        self.compilation = compilation
        #: Parameterized classes mapped so far, by generic-class name.
        self.templates = {}
        #: Specializations mapped so far, by ``str()`` of the slang type.
        self.specializations = {}
        #: Interface classes mapped so far (``DataTypeProtocol``), by name.
        self.protocols = {}
        #: Parameterized interface classes (``map_generic_api``), by name.
        self.api_templates = {}
        # While mapping a template body: the alias name of each type
        # parameter (``p::c::T``) -> the parameter's name, and the scope
        # its widths are read in.
        self._tparams = None
        self._body_scope = None

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
            return prop_sym.randMode == pyslang.ast.RandMode.Rand
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
                if sym.kind == pyslang.ast.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.ast.SymbolKind.ClassProperty:
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
            if isinstance(sv_type, pyslang.ast.IntegralType):
                width = int(sv_type.bitWidth) if hasattr(sv_type, 'bitWidth') else None
                signed = bool(sv_type.isSigned) if hasattr(sv_type, 'isSigned') else False
                return DataTypeInt(bits=width or 1, signed=signed,
                                   four_state=True if getattr(sv_type, "isFourState", False) else None)
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
                if sym.kind == pyslang.ast.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.ast.SymbolKind.ClassProperty:
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
                if sym.kind == pyslang.ast.SymbolKind.TransparentMember:
                    continue
                if sym.kind == pyslang.ast.SymbolKind.Subroutine:
                    if (sym.subroutineKind == pyslang.ast.SubroutineKind.Task
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
            if getattr(class_symbol, "isInterface", False):
                return self.map_protocol(class_symbol, class_symbol.name)

            # Route zsp_component subclasses
            if self._is_zsp_component_subclass(class_symbol):
                return self.map_component(class_symbol)

            # Route zsp_action_c subclasses
            if self._is_zsp_action_subclass(class_symbol):
                return self.map_action(class_symbol)

            return self._map_plain_class(class_symbol, class_symbol.name)

        except Exception as e:
            self.error_reporter.error(f"Error mapping class: {str(e)}")
            return None

    def _map_plain_class(self, class_symbol, class_name) -> DataTypeClass:
        base = getattr(class_symbol, "baseClass", None)
        ir_class = DataTypeClass(
            name=class_name,
            super=self._map_type(base) if base is not None else None)
        # An implemented interface by its (generic) class name; the
        # arguments of a parameterized interface are not kept.
        ir_class.implements = [
            x.genericClass.name if x.genericClass is not None else x.name
            for x in getattr(class_symbol, "implementedInterfaces", [])]

        fields = self._map_fields(class_symbol)
        if fields is not None:
            ir_class.fields = fields

        if self.function_mapper:
            functions = self.function_mapper.map_functions_from_class(class_symbol)
            if functions:
                ir_class.functions = functions

        return ir_class

    def map_protocol(self, class_symbol, name) -> Optional[DataTypeProtocol]:
        """Map an interface class to a ``DataTypeProtocol``: its pure
        virtual methods (slang ``MethodPrototype`` members)."""
        if name in self.protocols:
            return self.protocols[name]
        methods = []
        for m in class_symbol:
            if m.kind == pyslang.ast.SymbolKind.MethodPrototype and self.function_mapper:
                f = self.function_mapper.map_function(m)
                if f is not None:
                    methods.append(f)
        proto = DataTypeProtocol(name=name, methods=methods)
        self.protocols[name] = proto
        return proto

    def _map_fields(self, class_symbol) -> Optional[List[Field]]:
        """Map plain class fields."""
        fields = []
        try:
            for sym in class_symbol:
                if sym.kind == pyslang.ast.SymbolKind.ClassProperty:
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

            ir_type = self._map_type(var_type)
            if ir_type is None:
                return None
            if self._body_scope is not None and isinstance(ir_type, DataTypeInt):
                ir_type.width_expr = self._width_expr(property_symbol, var_type)
            elif (self._body_scope is not None
                    and isinstance(ir_type, DataTypeList)
                    and isinstance(ir_type.element_type, DataTypeInt)):
                # ``bit [W-1:0] q[$]``: the type syntax is the element's
                ir_type.element_type.width_expr = self._width_expr(
                    property_symbol, var_type.elementType)

            field = Field(name=field_name, datatype=ir_type)
            field.annotations = sv_annotations(self.compilation, property_symbol)
            return field

        except Exception as e:
            self.error_reporter.error(f"Error mapping field: {str(e)}")
            return None

    def _map_sig_type(self, sv_type):
        """A method argument or return type; an unsupported one is None,
        with a warning (not an error)."""
        return self._map_type(sv_type, quiet=True)

    def _map_type(self, sv_type, quiet=False):
        """Map a slang type to Zuspec IR type."""
        try:
            if self._tparams and type(sv_type).__name__ == "TypeAliasType" \
                    and str(sv_type) in self._tparams:
                return DataTypeRef(ref_name=self._tparams[str(sv_type)])
            if type(sv_type).__name__ == "TypeAliasType" and _type_param_alias(sv_type):
                # In a specialization, ``T`` is its actual type
                # (``put_if #(byte)``); its alias name is one per generic.
                return self._map_type(sv_type.targetType.type, quiet)
            if type(sv_type).__name__ in ("TypeAliasType", "EnumType", "PackedStructType"):
                return self.type_mapper.map_type(sv_type)
            if type(sv_type).__name__ == "QueueType":
                elem = self._map_type(sv_type.elementType, quiet)
                return None if elem is None else DataTypeList(element_type=elem)
            if type(sv_type).__name__ == "ClassType":
                if sv_type.genericClass is not None:
                    return self.map_specialization(sv_type)
                return DataTypeRef(ref_name=sv_type.name)
            if isinstance(sv_type, pyslang.ast.IntegralType):
                width = int(sv_type.bitWidth) if hasattr(sv_type, 'bitWidth') else None
                signed = bool(sv_type.isSigned) if hasattr(sv_type, 'isSigned') else None
                type_str = str(sv_type)
                type_name = type_str.split('[')[0].strip() if '[' in type_str else type_str
                t = self.type_mapper.map_builtin_type(
                    type_name=type_name, width=width, signed=signed,
                )
                if t is not None and bool(getattr(sv_type, "isFourState", False)):
                    t.four_state = True
                return t
            if quiet:
                self.error_reporter.warning(f"Unsupported type: {sv_type}")
            else:
                self.error_reporter.error(f"Unsupported type: {sv_type}")
            return None
        except Exception as e:
            self.error_reporter.error(f"Error mapping type: {str(e)}")
            return None

    # ------------------------------------------------------------------
    # Parameterized classes and their specializations
    # ------------------------------------------------------------------

    def map_generic_class(self, generic_sym) -> Optional[DataTypeParameterized]:
        """Map a parameterized class (a slang ``GenericClassDefSymbol``) to a
        ``DataTypeParameterized``: its parameters, and its body mapped from
        the default specialization, written over the parameters."""
        name = generic_sym.name
        if name in self.templates:
            return self.templates[name]
        if getattr(generic_sym, "isInterface", False):
            # A parameterized interface class is a protocol over its
            # default specialization; its parameters are not kept, but
            # method types written over them stay references (``T``).
            spec = _default_specialization(generic_sym)
            saved = self._tparams
            self._tparams = {
                _alias_name(spec, m.name): m.name for m in spec
                if m.kind == pyslang.ast.SymbolKind.TypeParameter and m.isPortParam}
            try:
                return self.map_protocol(spec, name)
            finally:
                self._tparams = saved
        try:
            spec = _default_specialization(generic_sym)
            tmpl = DataTypeParameterized(name=name)
            self.templates[name] = tmpl
            params, tparams = self._template_params(spec)
            tmpl.template_params = params
            saved = (self._tparams, self._body_scope)
            self._tparams, self._body_scope = tparams, spec
            try:
                tmpl.body = self._map_plain_class(spec, name)
            finally:
                self._tparams, self._body_scope = saved
            return tmpl
        except Exception as e:
            self.error_reporter.error(f"Error mapping parameterized class {name}: {e}")
            return None

    def _template_params(self, spec):
        """The port parameters of a default specialization, as template
        params, and its type-parameter aliases (``alias -> name``)."""
        params, tparams = [], {}
        for m in spec:
            if m.kind == pyslang.ast.SymbolKind.TypeParameter and m.isPortParam:
                tparams[_alias_name(spec, m.name)] = m.name
        for m in spec:
            if not getattr(m, "isPortParam", False):
                continue
            if m.kind == pyslang.ast.SymbolKind.TypeParameter:
                params.append(TemplateParamType(
                    name=m.name, kind=TemplateParamKind.TYPE,
                    default_value=self._map_type(m.targetType.type)))
            elif m.kind == pyslang.ast.SymbolKind.Parameter:
                params.append(TemplateParamValue(
                    name=m.name, kind=TemplateParamKind.VALUE,
                    value_type=self._map_type(m.type),
                    default_value=_const(m.value)))
        return params, tparams

    def map_generic_api(self, generic_sym) -> Optional[DataTypeParameterized]:
        """Map a parameterized interface class to a ``DataTypeParameterized``
        whose body is the ``DataTypeProtocol`` written over its parameters
        (``map_generic_class`` keeps only the protocol)."""
        name = generic_sym.name
        if name in self.api_templates:
            return self.api_templates[name]
        try:
            spec = _default_specialization(generic_sym)
            params, tparams = self._template_params(spec)
            saved = self._tparams
            self._tparams = tparams
            try:
                body = self.map_protocol(spec, name)
            finally:
                self._tparams = saved
            tmpl = DataTypeParameterized(name=name, template_params=params, body=body)
            self.api_templates[name] = tmpl
            return tmpl
        except Exception as e:
            self.error_reporter.error(f"Error mapping parameterized interface class {name}: {e}")
            return None

    def map_specialization(self, class_type) -> Optional[DataTypeSpecialized]:
        """Map a specialization of a parameterized class (a slang
        ``ClassType`` whose ``genericClass`` is set) to a
        ``DataTypeSpecialized`` of its template."""
        key = str(class_type)
        if key in self.specializations:
            return self.specializations[key]
        tmpl = self.map_generic_class(class_type.genericClass)
        if tmpl is None:
            return None
        if isinstance(tmpl, DataTypeProtocol):
            return DataTypeRef(ref_name=tmpl.name)
        saved = (self._tparams, self._body_scope)
        self._tparams, self._body_scope = None, None
        try:
            args = []
            for m in class_type:
                if not getattr(m, "isPortParam", False):
                    continue
                if m.kind == pyslang.ast.SymbolKind.TypeParameter:
                    args.append(TemplateArgType(param_name=m.name,
                                                type_value=self._map_type(m.targetType.type)))
                elif m.kind == pyslang.ast.SymbolKind.Parameter:
                    args.append(TemplateArgValue(param_name=m.name, value_expr=_const(m.value)))
        finally:
            self._tparams, self._body_scope = saved
        sp = DataTypeSpecialized(name=tmpl.name, base_template=tmpl,
                                 template_args=args, specialized_name=key)
        self.specializations[key] = sp
        return sp

    def _width_expr(self, prop, t):
        """A field's width as an expression over the template's value
        parameters, or None when it does not depend on them."""
        from zuspec.fe.sv.module_header import width_expr
        dt = getattr(prop, "declaredType", None)
        tsyn = getattr(dt, "typeSyntax", None) if dt is not None else None
        e = width_expr(tsyn, t, self._body_scope)
        return None if e is None or isinstance(e, ExprConstant) else e


def _default_specialization(generic_sym):
    """The default specialization of a parameterized class. pyslang binds
    ``GenericClassDefSymbol.defaultSpecialization`` as a property of a
    method that takes a scope, so call its getter directly."""
    fget = type(generic_sym).__dict__["defaultSpecialization"].fget
    return fget(generic_sym, generic_sym.parentScope)


def _type_param_alias(t) -> bool:
    from zuspec.fe.sv.type_mapper import _type_param_alias as f
    return f(t)


def _alias_name(spec, name: str) -> str:
    """The name a type parameter's alias prints under (``p::c::T``)."""
    return f"{spec.genericClass.hierarchicalPath}::{name}" \
        if getattr(spec.genericClass, "hierarchicalPath", None) else name


def _const(cv):
    """A parameter's constant value as an ``ExprConstant``."""
    from zuspec.fe.sv.module_header import _value
    return ExprConstant(value=_value(cv))
