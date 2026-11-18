"""Compile SystemVerilog sources with pyslang, for hosts of the interpreter.

Written against the pyslang 12 namespaced API (``pyslang.syntax`` /
``pyslang.ast`` / ``pyslang.parsing.PreprocessorOptions``). The compile set-up
is ported from fw-hdl's ``python/fw/hdl/fe/parser.py`` with the fw-hdl library
file lists removed: the host passes every file it wants.

Entry points:

- :func:`compile` -- files, include dirs, defines, extra in-memory sources and
  in-memory include files to a :class:`CompileResult`, which unpacks as
  ``(compilation, diagnostics)``.
- :func:`find_class` -- a class (``"pkg::name"``) by name; a generic class
  gives its default specialization.
- :func:`class_from_typedef` -- a class specialization named by a typedef,
  e.g. ``module __top; typedef fifo_chain#(bit[7:0]) T; endmodule``.
- :func:`extern_stub_bodies` -- default out-of-block bodies for ``extern``
  method prototypes that have none, so declarations-only code elaborates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import pyslang
from pyslang import ast, syntax

__all__ = [
    "CompileError", "CompileResult", "Diagnostic", "Diagnostics",
    "compile", "release", "find_class", "class_from_typedef", "extern_stub_bodies",
]

# Directory name under which in-memory include files are registered. It does
# not exist on disk; the preprocessor finds the files in the source manager.
_VIRTUAL_INC_DIR = "/__zuspec_fe_sv_virtual__"


@dataclass
class Diagnostic:
    """One compiler diagnostic, already formatted."""
    is_error: bool
    code: str
    text: str           # the full slang report (location, message, caret)

    def __str__(self) -> str:
        return self.text


class Diagnostics(list):
    """The diagnostics of a compilation (a list of :class:`Diagnostic`)."""

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self if d.is_error]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self if not d.is_error]

    @property
    def has_errors(self) -> bool:
        return any(d.is_error for d in self)

    def report(self, errors_only: bool = True) -> str:
        return "\n".join(d.text for d in self if d.is_error or not errors_only)


class CompileError(Exception):
    """A compilation had errors (raised by :meth:`CompileResult.check` and by
    :func:`compile` with ``check=True``)."""

    def __init__(self, diagnostics: Diagnostics):
        self.diagnostics = diagnostics
        errs = diagnostics.errors
        head = f"{len(errs)} SystemVerilog compile error(s)"
        super().__init__(head + ("\n" + diagnostics.report() if errs else ""))


class CompileResult(tuple):
    """``(compilation, diagnostics)``, plus the objects the compilation needs
    alive (source manager, syntax trees)."""

    def __new__(cls, compilation, diagnostics, source_manager, trees):
        self = super().__new__(cls, (compilation, diagnostics))
        self.source_manager = source_manager
        self.trees = trees
        return self

    @property
    def compilation(self) -> ast.Compilation:
        return self[0]

    @property
    def diagnostics(self) -> Diagnostics:
        return self[1]

    def check(self) -> "CompileResult":
        """Raise :class:`CompileError` if there were errors; else return self."""
        if self.diagnostics.has_errors:
            raise CompileError(self.diagnostics)
        return self


# Strong refs to source managers / trees for compilations handed out without
# their CompileResult (the C++ side references the source manager).
_KEEPALIVE: Dict[int, tuple] = {}


def compile(files: Sequence[str] = (), incdirs: Sequence[str] = (),
            defines: Optional[Mapping[str, Optional[str]]] = None,
            extra_text: Optional[Mapping[str, str]] = None,
            include_text: Optional[Mapping[str, str]] = None,
            check: bool = False, timescale: Optional[str] = None) -> CompileResult:
    """Compile SystemVerilog sources.

    :param files: source files, compiled in order (one compilation unit each).
    :param incdirs: include directories. Each file's own directory is added too.
    :param defines: ``{NAME: value}`` (value None: defined with no value).
    :param extra_text: ``{name: text}`` in-memory sources, compiled after
        ``files``, in order.
    :param include_text: ``{name: text}`` in-memory include files, found by
        ```include "name"`` from any source.
    :param check: raise :class:`CompileError` if there are errors.
    :param timescale: the time scale (``"1ns/1ps"``) of design elements
        that declare none, so files with and without ```timescale`` mix.
    :returns: a :class:`CompileResult`, which unpacks as
        ``(compilation, diagnostics)``.
    """
    files = [os.path.abspath(f) for f in files]
    dirs: List[str] = []
    for d in list(incdirs) + [os.path.dirname(f) for f in files]:
        if d and d not in dirs:
            dirs.append(d)

    sm = pyslang.SourceManager()
    for d in dirs:
        if os.path.isdir(d):
            sm.addUserDirectories(d)
    pp = pyslang.parsing.PreprocessorOptions()
    pp.predefines = [k if v is None else f"{k}={v}" for k, v in (defines or {}).items()]
    inc_paths = list(dirs)
    if include_text:
        inc_paths.append(_VIRTUAL_INC_DIR)
        for name, text in include_text.items():
            sm.assignText(f"{_VIRTUAL_INC_DIR}/{name}", text)
    pp.additionalIncludePaths = inc_paths
    bag = pyslang.Bag()
    bag.preprocessorOptions = pp
    if timescale is not None:
        co = ast.CompilationOptions()
        co.defaultTimeScale = pyslang.TimeScale.fromString(timescale)
        bag.compilationOptions = co

    comp = ast.Compilation(bag)
    trees = []
    for path in files:
        trees.append(syntax.SyntaxTree.fromFile(path, sm, bag))
    for name, text in (extra_text or {}).items():
        trees.append(syntax.SyntaxTree.fromText(text, sm, name, "", bag))
    for t in trees:
        comp.addSyntaxTree(t)

    diags = Diagnostics()
    engine = pyslang.DiagnosticEngine(sm)
    for d in comp.getAllDiagnostics():
        text = engine.reportAll(sm, [d]).strip()
        diags.append(Diagnostic(is_error=bool(d.isError()), code=str(d.code), text=text))

    _KEEPALIVE[id(comp)] = (comp, sm, trees)
    res = CompileResult(comp, diags, sm, trees)
    return res.check() if check else res


def release(compilation) -> None:
    """Drop the module's strong references for *compilation*."""
    _KEEPALIVE.pop(id(compilation), None)


# ---------------------------------------------------------------- lookup

def _as_class(sym):
    if sym is None:
        return None
    if sym.kind == ast.SymbolKind.ClassType:
        return sym
    if sym.kind == ast.SymbolKind.GenericClassDef:
        # The default specialization (only for classes whose parameters all
        # have defaults).
        return _default_specialization(sym)
    if sym.kind == ast.SymbolKind.TypeAlias:
        t = sym.targetType.type.canonicalType
        return t if t.kind == ast.SymbolKind.ClassType else None
    return None


def _default_specialization(gen):
    """The default specialization of a GenericClassDef (pyslang binds it as a
    property getter that takes the scope)."""
    try:
        return type(gen).defaultSpecialization.fget(gen, gen.parentScope)
    except Exception:
        return None


def find_class(compilation, name: str):
    """Find a class by ``"pkg::name"`` (or ``"name"`` in ``$unit`` / any
    package). Returns its ClassType symbol, or None."""
    if "::" in name:
        pkg_name, cls_name = name.split("::", 1)
        pkg = compilation.getPackage(pkg_name)
        if pkg is None:
            return None
        return _as_class(pkg.find(cls_name))
    for unit in compilation.getCompilationUnits():
        c = _as_class(unit.find(name))
        if c is not None:
            return c
    for pkg in compilation.getPackages():
        c = _as_class(pkg.find(name))
        if c is not None:
            return c
    return None


def class_from_typedef(compilation, module_name: str, typedef_name: str):
    """The class a typedef names, in module *module_name*, e.g. compile an
    extra snippet ``module __top; typedef fifo_chain#(bit[7:0]) T; endmodule``
    and pass ``("__top", "T")`` to get that specialization.

    The module is elaborated as a top-level instance (or found in the
    definitions if it is not a top). Returns the ClassType, or raises
    LookupError.
    """
    root = compilation.getRoot()
    scope = None
    for inst in root.topInstances:
        if inst.name == module_name:
            scope = inst.body
            break
    if scope is None:
        raise LookupError(f"no top-level module {module_name!r}")
    sym = scope.find(typedef_name)
    if sym is None:
        raise LookupError(f"no typedef {typedef_name!r} in {module_name!r}")
    cls = _as_class(sym)
    if cls is None:
        raise LookupError(f"{module_name}.{typedef_name} is not a class type")
    return cls


# ------------------------------------------------------------ stub bodies

def _tok(t) -> str:
    return t.valueText if t is not None else ""


def _text(node) -> str:
    """Source text of a syntax node without leading trivia (comments)."""
    if node is None:
        return ""
    s = str(node)
    first = node.getFirstToken()
    if first is not None:
        lead = "".join(str(x) for x in first.trivia)
        if s.startswith(lead):
            s = s[len(lead):]
    return s.strip()


def _class_local_names(cls_syn) -> set:
    names = set()
    params = cls_syn.parameters
    if params is not None:
        for decl in params.declarations:
            for d in getattr(decl, "declarators", []) or []:
                names.add(_tok(d.name))
    for it in cls_syn.items:
        if it.kind == syntax.SyntaxKind.TypedefDeclaration:
            names.add(_tok(it.name))
        elif it.kind == syntax.SyntaxKind.ClassPropertyDeclaration:
            decl = it.declaration
            if decl is not None and decl.kind == syntax.SyntaxKind.TypedefDeclaration:
                names.add(_tok(decl.name))
    return names


def _stubs_for_class(cls_syn, defined: set) -> List[str]:
    cname = _tok(cls_syn.name)
    if _tok(cls_syn.virtualOrInterface) == "interface":
        return []
    local = _class_local_names(cls_syn)
    out = []
    for it in cls_syn.items:
        if it.kind != syntax.SyntaxKind.ClassMethodPrototype:
            continue
        quals = [_tok(q) for q in it.qualifiers]
        if "extern" not in quals:
            continue
        p = it.prototype
        mname = _text(p.name)
        if (cname, mname) in defined:
            continue
        ports = _text(p.portList) or "()"
        kw = _tok(p.keyword)
        if kw == "task":
            out.append(f"task {cname}::{mname}{ports}; endtask")
            continue
        ret = _text(p.returnType)
        if mname == "new":
            out.append(f"function {cname}::new{ports}; endfunction")
        elif ret in ("", "void"):
            out.append(f"function void {cname}::{mname}{ports}; endfunction")
        else:
            head = ret.split("#")[0].split("::")[0].split("[")[0].strip()
            if head in local:
                ret = f"{cname}::{ret}"
            out.append(f"function {ret} {cname}::{mname}{ports}; "
                       f"{ret} __r; return __r; endfunction")
    return out


def _out_of_block_defs(members) -> set:
    defined = set()
    for m in members:
        if m.kind in (syntax.SyntaxKind.FunctionDeclaration, syntax.SyntaxKind.TaskDeclaration):
            name = m.prototype.name
            if name.kind == syntax.SyntaxKind.ScopedName:
                defined.add((_text(name.left), _text(name.right)))
    return defined


def extern_stub_bodies(trees: Iterable, packages: Optional[Iterable[str]] = None
                       ) -> Dict[str, str]:
    """Default out-of-block bodies for ``extern`` method prototypes that have
    none: a void function or task is empty, any other function returns a
    default-initialized value.

    Works on syntax (it does not need a clean elaboration, which
    declarations-only code does not have). Out-of-block bodies must be in the
    scope of their class, so the result maps a package name (``"$unit"`` for
    classes at compilation-unit level) to the text of its bodies; a host
    includes each text inside that package, e.g. through an include hook and
    :func:`compile`'s ``include_text``.

    :param trees: syntax trees (``CompileResult.trees``).
    :param packages: only these packages (default: all).
    """
    want = set(packages) if packages is not None else None
    out: Dict[str, List[str]] = {}
    for t in trees:
        root = t.root
        # A tree whose source holds a single declaration has it as its root.
        unit_members = (list(root.members) if root.kind == syntax.SyntaxKind.CompilationUnit
                        else [root])
        scopes = [("$unit", unit_members)]
        for m in unit_members:
            if m.kind == syntax.SyntaxKind.PackageDeclaration:
                scopes.append((_tok(m.header.name), list(m.members)))
        for scope_name, members in scopes:
            if want is not None and scope_name not in want:
                continue
            defined = _out_of_block_defs(members)
            for m in members:
                if m.kind == syntax.SyntaxKind.ClassDeclaration:
                    out.setdefault(scope_name, []).extend(_stubs_for_class(m, defined))
    return {k: "\n".join(v) + "\n" for k, v in out.items() if v}
