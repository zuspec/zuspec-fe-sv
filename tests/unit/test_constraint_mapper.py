"""Unit tests for constraint_mapper — Phase 3."""
import os
import tempfile
import pytest

import pyslang

from zuspec.fe.sv.constraint_mapper import (
    ConstraintMapper,
    ActionConstraintSet,
    BitRangeExtraction,
    SVConditionalBody,
    ValidMarker,
    DontCareMarker,
    sv_const_to_int,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SHARE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'zuspec', 'fe', 'sv', 'share')
)
_LIB_FILES = [
    os.path.join(SHARE, 'zsp_clock_if.sv'),
    os.path.join(SHARE, 'zsp_reset_if.sv'),
    os.path.join(SHARE, 'zsp_reg_if.sv'),
    os.path.join(SHARE, 'zsp_pkg.sv'),
]


def _compile(src: str):
    """Compile SV source with the zsp library and return (compilation, root, trees).

    ``trees`` must stay alive as long as ``compilation`` is used — pyslang
    holds raw C++ pointers into the SyntaxTree objects.
    """
    sm = pyslang.SourceManager()
    sm.addUserDirectories(SHARE)
    comp = pyslang.ast.Compilation()
    trees = []
    for f in _LIB_FILES:
        t = pyslang.syntax.SyntaxTree.fromFile(f, sm)
        trees.append(t)
        comp.addSyntaxTree(t)
    with tempfile.NamedTemporaryFile(suffix='.sv', mode='w', delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        t = pyslang.syntax.SyntaxTree.fromFile(tmp, sm)
        trees.append(t)
        comp.addSyntaxTree(t)
    finally:
        os.unlink(tmp)
    return comp, comp.getRoot(), trees


def _find_class(root, name: str):
    found = []
    def visit(sym):
        if sym.kind == pyslang.ast.SymbolKind.ClassType and getattr(sym, 'name', '') == name:
            found.append(sym)
        return True
    root.visit(visit)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# sv_const_to_int helper tests
# ---------------------------------------------------------------------------

def test_sv_const_binary():
    class FakeC:
        def __str__(self): return "7'b1100011"
    assert sv_const_to_int(FakeC()) == 0b1100011


def test_sv_const_hex():
    class FakeC:
        def __str__(self): return "7'h63"
    assert sv_const_to_int(FakeC()) == 0x63


def test_sv_const_decimal():
    class FakeC:
        def __str__(self): return "32'd42"
    assert sv_const_to_int(FakeC()) == 42


def test_sv_const_plain_int():
    class FakeC:
        def __str__(self): return "0"
    assert sv_const_to_int(FakeC()) == 0


# ---------------------------------------------------------------------------
# ConstraintMapper tests
# ---------------------------------------------------------------------------

_SIMPLE_DECODE_SRC = """`include "zsp_macros.svh"
package cm_test_pkg;
    import zsp_pkg::*;

    class my_core_c extends zsp_component;
        `zsp_component_utils(my_core_c)
    endclass

    class simple_decode_c extends zsp_action_c #(my_core_c);
        logic [31:0]      instr;
        rand logic [6:0]  opcode;
        rand logic [2:0]  funct3;
        rand logic [3:0]  alu_op;

        constraint c_extract {
            opcode == instr[6:0];
            funct3 == instr[14:12];
        }

        constraint c_add {
            if (opcode == 7'b0110011 && funct3 == 3'b000) {
                alu_op == 4'b0000;
                zsp_mark #(logic[3:0])::valid(alu_op);
            }
        }

        constraint c_sub {
            if (opcode == 7'b0110011 && funct3 == 3'b000) {
                alu_op == 4'b0001;
                zsp_mark #(logic[3:0])::dont_care(funct3);
            }
        }

        `zsp_action_utils(simple_decode_c, my_core_c)
    endclass
endpackage
"""


@pytest.fixture(scope='module')
def simple_acs():
    comp, root, _trees = _compile(_SIMPLE_DECODE_SRC)  # _trees kept alive
    sym = _find_class(root, 'simple_decode_c')
    assert sym is not None
    mapper = ConstraintMapper(compilation=comp)
    return mapper.map(sym, comp_type_name='my_core_c')


def test_acs_basic(simple_acs):
    """ActionConstraintSet has expected class/comp names."""
    assert simple_acs.class_name == 'simple_decode_c'
    assert simple_acs.comp_type_name == 'my_core_c'


def test_acs_input_field(simple_acs):
    """Primary input field is 'instr' (non-rand, 32-bit)."""
    assert simple_acs.input_field == 'instr'
    assert simple_acs.input_width == 32


def test_acs_fields(simple_acs):
    """Field info: instr=input, opcode/funct3/alu_op=rand."""
    by_name = {f.name: f for f in simple_acs.fields}
    assert by_name['instr'].is_input
    assert not by_name['instr'].is_rand
    assert by_name['opcode'].is_rand
    assert by_name['funct3'].is_rand
    assert by_name['alu_op'].is_rand


def test_acs_extractions(simple_acs):
    """Extraction constraints opcode==instr[6:0] and funct3==instr[14:12] are captured."""
    by_field = {e.field_name: e for e in simple_acs.extractions}
    assert 'opcode' in by_field
    assert by_field['opcode'].msb == 6
    assert by_field['opcode'].lsb == 0
    assert by_field['opcode'].input_field == 'instr'

    assert 'funct3' in by_field
    assert by_field['funct3'].msb == 14
    assert by_field['funct3'].lsb == 12


def test_acs_constraint_blocks_count(simple_acs):
    """Three constraint blocks (c_extract, c_add, c_sub)."""
    names = [b.name for b in simple_acs.constraint_blocks]
    assert 'c_extract' in names
    assert 'c_add' in names
    assert 'c_sub' in names


def test_acs_c_add_guard(simple_acs):
    """c_add guard has opcode==0x33 and funct3==0."""
    block = next(b for b in simple_acs.constraint_blocks if b.name == 'c_add')
    assert block.conditional is not None
    conds = {c.field_name: c.value for c in block.conditional.guard_conditions}
    assert conds['opcode'] == 0b0110011   # 0x33
    assert conds['funct3'] == 0


def test_acs_c_add_assignment(simple_acs):
    """c_add body assigns alu_op == 0."""
    block = next(b for b in simple_acs.constraint_blocks if b.name == 'c_add')
    assert block.conditional.assignments.get('alu_op') == 0


def test_acs_c_add_valid_marker(simple_acs):
    """c_add has zsp_mark::valid(alu_op)."""
    block = next(b for b in simple_acs.constraint_blocks if b.name == 'c_add')
    valid_fields = [m.field_name for m in block.conditional.valid_markers]
    assert 'alu_op' in valid_fields


def test_acs_c_sub_dont_care(simple_acs):
    """c_sub has zsp_mark::dont_care(funct3)."""
    block = next(b for b in simple_acs.constraint_blocks if b.name == 'c_sub')
    dc_fields = [m.field_name for m in block.conditional.dont_care_markers]
    assert 'funct3' in dc_fields
