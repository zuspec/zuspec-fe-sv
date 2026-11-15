"""CLI plugin for zuspec-fe-sv.

Registers :class:`SVFrontend` with the zuspec-cli
:class:`~zuspec.cli.Registry`.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, TYPE_CHECKING

from zuspec.cli.plugin import Plugin
from zuspec.cli.frontend import Frontend
from zuspec.cli.ir import IR

if TYPE_CHECKING:
    from zuspec.cli.registry import Registry

_SHARE = Path(__file__).parent / "share"
_LIB_FILES = [
    str(_SHARE / f)
    for f in [
        "zsp_clock_if.sv",
        "zsp_reset_if.sv",
        "zsp_reg_if.sv",
        "zsp_pkg.sv",
    ]
]


class SVFrontend(Frontend):
    """SystemVerilog front-end — parses ``.sv`` / ``.svh`` files via pyslang
    and produces a :class:`~zuspec.cli.IR` wrapping a
    ``ConstraintCompiler`` instance.
    """

    @property
    def name(self) -> str:
        return "sv"

    @property
    def description(self) -> str:
        return "SystemVerilog action class (*.sv / *.svh)"

    @property
    def file_extensions(self) -> List[str]:
        return [".sv", ".svh", ".pkg"]

    def load(self, files: List[str], args: argparse.Namespace) -> IR:
        import pyslang
        from zuspec.fe.sv.class_mapper import ClassMapper
        from zuspec.fe.sv.error import ErrorReporter
        from zuspec.fe.sv.config import SVToZuspecConfig
        from zuspec.fe.sv.type_mapper import TypeMapper
        from zuspec.synth.sprtl.constraint_compiler import ConstraintCompiler

        sm = pyslang.SourceManager()
        sm.addUserDirectories(str(_SHARE))
        comp = pyslang.ast.Compilation()
        trees = []
        for f in _LIB_FILES + list(files):
            t = pyslang.syntax.SyntaxTree.fromFile(f, sm)
            trees.append(t)
            comp.addSyntaxTree(t)

        top_name = args.top
        target = None

        def visit(sym):
            nonlocal target
            if (
                sym.kind == pyslang.ast.SymbolKind.ClassType
                and sym.name == top_name
            ):
                target = sym
            return True

        comp.getRoot().visit(visit)
        if target is None:
            raise ValueError(
                f"Class '{top_name}' not found in SV sources"
            )

        config = SVToZuspecConfig()
        er = ErrorReporter()
        mapper = ClassMapper(config, er, TypeMapper(config, er), compilation=comp)
        action_ir = mapper.map_action(target)
        if action_ir is None:
            raise ValueError(
                f"'{top_name}' is not a zsp_action_c subclass"
            )

        cc = ConstraintCompiler.from_sv_action(
            action_ir, prefix=getattr(args, "be_prefix", "d")
        )

        return IR(
            payload=cc,
            kind="zuspec.constraint.compiler",
            provenance=f"sv:{top_name}",
            metadata={"trees": trees, "comp": comp},
        )


class SVPlugin(Plugin):
    """Plugin that registers :class:`SVFrontend` for ``.sv`` / ``.svh`` files."""

    @property
    def name(self) -> str:
        return "zuspec-fe-sv"

    @property
    def description(self) -> str:
        return "SystemVerilog front-end via pyslang"

    def register(self, registry: "Registry") -> None:
        registry.add_frontend(SVFrontend())
