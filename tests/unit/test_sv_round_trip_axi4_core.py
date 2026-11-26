"""SV -> IR -> SV round trip of the real AXI4 core (``axi4_core/``, from
fw-proto-axi-new): SVMapper maps its three packages, zuspec-be-sv's
``translate_ir_package`` emits each back, the output lints in Verilator,
re-maps to an equal IR and re-emits unchanged, and keeps what the source
says over the parameter ``P`` (fw-hdl protocol-kit-followups §18)."""
import os
import shutil
import subprocess

import pytest

pytest.importorskip("zuspec.be.sv")

from zuspec.be.sv.ir.core_to_sv import translate_ir_package
from zuspec.be.sv.ir.sv_emit import SVEmitter
from zuspec.fe.sv.config import SVToZuspecConfig
from zuspec.fe.sv.mapper import SVMapper

CORE = os.path.join(os.path.dirname(__file__), "axi4_core")
FILES = ["fw_proto_pkg.sv", "fw_proto_axi4_defs_pkg.sv", "fw_proto_axi4_core_pkg.sv"]


def _map(files, inc=()):
    m = SVMapper(SVToZuspecConfig(include_dirs=list(inc)))
    assert m.map_files(files), m.get_error_report()
    return m


def _emit(m, out_dir):
    out = {}
    for p in m.get_packages():
        sv = SVEmitter().emit_one(translate_ir_package(p, m.get_types(), m.get_decls()))
        path = os.path.join(out_dir, f"{p.name}.sv")
        with open(path, "w") as fp:
            fp.write(sv + "\n")
        out[p.name] = (path, sv)
    return out


@pytest.fixture(scope="module")
def round_trip(tmp_path_factory):
    d1, d2 = tmp_path_factory.mktemp("rt1"), tmp_path_factory.mktemp("rt2")
    m1 = _map([os.path.join(CORE, f) for f in FILES], [CORE])
    sv1 = _emit(m1, str(d1))
    m2 = _map([sv1[n][0] for n in ("fw_proto_pkg", "fw_proto_axi4_defs_pkg",
                                   "fw_proto_axi4_core_pkg")])
    sv2 = _emit(m2, str(d2))
    return m1, sv1, m2, sv2


def test_packages(round_trip):
    m1, _, _, _ = round_trip
    assert [p.name for p in m1.get_packages()] == [
        "fw_proto_pkg", "fw_proto_axi4_defs_pkg", "fw_proto_axi4_core_pkg"]
    defs = m1.get_packages()[1]
    assert [c.name for c in defs.constants] == ["AXI4_PARAM_DEFAULT"]
    assert {"w1", "max2", "axi4_burst_legal"} <= {f.name for f in defs.functions}
    core = m1.get_packages()[2]
    assert [i.package for i in core.imports] == ["fw_proto_pkg", "fw_proto_axi4_defs_pkg"]


def test_kept_over_p(round_trip):
    _, sv1, _, _ = round_trip
    core = sv1["fw_proto_axi4_core_pkg"][1]
    defs = sv1["fw_proto_axi4_defs_pkg"][1]
    assert "localparam axi4_param_t AXI4_PARAM_DEFAULT = '{ADDR_W: 32" in defs
    assert "function automatic bit [31:0] w1(input bit [31:0] w);" in defs
    assert "class axi4_types #(axi4_param_t P = AXI4_PARAM_DEFAULT);" in core
    assert "(* fw_opt = \"ID_W\" *) logic [w1(P.ID_W)-1:0] id;" in core
    assert "logic [w1(max2(P.AWUSER_W, P.ARUSER_W))-1:0] user;" in core
    assert "typedef logic [(P.DATA_W / 8)-1:0] strb_t;" in core
    assert "typedef axi4_types #(P) t;" in core
    assert "(* fw_fwd *) fw_rv_ch #(t::aw_t) aw;" in core
    assert "typedef axi4_types #(P)::aw_t aw_t;" in core
    assert "(* fw_api *)" in core
    assert "fw_roles = \"mgr,sub\"" in core
    # ``&&`` over ``||`` keeps its parentheses
    assert ("((burst == AXI4_BURST_WRAP) && (!(beats inside {2, 4, 8, 16}) || "
            "(axi4_aligned_addr(addr, size) != addr)))") in defs


def test_reparse_equal_ir(round_trip):
    m1, _, m2, _ = round_trip
    assert m1.get_types() == m2.get_types()
    d1 = {d.name: d for d in m1.get_decls()}
    d2 = {d.name: d for d in m2.get_decls()}
    assert d1.keys() == d2.keys()
    for n in d1:
        assert d1[n] == d2[n], n
    assert m1.get_packages() == m2.get_packages()


def test_emit_fixed_point(round_trip):
    _, sv1, _, sv2 = round_trip
    for n in sv1:
        assert sv1[n][1] == sv2[n][1], n


@pytest.mark.skipif(shutil.which("verilator") is None,
                    reason="verilator not on PATH")
def test_verilator_lint(round_trip):
    _, sv1, _, _ = round_trip
    files = [sv1[n][0] for n in ("fw_proto_pkg", "fw_proto_axi4_defs_pkg",
                                 "fw_proto_axi4_core_pkg")]
    r = subprocess.run(
        ["verilator", "--lint-only", "-Wall", "-Wno-UNUSEDSIGNAL",
         "-Wno-UNDRIVEN", "-Wno-UNUSEDPARAM", *files],
        capture_output=True, text=True, cwd=os.path.dirname(files[0]))
    assert r.returncode == 0, r.stderr
