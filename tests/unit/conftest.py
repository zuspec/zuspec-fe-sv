"""Shared helpers for the interpreter tests."""
import pytest

from zuspec.fe.sv import compile
from zuspec.fe.sv.interp import Interpreter


class SV:
    """A compiled snippet plus an interpreter over it."""

    def __init__(self, text: str, intrinsics=None, pkg: str = "p"):
        self.res = compile(extra_text={"t.sv": text}).check()
        self.comp = self.res.compilation
        self.pkg = self.comp.getPackage(pkg)
        self.it = Interpreter(self.comp, intrinsics)

    def fn(self, name: str):
        sub = self.pkg.find(name)
        assert sub is not None, name
        return sub

    def run(self, name: str, *args, **named):
        """Call package function *name*."""
        return self.it.call(self.fn(name), None, args, named)


@pytest.fixture
def sv():
    return SV
