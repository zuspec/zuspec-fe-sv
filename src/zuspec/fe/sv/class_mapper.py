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
from zuspec.ir.core.expr import (
    ExprConstant, ExprRefUnresolved, ExprStructField, ExprStructLiteral)
from zuspec.ir.core.data_type import DataTypeMemberRef, TypeDef
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
            function_mapper.decl_type_fn = self._map_arg_type
            sm = getattr(function_mapper, "stmt_mapper", None)
            if sm is not None:
                sm.type_fn = self._map_sig_type
                if getattr(sm, "expr_mapper", None) is not None:
                    sm.expr_mapper.type_fn = self._map_sig_type
        type_mapper.class_local = self._map_class_local
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
        # The classes being mapped (``p::cls``), innermost last: a typedef
        # of one of them is referred to by its local name.
        self._cls_stack: List[str] = []
        # The scope type syntax is looked up in (the class being mapped).
        self._scope = None

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
        with self._in_class(class_symbol):
            ir_class = self._map_plain_class_body(class_symbol, class_name)
        ir_class.annotations = sv_annotations(self.compilation, class_symbol)
        return ir_class

    def _map_plain_class_body(self, class_symbol, class_name) -> DataTypeClass:
        base = getattr(class_symbol, "baseClass", None)
        ir_class = DataTypeClass(
            name=class_name,
            super=self._map_type(base) if base is not None else None)
        ir_class.typedefs = self._map_typedefs(class_symbol)
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
        with self._in_class(class_symbol):
            typedefs = self._map_typedefs(class_symbol)
            methods = []
            for m in class_symbol:
                if m.kind == pyslang.ast.SymbolKind.MethodPrototype and self.function_mapper:
                    f = self.function_mapper.map_function(m)
                    if f is not None:
                        methods.append(f)
        proto = DataTypeProtocol(name=name, methods=methods, typedefs=typedefs)
        proto.annotations = sv_annotations(self.compilation, class_symbol)
        self.protocols[name] = proto
        return proto

    # ------------------------------------------------------------------
    # Class-scope types (followups §18, Q20)
    # ------------------------------------------------------------------

    def _in_class(self, class_symbol):
        """Context: ``class_symbol`` (a class or a specialization) is being
        mapped; its typedefs are referred to by their local names."""
        import contextlib
        mapper = self

        @contextlib.contextmanager
        def ctx():
            mapper._cls_stack.append(_class_qname(class_symbol))
            saved = mapper._scope
            mapper._scope = class_symbol
            try:
                yield
            finally:
                mapper._scope = saved
                mapper._cls_stack.pop()
        return ctx()

    def _map_typedefs(self, class_symbol) -> List[TypeDef]:
        """A class's typedefs, in source order."""
        out = []
        for m in class_symbol:
            if m.kind != pyslang.ast.SymbolKind.TypeAlias or _type_param_alias(m):
                continue
            dt = self._map_typedef_target(m)
            if dt is None:
                self.error_reporter.warning(f"typedef {m.name}: type not mapped")
                continue
            out.append(TypeDef(name=m.name, datatype=dt))
        return out

    def _map_typedef_target(self, alias):
        """The type a class-scope typedef names: from its syntax where
        that keeps more (``axi4_types #(P)``), else its resolved type; a
        struct's or integral type's widths over the class parameters."""
        tt = alias.targetType
        t, ts = tt.type, tt.typeSyntax
        r = self._map_tsyn(ts, t)
        if r is not None:
            return r
        kind = type(t).__name__
        if kind == "PackedStructType":
            s = self.type_mapper._map_struct(t, None, True)
            if s is not None and ts is not None and ts.kind.name == "StructType":
                self._struct_widths(s, t, ts)
            return s
        if kind == "EnumType":
            return self.type_mapper._map_enum(t, None, True)
        r = self._map_type(t)
        if isinstance(r, DataTypeInt) and ts is not None and self._body_scope is not None:
            from zuspec.fe.sv.module_header import width_expr
            e = width_expr(ts, t, self._body_scope)
            r.width_expr = None if e is None or isinstance(e, ExprConstant) else e
        return r

    def _struct_widths(self, s, t, ts):
        """Set each integral member's ``width_expr`` from its syntax."""
        if self._body_scope is None:
            return
        from zuspec.fe.sv.module_header import width_expr
        syn = {}
        for mem in ts.members:
            for d in mem.declarators:
                syn[d.name.valueText] = mem.type
        sym = {f.name: f for f in t if type(f).__name__ == "FieldSymbol"}
        for f in s.fields:
            if isinstance(f.datatype, DataTypeInt) and f.name in syn and f.name in sym:
                e = width_expr(syn[f.name], sym[f.name].type, self._body_scope)
                f.datatype.width_expr = (None if e is None or isinstance(e, ExprConstant)
                                         else e)

    def _map_class_local(self, alias):
        """A class-scope typedef used by a type: its local name in its own
        class, else a ``DataTypeMemberRef`` into its class (through the
        class's default specialization when the class is generic)."""
        owner, _, name = str(alias).rpartition("::")
        if owner in self._cls_stack:
            return DataTypeRef(ref_name=name)
        base = self._class_ref(owner)
        if base is None:
            return None
        return DataTypeMemberRef(base=base, member=name)

    def _class_ref(self, qname):
        pkg, _, cls = qname.rpartition("::")
        sym = None
        try:
            p = self.compilation.getPackage(pkg) if (self.compilation and pkg) else None
            sym = p.find(cls) if p is not None else None
        except Exception:
            sym = None
        if sym is None:
            return DataTypeRef(ref_name=cls)
        if sym.kind == pyslang.ast.SymbolKind.GenericClassDef:
            return self.map_specialization(_default_specialization(sym))
        return DataTypeRef(ref_name=cls)

    def _map_decl_type(self, t, ts):
        """A declaration's type: from its syntax where that keeps more,
        else from the resolved type."""
        r = self._map_tsyn(ts, t)
        return r if r is not None else self._map_type(t)

    def _map_arg_type(self, arg):
        dt = getattr(arg, "declaredType", None)
        ts = getattr(dt, "typeSyntax", None) if dt is not None else None
        r = self._map_tsyn(ts, arg.type)
        return r if r is not None else self._map_sig_type(arg.type)

    def _map_tsyn(self, ts, t):
        """Map a named type from its syntax: ``cls #(args)``, ``x::member``.
        None to fall back to the resolved type (a plain name, a keyword
        type, or anything not understood)."""
        if ts is None or self._scope is None or ts.kind.name != "NamedType":
            return None
        try:
            return self._map_name_syn(ts.name, t)
        except Exception as e:
            self.error_reporter.warning(f"type {str(ts).strip()}: {e}")
            return None

    def _map_name_syn(self, n, t):
        from zuspec.fe.sv.module_header import _lookup
        k = n.kind.name
        if k == "ScopedName" and n.right.kind.name == "IdentifierName":
            left = n.left
            if left.kind.name == "IdentifierName":
                m = _lookup(self._scope, left.identifier.valueText)
                if m is None or m.kind.name != "TypeAlias" or _type_param_alias(m):
                    return None          # a package, or a type parameter
                owner = str(m).rpartition("::")[0]
                if owner not in self._cls_stack:
                    return None
                base = DataTypeRef(ref_name=m.name)
            elif left.kind.name == "ClassName":
                base = self._map_class_name(left, None)
            else:
                return None
            if base is None:
                return None
            return DataTypeMemberRef(base=base, member=n.right.identifier.valueText)
        # ``cls #(P)`` in a template body: its arguments as written (outside
        # one, the resolved specialization says the same)
        if (k == "ClassName" and type(t).__name__ == "ClassType"
                and self._body_scope is not None):
            return self._map_class_name(n, t)
        return None

    def _map_class_name(self, n, t):
        """``cls #(args)`` as a ``DataTypeSpecialized``: a value argument
        as written over the enclosing parameters (``P``), a type argument
        mapped from its syntax or its resolved type (from ``t``, the
        resolved class, when known). Every parameter gets an argument: one
        not written takes its resolved value, else the template's
        default."""
        from zuspec.fe.sv.module_header import _lookup, _xlate
        g = _lookup(self._scope, n.identifier.valueText)
        if g is not None and getattr(g, "genericClass", None) is not None:
            g = g.genericClass       # found as a specialization (the default)
        if g is None or g.kind != pyslang.ast.SymbolKind.GenericClassDef:
            return None
        tmpl = (self.map_generic_api(g) if getattr(g, "isInterface", False)
                else self.map_generic_class(g))
        if not isinstance(tmpl, DataTypeParameterized):
            return None
        resolved = {}
        if t is not None:
            for m in t:
                if getattr(m, "isPortParam", False):
                    resolved[m.name] = m
        from zuspec.fe.sv.module_header import _items
        assigns = _items(n.parameters.parameters) if n.parameters is not None else []
        params = tmpl.template_params
        given = {}
        for i, a in enumerate(assigns):
            if a.kind.name == "OrderedParamAssignment":
                if i >= len(params):
                    return None
                given[params[i].name] = a.expr
            elif a.kind.name == "NamedParamAssignment":
                given[a.name.valueText] = a.expr
            else:
                return None
        args = []
        for p in params:
            e = given.get(p.name)
            if isinstance(p, TemplateParamValue):
                rv = resolved.get(p.name)
                if e is not None:
                    v = _xlate(e, self._scope)
                elif rv is not None:
                    v = _param_value(rv.value, rv.type)
                else:
                    v = p.default_value
                if v is None:
                    return None
                args.append(TemplateArgValue(param_name=p.name, value_expr=v))
            elif isinstance(p, TemplateParamType):
                tv = None
                rt = resolved.get(p.name)
                rtype = rt.targetType.type if rt is not None else None
                if e is not None and e.kind.name in ("ScopedName", "ClassName"):
                    tv = self._map_name_syn(e, rtype)
                if tv is None and rtype is not None:
                    tv = self._map_type(rtype)
                if tv is None and e is None:
                    tv = p.default_value
                if tv is None:
                    return None
                args.append(TemplateArgType(param_name=p.name, type_value=tv))
            else:
                return None
        return DataTypeSpecialized(name=tmpl.name, base_template=tmpl,
                                   template_args=args,
                                   specialized_name=str(t) if t is not None
                                   else str(n).strip())

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

            dt = getattr(property_symbol, "declaredType", None)
            ir_type = self._map_decl_type(
                var_type, getattr(dt, "typeSyntax", None) if dt is not None else None)
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
            tmpl.annotations = sv_annotations(self.compilation, generic_sym)
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
                    default_value=_param_default(m)))
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
            saved = (self._tparams, self._body_scope)
            self._tparams, self._body_scope = tparams, spec
            try:
                body = self.map_protocol(spec, name)
            finally:
                self._tparams, self._body_scope = saved
            tmpl = DataTypeParameterized(name=name, template_params=params, body=body)
            tmpl.annotations = sv_annotations(self.compilation, generic_sym)
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
                    args.append(TemplateArgValue(param_name=m.name,
                                                 value_expr=_param_value(m.value, m.type)))
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


def _param_default(param):
    """A value parameter's default. A struct-valued one keeps a named
    constant (``AXI4_PARAM_DEFAULT``) as a reference; any other struct
    value becomes a member-by-member literal."""
    init = getattr(param, "initializer", None)
    if (_packed_struct(param.type) is not None and init is not None
            and init.kind == pyslang.ast.ExpressionKind.NamedValue):
        from zuspec.fe.sv.module_header import package_ref
        return (package_ref(init.symbol)
                or ExprRefUnresolved(name=init.symbol.name))
    return _param_value(param.value, param.type)


def _param_value(cv, t):
    """A parameter value: a struct literal for a packed struct, else
    ``_const``."""
    st = _packed_struct(t)
    if st is not None:
        lit = _struct_literal(cv, st)
        if lit is not None:
            return lit
    return _const(cv)


def _packed_struct(t):
    ct = getattr(t, "canonicalType", None)
    return ct if (ct is not None and ct.kind == pyslang.ast.SymbolKind.PackedStructType) else None


def _struct_literal(cv, st):
    """``'{A: 4, B: 8}`` for a packed struct constant, members in
    declaration order, or None when the value has X/Z bits. The value is
    read as hex text and sliced here: printing it in decimal is
    abbreviated for wide structs."""
    try:
        text = cv.value.toString(pyslang.LiteralBase.Hex, False)
        bits = int(text.replace("_", ""), 16)
    except (AttributeError, TypeError, ValueError):
        return None
    return _struct_fields(bits, st)


def _struct_fields(bits, st):
    fields = []
    for f in st:
        if f.kind != pyslang.ast.SymbolKind.Field:
            continue
        w = f.type.bitWidth
        v = (bits >> f.bitOffset) & ((1 << w) - 1)
        sub = _packed_struct(f.type)
        fields.append(ExprStructField(
            name=f.name,
            value=_struct_fields(v, sub) if sub is not None else ExprConstant(value=v)))
    return ExprStructLiteral(fields=fields)


def _const(cv):
    """A parameter's constant value as an ``ExprConstant``."""
    from zuspec.fe.sv.module_header import _value
    return ExprConstant(value=_value(cv))


def _class_qname(sym) -> str:
    """``p::cls`` for a class or a specialization of a generic class."""
    g = getattr(sym, "genericClass", None)
    return (g if g is not None else sym).hierarchicalPath
