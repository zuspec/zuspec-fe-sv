"""Host hooks of the interpreter."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["NOT_HANDLED", "Intrinsics"]


class _NotHandled:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NOT_HANDLED"


#: Returned by :meth:`Intrinsics.call` to let the SV body run.
NOT_HANDLED: Any = _NotHandled()


class Intrinsics:
    """Base class for host hooks. Every method has a do-nothing default."""

    def call(self, interp, sub, this, args: List[Any], named: Dict[str, Any]) -> Any:
        """Called for every subroutine call (methods, static and package
        functions, tasks, constructors) before its body runs, after virtual
        dispatch.

        :param sub: the SubroutineSymbol about to run.
        :param this: the receiver Obj (None for a static call).
        :param args: the actual argument values in declaration order, with
            defaults filled in (a ``ref``/``output`` argument's current value).
        :param named: the same values keyed by formal name.
        :returns: :data:`NOT_HANDLED` to run the SV body; anything else is the
            call's result (output/ref arguments are then left unchanged).
        """
        return NOT_HANDLED

    def new_object(self, interp, cls, caller_sub) -> Optional[Any]:
        """Called when SV code executes ``new`` for class *cls* (a ClassType),
        from subroutine *caller_sub* (None outside a subroutine). Return None
        for the default behavior (allocate, then run the constructor chain);
        return an Obj to use it as the result instead (no constructor runs);
        raise to reject the ``new``."""
        return None

    def symbolic_type(self, interp, t) -> Optional[Any]:
        """Tracing (``trace.py``): the leaf expression to trace a type by, or
        None. Asked for the argument type of ``$bits(T)`` (as written, not
        canonical: a type parameter is a ``TypeAliasType``); a leaf makes the
        result ``Sym(width, $bits(leaf))``."""
        return None

    def symbolic_param(self, interp, sym) -> Optional[Any]:
        """Tracing: how to follow class parameter *sym* (a ``ParameterSymbol``
        or ``TypeParameterSymbol``), or None to read it as its constant value.

        For a value parameter, return its value as a traced value: a
        ``Sym(value, leaf)`` (``leaf`` an Expr the host recognizes; for a
        struct-valued parameter, member reads build ``ExprAttribute`` over
        it), or ``Sym(value, None)`` for a value the host knows depends on
        traced values in a way it cannot express. For a type parameter,
        return the leaf expression to trace it by (types written over it in
        class arguments become ``DataTypeRef(leaf.name)``), or
        :data:`~zuspec.fe.sv.interp.trace.TAINTED`."""
        return None

    def ctl_end(self, interp, frame) -> None:
        """Tracing: a control frame (:class:`~zuspec.fe.sv.interp.trace.CtlFrame`)
        that depended on a traced value has ended. Called for every such frame,
        innermost first, while ``interp.ctl`` still holds its enclosing frames."""

    def ctl_force(self, interp, frame) -> Optional[bool]:
        """Tracing: the arm a traced ``if`` (*frame*, its ``cond`` and
        ``key`` set, not yet run) is to take, whatever its condition's
        value: True, False, or None to follow the value. A host that wants
        to see an arm that did not run runs the code again and forces it
        here (by the condition, say, so every ``if`` on it agrees); the
        frame then has ``forced`` set."""
        return None

    def message(self, interp, kind: str, text: str) -> None:
        """A ``$display``-family message. *kind* is one of ``display``,
        ``write``, ``info``, ``warning``, ``error``, ``fatal``. A ``$fatal``
        raises :class:`~zuspec.fe.sv.interp.InterpFatal` after this returns."""
        interp.messages.append((kind, text))
