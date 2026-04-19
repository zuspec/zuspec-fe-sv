"""SV action constraint extraction — Phase 3 of the fe-sv pipeline.

Reads a pyslang action class symbol and produces an ``ActionConstraintSet``,
an SV-specific intermediate representation of the constraint structure.

The ``ActionConstraintSet`` is later consumed by
``ConstraintCompiler.from_sv_action()`` (Phase 4) to produce a
``ConstraintBlockSet`` suitable for synthesis.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pyslang


# ---------------------------------------------------------------------------
# SV-specific intermediate representation
# ---------------------------------------------------------------------------

@dataclass
class SVFieldInfo:
    """Metadata for one field in an SV action class."""
    name: str
    width: int
    is_rand: bool
    is_input: bool    # non-rand (RandMode.None_) — driven from outside
    is_output: bool   # marked (*zsp_output*) — meaningful output


@dataclass
class BitRangeExtraction:
    """``derived_field == input_field[msb:lsb]`` extraction constraint."""
    field_name: str
    input_field: str  # name of the input field (e.g. "instr")
    msb: int
    lsb: int


@dataclass
class SVConstraintExpr:
    """A single ``field == value`` equality term inside a guard."""
    field_name: str
    value: int


@dataclass
class ValidMarker:
    """``zsp_mark::valid(field)`` call inside a constraint block."""
    field_name: str


@dataclass
class DontCareMarker:
    """``zsp_mark::dont_care(field)`` call inside a constraint block."""
    field_name: str


@dataclass
class SVConditionalBody:
    """The body of one ``if (guard) { ... }`` branch in a constraint block."""
    guard_conditions: List[SVConstraintExpr]   # AND of equality conditions
    assignments: Dict[str, int]                # field_name → assigned value
    valid_markers: List[ValidMarker] = field(default_factory=list)
    dont_care_markers: List[DontCareMarker] = field(default_factory=list)


@dataclass
class ActionConstraintBlock:
    """One ``constraint c_X { ... }`` block from the SV action class.

    ``conditional`` is ``None`` for unconditional (extraction) blocks.
    """
    name: str
    conditional: Optional[SVConditionalBody] = None
    extractions: List[BitRangeExtraction] = field(default_factory=list)


@dataclass
class ActionConstraintSet:
    """All parsed constraint info from one SV action class."""
    class_name: str
    comp_type_name: Optional[str]
    fields: List[SVFieldInfo]
    input_field: Optional[str]   # primary non-rand field (e.g. "instr")
    input_width: int             # bit width of the input field
    extractions: List[BitRangeExtraction]  # field-slice wiring
    constraint_blocks: List[ActionConstraintBlock]


# ---------------------------------------------------------------------------
# Integer literal helper
# ---------------------------------------------------------------------------

def sv_const_to_int(constant_value) -> int:
    """Convert a pyslang ConstantValue to a Python int.

    Handles formats like ``7'b1100011``, ``3'h4``, ``32'd0``, plain ``0``.
    """
    s = str(constant_value)
    if "'" in s:
        parts = s.split("'")
        base_char = parts[1][0].lower()
        val_str = parts[1][1:]
        bases = {'b': 2, 'd': 10, 'h': 16, 'o': 8}
        return int(val_str, bases.get(base_char, 10))
    return int(s)


# ---------------------------------------------------------------------------
# ConstraintMapper
# ---------------------------------------------------------------------------

class ConstraintMapper:
    """Extracts constraint structure from a pyslang action class symbol.

    Usage::

        mapper = ConstraintMapper(compilation)
        acs = mapper.map(action_class_symbol)
    """

    def __init__(self, compilation=None):
        self.compilation = compilation

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def map(self, class_sym, comp_type_name: Optional[str] = None) -> ActionConstraintSet:
        """Return an ``ActionConstraintSet`` for the given action class symbol."""
        class_name = class_sym.name

        # --- Collect field info ---
        fields = self._collect_fields(class_sym)

        # Determine primary input field (first non-rand field)
        input_field = None
        input_width = 0
        for fi in fields:
            if fi.is_input:
                input_field = fi.name
                input_width = fi.width
                break

        # --- Parse constraint blocks ---
        extractions: List[BitRangeExtraction] = []
        blocks: List[ActionConstraintBlock] = []

        for sym in class_sym:
            if sym.kind != pyslang.SymbolKind.ConstraintBlock:
                continue
            try:
                block = self._map_constraint_block(sym, input_field)
                if block is not None:
                    # Hoist top-level extractions into the global list
                    extractions.extend(block.extractions)
                    blocks.append(block)
            except Exception:
                pass  # skip unparseable blocks

        return ActionConstraintSet(
            class_name=class_name,
            comp_type_name=comp_type_name,
            fields=fields,
            input_field=input_field,
            input_width=input_width,
            extractions=extractions,
            constraint_blocks=blocks,
        )

    # ------------------------------------------------------------------
    # Field collection
    # ------------------------------------------------------------------

    _SKIP = frozenset({
        'new', 'create', 'm_comp',
        'randomize', 'pre_randomize', 'post_randomize',
        'get_randstate', 'set_randstate', 'srandom',
        'rand_mode', 'constraint_mode',
        'm_parent', 'm_children', 'clock_d', 'm_clock_domains',
    })

    def _collect_fields(self, class_sym) -> List[SVFieldInfo]:
        result = []
        for sym in class_sym:
            if sym.name in self._SKIP:
                continue
            if sym.kind != pyslang.SymbolKind.ClassProperty:
                continue
            try:
                vtype = sym.type
                if not isinstance(vtype, pyslang.IntegralType):
                    continue
                width = int(vtype.bitWidth) if hasattr(vtype, 'bitWidth') else 1
                is_rand = sym.randMode == pyslang.RandMode.Rand
                is_out = self._is_output_attr(sym)
                result.append(SVFieldInfo(
                    name=sym.name,
                    width=width,
                    is_rand=is_rand,
                    is_input=not is_rand,
                    is_output=is_out,
                ))
            except Exception:
                pass
        return result

    def _is_output_attr(self, sym) -> bool:
        if self.compilation is None:
            return False
        try:
            attrs = self.compilation.getAttributes(sym)
            return any(getattr(a, 'name', None) == 'zsp_output' for a in attrs)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Constraint block parsing
    # ------------------------------------------------------------------

    def _map_constraint_block(
        self, constraint_sym, input_field: Optional[str]
    ) -> Optional[ActionConstraintBlock]:
        """Parse one constraint block symbol into an ``ActionConstraintBlock``."""
        block_name = constraint_sym.name
        block = ActionConstraintBlock(name=block_name)

        constraint_list = getattr(constraint_sym, 'constraints', None)
        if constraint_list is None:
            return block

        items = list(getattr(constraint_list, 'list', []))
        for item in items:
            self._process_constraint_item(item, block, input_field)

        return block

    def _process_constraint_item(self, item, block: ActionConstraintBlock,
                                  input_field: Optional[str]) -> None:
        kind = getattr(item, 'kind', None)

        if str(kind) == 'ConstraintKind.Conditional':
            cond_body = self._parse_conditional(item, input_field)
            if cond_body is not None:
                block.conditional = cond_body

        elif str(kind) == 'ConstraintKind.Expression':
            expr = getattr(item, 'expr', None)
            if expr is None:
                return
            # Check for extraction: field == instr[msb:lsb]
            extr = self._try_parse_extraction(expr, input_field)
            if extr is not None:
                block.extractions.append(extr)

        elif str(kind) == 'ConstraintKind.List':
            sub_items = list(getattr(item, 'list', []))
            for sub in sub_items:
                self._process_constraint_item(sub, block, input_field)

    # ------------------------------------------------------------------
    # Conditional constraint parsing
    # ------------------------------------------------------------------

    def _parse_conditional(self, cond_sym, input_field: Optional[str]
                           ) -> Optional[SVConditionalBody]:
        """Parse ``if (guard) { body }`` constraint."""
        predicate = getattr(cond_sym, 'predicate', None)
        if_body = getattr(cond_sym, 'ifBody', None)
        if predicate is None or if_body is None:
            return None

        guard_conds = self._parse_guard(predicate)
        if guard_conds is None:
            return None

        assignments: Dict[str, int] = {}
        valid_markers: List[ValidMarker] = []
        dont_care_markers: List[DontCareMarker] = []

        body_items = list(getattr(if_body, 'list', []))
        for item in body_items:
            kind = str(getattr(item, 'kind', ''))
            if kind == 'ConstraintKind.Expression':
                expr = getattr(item, 'expr', None)
                if expr is None:
                    continue
                # Check for zsp_mark calls
                marker = self._try_parse_zsp_mark(expr)
                if marker is not None:
                    if isinstance(marker, ValidMarker):
                        valid_markers.append(marker)
                    else:
                        dont_care_markers.append(marker)
                    continue
                # Otherwise try as assignment: field == value
                asgn = self._try_parse_assignment(expr)
                if asgn is not None:
                    assignments[asgn[0]] = asgn[1]
            elif kind == 'ConstraintKind.List':
                sub_items = list(getattr(item, 'list', []))
                for sub in sub_items:
                    sub_kind = str(getattr(sub, 'kind', ''))
                    if sub_kind == 'ConstraintKind.Expression':
                        expr = getattr(sub, 'expr', None)
                        if expr is None:
                            continue
                        marker = self._try_parse_zsp_mark(expr)
                        if marker is not None:
                            if isinstance(marker, ValidMarker):
                                valid_markers.append(marker)
                            else:
                                dont_care_markers.append(marker)
                            continue
                        asgn = self._try_parse_assignment(expr)
                        if asgn is not None:
                            assignments[asgn[0]] = asgn[1]

        return SVConditionalBody(
            guard_conditions=guard_conds,
            assignments=assignments,
            valid_markers=valid_markers,
            dont_care_markers=dont_care_markers,
        )

    # ------------------------------------------------------------------
    # Guard expression parsing
    # ------------------------------------------------------------------

    def _parse_guard(self, expr) -> Optional[List[SVConstraintExpr]]:
        """Flatten a ``&&``-chain of ``field == value`` expressions."""
        if expr is None:
            return None
        ek = str(getattr(expr, 'kind', ''))

        if ek == 'ExpressionKind.BinaryOp':
            op = str(getattr(expr, 'op', ''))
            if op == 'BinaryOperator.LogicalAnd':
                left = self._parse_guard(expr.left)
                right = self._parse_guard(expr.right)
                if left is None or right is None:
                    return None
                return left + right
            if op == 'BinaryOperator.Equality':
                eq = self._parse_equality(expr)
                if eq is not None:
                    return [eq]
                return None
            return None

        if ek == 'ExpressionKind.Conversion':
            return self._parse_guard(getattr(expr, 'operand', None))

        return None

    def _parse_equality(self, expr) -> Optional[SVConstraintExpr]:
        """Parse ``field == literal`` or ``literal == field``."""
        left = getattr(expr, 'left', None)
        right = getattr(expr, 'right', None)
        if left is None or right is None:
            return None

        # Unwrap conversions
        left = self._unwrap_conv(left)
        right = self._unwrap_conv(right)

        field_name = self._get_named_value(left)
        value = self._get_integer_literal(right)
        if field_name is None or value is None:
            field_name = self._get_named_value(right)
            value = self._get_integer_literal(left)
        if field_name is None or value is None:
            return None
        return SVConstraintExpr(field_name=field_name, value=value)

    # ------------------------------------------------------------------
    # Body expression parsing
    # ------------------------------------------------------------------

    def _try_parse_assignment(self, expr) -> Optional[tuple]:
        """Parse ``field == value`` assignment inside a constraint body."""
        ek = str(getattr(expr, 'kind', ''))
        if ek == 'ExpressionKind.BinaryOp':
            op = str(getattr(expr, 'op', ''))
            if op == 'BinaryOperator.Equality':
                return self._parse_equality_tuple(expr)
        if ek == 'ExpressionKind.Conversion':
            return self._try_parse_assignment(getattr(expr, 'operand', None))
        return None

    def _parse_equality_tuple(self, expr) -> Optional[tuple]:
        left = self._unwrap_conv(getattr(expr, 'left', None))
        right = self._unwrap_conv(getattr(expr, 'right', None))
        field_name = self._get_named_value(left)
        value = self._get_integer_literal(right)
        if field_name is None or value is None:
            field_name = self._get_named_value(right)
            value = self._get_integer_literal(left)
        if field_name is None or value is None:
            return None
        return (field_name, value)

    def _try_parse_extraction(self, expr, input_field: Optional[str]
                              ) -> Optional[BitRangeExtraction]:
        """Parse ``derived_field == input[msb:lsb]`` extraction constraint."""
        ek = str(getattr(expr, 'kind', ''))
        if ek == 'ExpressionKind.Conversion':
            return self._try_parse_extraction(
                getattr(expr, 'operand', None), input_field)
        if ek != 'ExpressionKind.BinaryOp':
            return None
        op = str(getattr(expr, 'op', ''))
        if op != 'BinaryOperator.Equality':
            return None

        left = self._unwrap_conv(getattr(expr, 'left', None))
        right = self._unwrap_conv(getattr(expr, 'right', None))

        # field == input[msb:lsb]
        field_name = self._get_named_value(left)
        rng = self._get_range_select(right)
        if field_name is None or rng is None:
            field_name = self._get_named_value(right)
            rng = self._get_range_select(left)
        if field_name is None or rng is None:
            return None

        base_name, msb, lsb = rng
        if input_field and base_name != input_field:
            return None
        return BitRangeExtraction(
            field_name=field_name,
            input_field=base_name,
            msb=msb,
            lsb=lsb,
        )

    def _try_parse_zsp_mark(self, expr):
        """Detect and parse ``zsp_mark #(T)::valid(field)`` calls.

        Returns ``ValidMarker`` or ``DontCareMarker``, or ``None``.
        """
        ek = str(getattr(expr, 'kind', ''))
        if ek == 'ExpressionKind.Conversion':
            return self._try_parse_zsp_mark(getattr(expr, 'operand', None))
        if ek != 'ExpressionKind.Call':
            return None

        subroutine = getattr(expr, 'subroutine', None)
        if subroutine is None:
            return None
        path = getattr(subroutine, 'hierarchicalPath', '') or ''
        if 'zsp_mark' not in path:
            return None

        method_name = getattr(expr, 'subroutineName', None)
        args = getattr(expr, 'arguments', None)
        if args is None:
            return None
        try:
            arg0 = list(args)[0]
        except (StopIteration, IndexError, TypeError):
            return None

        field_name = None
        try:
            sym_ref = arg0.getSymbolReference()
            if sym_ref is not None:
                field_name = sym_ref.name
        except Exception:
            pass
        if field_name is None:
            return None

        if method_name == 'valid':
            return ValidMarker(field_name=field_name)
        if method_name == 'dont_care':
            return DontCareMarker(field_name=field_name)
        return None

    # ------------------------------------------------------------------
    # Expression utilities
    # ------------------------------------------------------------------

    def _unwrap_conv(self, expr):
        """Peel off Conversion wrappers."""
        while expr is not None and str(getattr(expr, 'kind', '')) == 'ExpressionKind.Conversion':
            expr = getattr(expr, 'operand', None)
        return expr

    def _get_named_value(self, expr) -> Optional[str]:
        """Return the field name if expr is a NamedValue expression."""
        if expr is None:
            return None
        ek = str(getattr(expr, 'kind', ''))
        if ek == 'ExpressionKind.NamedValue':
            try:
                sym_ref = expr.getSymbolReference()
                if sym_ref is not None:
                    return sym_ref.name
            except Exception:
                pass
        return None

    def _get_integer_literal(self, expr) -> Optional[int]:
        """Return the integer value if expr evaluates to a constant integer.

        Handles:
        - ``ExpressionKind.IntegerLiteral``  — inline literal
        - ``ExpressionKind.NamedValue``       — localparam reference (constant-folded)
        - ``ExpressionKind.Conversion``       — type conversion wrapping any of the above
        - Any expression with a ``.constant`` attribute (pyslang constant-folds these)
        """
        if expr is None:
            return None
        # Unwrap conversions first
        while str(getattr(expr, 'kind', '')) == 'ExpressionKind.Conversion':
            expr = getattr(expr, 'operand', None)
            if expr is None:
                return None
        # Try the .constant attribute — pyslang constant-folds literals, localparams, etc.
        c = getattr(expr, 'constant', None)
        if c is not None:
            try:
                return sv_const_to_int(c)
            except Exception:
                pass
        return None

    def _get_range_select(self, expr) -> Optional[tuple]:
        """Return ``(base_name, msb, lsb)`` if expr is a RangeSelect or ElementSelect."""
        if expr is None:
            return None
        ek = str(getattr(expr, 'kind', ''))

        if ek == 'ExpressionKind.RangeSelect':
            base = self._unwrap_conv(getattr(expr, 'value', None))
            base_name = self._get_named_value(base)
            if base_name is None:
                return None
            left_expr = getattr(expr, 'left', None)
            right_expr = getattr(expr, 'right', None)
            msb = self._get_integer_literal(left_expr)
            lsb = self._get_integer_literal(right_expr)
            if msb is None or lsb is None:
                return None
            return (base_name, msb, lsb)

        if ek == 'ExpressionKind.ElementSelect':
            # Single-bit selection: instr[30] → BitRange(30, 30)
            base = self._unwrap_conv(getattr(expr, 'value', None))
            base_name = self._get_named_value(base)
            if base_name is None:
                return None
            selector = getattr(expr, 'selector', None)
            if selector is None:
                return None
            # IntegerLiteral has a .value attribute (SVInt), use sv_const_to_int
            sv_val = getattr(selector, 'value', None)
            if sv_val is None:
                return None
            try:
                bit = sv_const_to_int(sv_val)
            except Exception:
                return None
            return (base_name, bit, bit)

        return None
