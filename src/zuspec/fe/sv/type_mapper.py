"""Type mapping from SystemVerilog to Zuspec IR.

This module handles mapping of SystemVerilog types to Zuspec IR types.
A 4-state type (``logic``, ``reg``, ``integer``) is a ``DataTypeInt`` with
``four_state`` set.
"""
from typing import Dict, Optional

from zuspec.ir.core.data_type import (
    DataType,
    DataTypeInt,
    DataTypeRef,
    DataTypeString,
    DataTypeEnum,
    DataTypeStruct,
)
from zuspec.ir.core.fields import Field
from zuspec.fe.sv.error import ErrorReporter
from zuspec.fe.sv.config import SVToZuspecConfig


class TypeMapper:
    """Maps SystemVerilog types to Zuspec IR types.
    
    - bit, int, byte, shortint, longint: 2-state
    - logic, reg, integer: 4-state (``DataTypeInt.four_state``)
    """
    
    def __init__(self, config: SVToZuspecConfig, error_reporter: ErrorReporter):
        self.config = config
        self.error_reporter = error_reporter
        #: Typedefs mapped so far, by qualified name (``p::s_t``,
        #: ``p::cls::loc_t``), in the order first mapped. A typedef of a
        #: packed struct or enum is that ``DataTypeStruct`` (``packed``) or
        #: ``DataTypeEnum``; of an integral type, a named ``DataTypeInt``; of
        #: another typedef, a named ``DataTypeRef`` to it.
        self.types: Dict[str, DataType] = {}
        #: Maps a class-scope typedef (``p::cls::aw_t``); set by the class
        #: mapper. Without it, such a typedef is mapped like a package one.
        self.class_local = None
        #: For attribute queries on struct members.
        self.compilation = None

    def map_type(self, sv_type, allow_4state: bool = True) -> Optional[DataType]:
        """Map a slang type to Zuspec IR.

        A typedef maps to a ``DataTypeRef`` to its entry in :attr:`types`
        (mapped once); a type parameter's alias in a specialization
        (``T`` in ``put_if #(byte)``) maps as its actual type; an anonymous
        packed struct or enum maps to the ``DataTypeStruct``/
        ``DataTypeEnum`` itself; any other integral type to a
        ``DataTypeInt``, with ``four_state`` set from the type (``logic``,
        ``reg``, ``integer``: True). A 4-state type is an error only when
        *allow_4state* is False. None on error."""
        kind = type(sv_type).__name__
        if kind == "TypeAliasType":
            if _type_param_alias(sv_type):
                return self.map_type(sv_type.targetType.type, allow_4state)
            if self.class_local is not None and _class_local(sv_type):
                return self.class_local(sv_type)
            return self._map_typedef(sv_type, allow_4state)
        if kind == "EnumType":
            return self._map_enum(sv_type, None, allow_4state)
        if kind == "PackedStructType":
            return self._map_struct(sv_type, None, allow_4state)
        if bool(getattr(sv_type, "isIntegral", False)):
            return self._map_integral(sv_type, allow_4state)
        self.error_reporter.error(f"Unsupported type: {sv_type}")
        return None

    def _map_typedef(self, alias, allow_4state: bool) -> Optional[DataType]:
        name = str(alias)
        if name not in self.types:
            target = alias.targetType.type
            kind = type(target).__name__
            if kind == "EnumType":
                t = self._map_enum(target, name, allow_4state)
            elif kind == "PackedStructType":
                t = self._map_struct(target, name, allow_4state)
            elif kind == "TypeAliasType":
                if self._map_typedef(target, allow_4state) is None:
                    return None
                t = DataTypeRef(name=name, ref_name=str(target))
            else:
                t = self.map_type(target, allow_4state)
                if isinstance(t, DataTypeInt):
                    t.name = name
            if t is None:
                return None
            self.types[name] = t
        return DataTypeRef(ref_name=name)

    def _map_enum(self, sv_type, name, allow_4state: bool) -> Optional[DataTypeEnum]:
        from zuspec.fe.sv.interp.values import const_to_py
        base = self._map_integral(sv_type.baseType, allow_4state)
        if base is None:
            return None
        items = {m.name: const_to_py(m.value, sv_type) for m in sv_type
                 if type(m).__name__ == "EnumValueSymbol"}
        return DataTypeEnum(name=name, items=items, width=int(sv_type.bitWidth),
                            base_type=base)

    def _map_struct(self, sv_type, name, allow_4state: bool) -> Optional[DataTypeStruct]:
        fields = []
        for m in sv_type:
            if type(m).__name__ != "FieldSymbol":
                continue
            ft = self.map_type(m.type, allow_4state)
            if ft is None:
                return None
            f = Field(name=m.name, datatype=ft)
            if self.compilation is not None:
                from zuspec.fe.sv.function_mapper import sv_annotations
                f.annotations = sv_annotations(self.compilation, m)
            fields.append(f)
        return DataTypeStruct(name=name, super=None, packed=True, fields=fields)

    def _map_integral(self, sv_type, allow_4state: bool) -> Optional[DataTypeInt]:
        width = int(sv_type.bitWidth)
        signed = bool(sv_type.isSigned)
        if bool(getattr(sv_type, "isFourState", False)):
            if not allow_4state:
                self.error_reporter.error(
                    f"4-state type '{sv_type}' not supported",
                    suggestion=self._suggest_2state_replacement("logic"))
                return None
            return DataTypeInt(bits=width, signed=signed, four_state=True)
        return DataTypeInt(bits=width, signed=signed, four_state=False)

    def map_builtin_type(
        self,
        type_name: str,
        width: Optional[int] = None,
        signed: Optional[bool] = None,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        column: Optional[int] = None,
    ) -> Optional[DataTypeInt]:
        """Map a SystemVerilog builtin type to Zuspec IR.
        
        Args:
            type_name: The SV type name (e.g., 'bit', 'int', 'logic')
            width: Optional width override for bit vectors
            signed: Optional signed override
            file_path: Source file for error reporting
            line: Line number for error reporting
            column: Column number for error reporting
            
        Returns:
            DataTypeInt if successful, None on error
        """
        type_name_lower = type_name.lower()
        # ``int unsigned``, ``byte signed``: the keyword sets the sign
        sign_kw = None
        for kw in (' unsigned', ' signed'):
            if type_name_lower.endswith(kw):
                type_name_lower = type_name_lower[:-len(kw)].strip()
                sign_kw = kw == ' signed'
                break
        if sign_kw is not None and type_name_lower in (
                'byte', 'shortint', 'int', 'longint'):
            bits = {'byte': 8, 'shortint': 16, 'int': 32,
                    'longint': 64}[type_name_lower]
            return DataTypeInt(bits=bits, signed=sign_kw)
        if sign_kw is not None:
            signed = sign_kw

        # 4-state types: ``four_state`` is set
        if type_name_lower in ['logic', 'reg']:
            bits = width if width is not None else 1
            is_signed = signed if signed is not None else False
            return DataTypeInt(bits=bits, signed=is_signed, four_state=True)
        if type_name_lower == 'integer':
            return DataTypeInt(bits=32, signed=True if signed is None else signed,
                               four_state=True)
        
        # Map 2-state types
        if type_name_lower == 'bit':
            bits = width if width is not None else 1
            is_signed = signed if signed is not None else False
            return DataTypeInt(bits=bits, signed=is_signed)
        
        elif type_name_lower == 'byte':
            return DataTypeInt(bits=8, signed=True)
        
        elif type_name_lower == 'shortint':
            return DataTypeInt(bits=16, signed=True)
        
        elif type_name_lower == 'int':
            return DataTypeInt(bits=32, signed=True)
        
        elif type_name_lower == 'longint':
            return DataTypeInt(bits=64, signed=True)
        
        # Unsupported type
        self.error_reporter.error(
            f"Unsupported type '{type_name}'",
            file_path=file_path,
            line=line,
            column=column,
        )
        return None
    
    def _suggest_2state_replacement(self, type_name: str) -> str:
        """Suggest a 2-state replacement for a 4-state type."""
        replacements = {
            'logic': 'Use 2-state type: bit',
            'reg': 'Use 2-state type: bit',
            'integer': 'Use 2-state type: int',
        }
        return replacements.get(type_name, 'Use a 2-state type (bit, int, byte, etc.)')
    
    def map_zsp_reg_width(self, prop_sym) -> tuple:
        """Extract (bits, signed) from a zsp_reg_c #(T) ClassProperty symbol."""
        try:
            val = prop_sym.type.find('val')
            if val is None:
                return (32, False)
            t = val.type
            bits = int(t.bitWidth) if hasattr(t, 'bitWidth') else 32
            signed = bool(t.isSigned) if hasattr(t, 'isSigned') else False
            return (bits, signed)
        except Exception:
            return (32, False)

    def is_4state_type(self, type_name: str) -> bool:
        """Check if a type name is a 4-state type."""
        return type_name.lower() in ['logic', 'reg', 'integer']


def _type_param_alias(t) -> bool:
    """Whether slang alias type ``t`` is a type parameter's alias (``T`` in
    a specialization of ``put_if #(type T)``) rather than a typedef: its
    name is one key per generic, so it is mapped by its value."""
    p = getattr(t, "parentScope", None)
    m = p.find(t.name) if p is not None else None
    return type(m).__name__ == "TypeParameterSymbol"


def _class_local(alias) -> bool:
    """A typedef declared in a class (``p::cls::aw_t``), not a package."""
    return str(alias).count("::") >= 2
