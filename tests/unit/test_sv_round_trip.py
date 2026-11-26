"""SV -> IR -> SV round trip of an AXI4-sized API and payload package:
SVMapper maps it, zuspec-be-sv's ``translate_package`` emits it back, the
output lints in Verilator and maps to an equal IR."""
import os
import shutil
import subprocess

import pytest

pytest.importorskip("zuspec.be.sv")

from zuspec.be.sv.ir.core_to_sv import translate_package
from zuspec.be.sv.ir.sv_emit import SVEmitter
from zuspec.fe.sv.mapper import SVMapper

PKG = "axi4_api_pkg"
SRC = os.path.join(os.path.dirname(__file__), f"{PKG}.sv")


def _map(text):
    m = SVMapper()
    assert m.map_text(text, f"{PKG}.sv"), m.get_error_report()
    return m


def _emit(m):
    return SVEmitter().emit_one(translate_package(PKG, m.get_types(), m.get_decls()))


def _decls(m):
    """Declarations by name (methods keep source order through the
    emitter)."""
    return {d.name: d for d in m.get_decls()}


@pytest.fixture(scope="module")
def round_trip():
    with open(SRC) as fp:
        m1 = _map(fp.read())
    sv = _emit(m1)
    return m1, sv, _map(sv)


def test_types_and_order(round_trip):
    _, sv, _ = round_trip
    # every typedef precedes its users; base and interfaces precede the class
    pos = {n: sv.index(n) for n in (
        "} axi4_resp_e;", "typedef bit [3:0] id_t;", "} aw_t;",
        "typedef aw_t ar_t;", "class axi4_xact_base;",
        "interface class axi4_burst_req_if;", "class axi4_xact_mem #(",
        "class axi4_xact_user;")}
    assert pos["typedef bit [3:0] id_t;"] < pos["} aw_t;"] < pos["typedef aw_t ar_t;"]
    assert pos["} axi4_resp_e;"] < pos["class axi4_xact_base;"]
    assert pos["class axi4_xact_base;"] < pos["class axi4_xact_mem #("]
    assert pos["interface class axi4_burst_req_if;"] < pos["class axi4_xact_mem #("]
    assert pos["class axi4_xact_mem #("] < pos["class axi4_xact_user;"]
    assert "typedef enum bit[1:0] {" in sv
    assert "bit [DATA_W-1:0] mem [$];" in sv
    assert "axi4_xact_mem #(48, 128) wide;" in sv


def test_reparse_equal_ir(round_trip):
    m1, _, m2 = round_trip
    assert m1.get_types() == m2.get_types()
    d1, d2 = _decls(m1), _decls(m2)
    assert d1.keys() == d2.keys()
    for n in d1:
        assert d1[n] == d2[n], n


def test_emit_fixed_point(round_trip):
    _, sv, _ = round_trip
    assert _emit(_map(sv)) == sv


@pytest.mark.skipif(shutil.which("verilator") is None,
                    reason="verilator not on PATH")
def test_verilator_lint(round_trip, tmp_path):
    _, sv, _ = round_trip
    path = tmp_path / f"{PKG}.sv"
    path.write_text(sv + "\n")
    r = subprocess.run(
        ["verilator", "--lint-only", "-Wall", "-Wno-UNUSEDSIGNAL",
         "-Wno-UNDRIVEN", str(path)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
