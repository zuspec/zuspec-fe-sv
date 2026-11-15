"""A generic interpreter for the class subset of SystemVerilog.

Runs SV class code (constructors, methods, static and package functions)
over a pyslang compilation, sequentially, the way a simulator runs it at time
zero. It knows nothing about any particular library: a host steers it with
:class:`Intrinsics`, which sees every subroutine call before its body runs
(and may supply the result instead) and every ``new``.

Ported from fw-hdl ``python/fw/hdl/fe/elab/interp.py`` and ``values.py``.

Usage::

    from zuspec.fe.sv import compile, class_from_typedef
    from zuspec.fe.sv.interp import Interpreter, Intrinsics, NOT_HANDLED

    comp, diags = compile(files, extra_text={"top.sv":
        "module __top; typedef my_pkg::fifo#(bit[7:0]) T; endmodule"}).check()
    cls = class_from_typedef(comp, "__top", "T")
    it = Interpreter(comp, MyIntrinsics())
    obj = it.construct(cls)
    it.call_method(obj, "build")

Value representation
--------------------

======================== ==================================================
SV                        Python
======================== ==================================================
integral (bit, logic,    ``int``, normalized to the type: truncated to its
int, packed struct,      width; negative only for signed types (``int``
enum, ...)               ``-1`` is ``-1``; ``bit [7:0]`` 255 is ``255``).
                         Two-state: X/Z read as 0. Packed structs/arrays
                         are one ``int``. Enums are their ``int`` value.
real / shortreal         ``float``
string                   ``str``
null                     ``None``
class handle             :class:`Obj` (``obj.cls`` is the ClassType it was
                         created as; fields by name: ``obj.get(name)`` /
                         ``obj.set(name, v)``; ``obj.host`` is free for the
                         host)
dynamic array, queue,    ``list`` (fixed arrays index through their declared
fixed unpacked array     range)
associative array        ``dict``
unpacked struct          :class:`Struct` (``s["f"]`` / ``s.f``; ``s.fields``
                         is a name->value dict); copied on assignment
======================== ==================================================

Assignment has value semantics: arrays, queues, associative arrays and structs
are copied on assignment, on ``input`` argument passing and when stored;
class handles are not. ``ref`` arguments alias the caller's variable;
``output``/``inout`` copy back on return.

Scope
-----

Supported: classes (parameterized, specializations, mixins ``class C #(type B)
extends B``), constructors with ``super.new`` and ``extends B(args)``, property
initializers, static properties (per specialization), virtual dispatch
(including interface classes / ``implements``; ``super.f()`` and ``C::f()``
are not dispatched), static and package functions, tasks (run to completion),
named and default arguments, ``ref``/``output``/``inout``, ``this``, ``null``,
dynamic arrays (``new[n]``), queues and associative arrays with their built-in
methods, assignment patterns, struct typedefs, enums and their methods,
strings and their methods, ``if``/``case``/``for``/``foreach``/``while``/
``do``/``repeat``/``forever`` with ``break``/``continue``/``return``,
``$sformatf``/``$display``-family (routed to :meth:`Intrinsics.message`),
``$bits``, ``$clog2``, ``$cast``, ``$signed``/``$unsigned``, ``$size``...

Tracing (``trace.py``): a host may put :class:`Sym` values (a value plus a
Zuspec IR leaf expression) where it wants values followed; the interpreter
then also builds the expressions that compute from them, reports control
flow that depends on them (:class:`~zuspec.fe.sv.interp.trace.CtlFrame`, to
:meth:`Intrinsics.ctl_end`), and taints what it cannot express. A dynamic
array sized by a traced value is a :class:`SymList`.

Not supported (raise :class:`InterpError`): fork/join, timing controls,
``wait``, events, nonblocking assignment, interfaces / virtual interfaces,
randomization, array methods with ``with``, pattern-matching ``if``/``case``.
"""
from .intrinsics import NOT_HANDLED, Intrinsics
from .interp import Cell, Frame, InterpError, InterpFatal, Interpreter, Ref
from .values import Obj, Struct, Sym, SymList

__all__ = ["Interpreter", "Intrinsics", "NOT_HANDLED", "Obj", "Struct", "Sym", "SymList",
           "InterpError", "InterpFatal", "Frame", "Ref", "Cell"]
