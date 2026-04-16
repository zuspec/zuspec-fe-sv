"""End-to-end test: parse counter SV files and verify DataTypeComponent IR."""
import os
import pytest
from zuspec.fe.sv.mapper import SVMapper
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.dataclasses.ir.data_type import DataTypeComponent, Process
from zuspec.dataclasses.ir.stmt import StmtWhile
from zuspec.dataclasses.ir.expr import (
    ExprAwait, ExprCall, ExprAttribute, ExprRefField,
)

EXAMPLES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'examples', '01_counter')
)
COUNTER_PKG = os.path.join(EXAMPLES_DIR, 'counter_pkg.sv')


def _get_component(mapper: SVMapper, name: str) -> DataTypeComponent:
    comps = mapper.get_components()
    for c in comps:
        if c.name == name:
            return c
    names = [c.name for c in comps]
    raise AssertionError(f"Component '{name}' not found; found: {names}")


@pytest.fixture
def counter_mapper():
    cfg = SVToZuspecConfig()
    m = SVMapper(config=cfg)
    ok = m.map_files([COUNTER_PKG])
    errors = m.get_error_report()
    assert ok, f"map_files failed:\n{errors}"
    return m


def test_components_found(counter_mapper):
    comps = counter_mapper.get_components()
    assert len(comps) >= 1, "Expected at least one component"
    names = [c.name for c in comps]
    assert 'counter_c' in names, f"counter_c not in {names}"


def test_counter_has_reg_field(counter_mapper):
    comp = _get_component(counter_mapper, 'counter_c')
    assert len(comp.fields) >= 1, "Expected at least one field"
    count_field = next((f for f in comp.fields if f.name == 'count'), None)
    assert count_field is not None, f"'count' field missing; fields={[f.name for f in comp.fields]}"
    assert count_field.is_reg, "'count' should be is_reg=True"
    assert count_field.datatype.bits == 32, f"Expected 32 bits, got {count_field.datatype.bits}"


def test_counter_has_run_process(counter_mapper):
    comp = _get_component(counter_mapper, 'counter_c')
    assert len(comp.proc_processes) >= 1, "Expected at least one proc_process"
    run = next((p for p in comp.proc_processes if p.name == 'run'), None)
    assert run is not None, "Expected a 'run' process"


def test_run_has_forever_loop(counter_mapper):
    comp = _get_component(counter_mapper, 'counter_c')
    run = next(p for p in comp.proc_processes if p.name == 'run')
    assert len(run.body) >= 1, "run body is empty"
    while_stmt = run.body[0]
    assert isinstance(while_stmt, StmtWhile), \
        f"Expected StmtWhile, got {type(while_stmt).__name__}"


def test_run_body_has_write_call(counter_mapper):
    comp = _get_component(counter_mapper, 'counter_c')
    run = next(p for p in comp.proc_processes if p.name == 'run')
    while_stmt = run.body[0]

    # Walk the while body looking for an ExprAwait around a write call
    def find_await(stmts):
        for s in stmts:
            expr = getattr(s, 'value', None)
            if expr is None:
                expr = getattr(s, 'expr', None)
            if isinstance(expr, ExprAwait):
                return expr
            # recurse into nested bodies
            for attr in ('body', 'orelse'):
                sub = getattr(s, attr, [])
                found = find_await(sub)
                if found:
                    return found
        return None

    await_expr = find_await(while_stmt.body)
    assert await_expr is not None, "No ExprAwait found in run body"

    call = await_expr.value
    assert isinstance(call, ExprCall), f"Expected ExprCall inside ExprAwait, got {type(call).__name__}"
    func = call.func
    assert isinstance(func, ExprAttribute), \
        f"Expected ExprAttribute as call func, got {type(func).__name__}"
    assert func.attr in ('write', 'read'), f"Unexpected method name: {func.attr}"
    assert isinstance(func.value, ExprRefField), \
        f"Expected ExprRefField as receiver, got {type(func.value).__name__}"
