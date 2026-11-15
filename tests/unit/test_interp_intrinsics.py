"""Host hooks: Intrinsics.call, Intrinsics.new_object, Intrinsics.message."""
import pytest

from zuspec.fe.sv import compile, extern_stub_bodies, find_class
from zuspec.fe.sv.interp import NOT_HANDLED, InterpError, Interpreter, Intrinsics, Obj

SRC = """
package p;
  class sig #(type T = bit);
    typedef sig #(T) this_t;
    extern static function this_t create(string name, int width = $bits(T));
    extern function T peek(int offset = 3);
  endclass
  // stub bodies (the host supplies behavior through intrinsics)
  function sig::this_t sig::create(string name, int width = $bits(T));
    sig::this_t r; return r;
  endfunction
  function sig::T sig::peek(int offset = 3); return 0; endfunction

  class comp;
    sig #(bit [7:0]) a;
    sig #(bit [3:0]) b;
    int seen;
    virtual function void build();
      a = sig #(bit [7:0])::create("a");
      b = sig #(bit [3:0])::create("b", 9);
      seen = a.peek() + b.peek(.offset(1));
    endfunction
  endclass
  class leaf; int v = 5; endclass
  class maker;
    leaf l;
    function new(); l = new(); endfunction
  endclass
endpackage
"""


class Recorder(Intrinsics):
    """Implements sig::create and sig::peek in Python; logs everything."""

    def __init__(self):
        self.calls = []
        self.news = []

    def call(self, interp, sub, this, args, named):
        owner = interp.owner_class(sub)
        self.calls.append((str(owner) if owner is not None else None, sub.name, list(args)))
        if owner is not None and owner.genericClass is not None \
                and owner.genericClass.name == "sig":
            if sub.name == "create":
                o = interp.allocate(owner)
                interp.run_ctor(o)
                o.host = {"name": args[0], "width": args[1]}
                return o
            if sub.name == "peek":
                return this.host["width"] * 10 + named["offset"]
        return NOT_HANDLED

    def new_object(self, interp, cls, caller_sub):
        self.news.append((str(cls), caller_sub.name if caller_sub is not None else None))
        return None


def test_intercept_extern_and_defaults():
    comp, _ = compile(extra_text={"t.sv": SRC}).check()
    rec = Recorder()
    it = Interpreter(comp, rec)
    c = it.construct(find_class(comp, "p::comp"))
    it.call_method(c, "build")
    a, b = c.get("a"), c.get("b")
    assert isinstance(a, Obj) and a.host == {"name": "a", "width": 8}
    assert str(a.cls) == "sig#(bit[7:0])"
    assert b.host == {"name": "b", "width": 9}
    # peek: the default (3) and a named actual (1) both reach the host; the
    # host's result is converted to the return type T (bit [3:0]: 91 -> 11)
    assert c.get("seen") == (8 * 10 + 3) + ((9 * 10 + 1) & 0xF)
    names = [(o, n, a) for o, n, a in rec.calls if n in ("create", "peek")]
    assert names == [
        ("sig#(bit[7:0])", "create", ["a", 8]),
        ("sig#(bit[3:0])", "create", ["b", 9]),
        ("sig#(bit[7:0])", "peek", [3]),
        ("sig#(bit[3:0])", "peek", [1]),
    ]
    # build itself went through the hook first (after dispatch)
    assert ("comp", "build", []) in rec.calls


def test_new_hook_observe_replace_reject():
    comp, _ = compile(extra_text={"t.sv": SRC}).check()
    rec = Recorder()
    it = Interpreter(comp, rec)
    m = it.construct(find_class(comp, "p::maker"))
    assert rec.news == [("leaf", "new")]
    assert m.get("l").get("v") == 5

    class Replace(Intrinsics):
        def new_object(self, interp, cls, caller_sub):
            o = interp.allocate(cls)
            o.set("v", 42)
            return o

    it2 = Interpreter(comp, Replace())
    assert it2.construct(find_class(comp, "p::maker")).get("l").get("v") == 42

    class Reject(Intrinsics):
        def new_object(self, interp, cls, caller_sub):
            raise InterpError(f"new of {cls} is not allowed")

    with pytest.raises(InterpError, match="not allowed"):
        Interpreter(comp, Reject()).construct(find_class(comp, "p::maker"))


def test_call_body_bypasses_hook():
    comp, _ = compile(extra_text={"t.sv": SRC}).check()

    class Wrap(Recorder):
        def __init__(self):
            super().__init__()
            self.order = []

        def call(self, interp, sub, this, args, named):
            if sub.name == "build":
                self.order.append("pre")
                interp.call_body(sub, this, args)
                self.order.append(f"post:{this.get('seen')}")
                return None
            return super().call(interp, sub, this, args, named)

    w = Wrap()
    it = Interpreter(comp, w)
    c = it.construct(find_class(comp, "p::comp"))
    it.call_method(c, "build")
    assert w.order == ["pre", "post:94"]
    assert [n for _, n, _ in w.calls].count("build") == 0   # Recorder.call never saw it

    # Without intrinsics the stub bodies run: create returns null.
    it2 = Interpreter(comp)
    c2 = it2.construct(find_class(comp, "p::comp"))
    with pytest.raises(InterpError, match="null object access calling peek"):
        it2.call_method(c2, "build")
    assert c2.get("a") is None


def test_extern_stub_bodies_generated():
    decl = """
    package q;
      class k #(type T = int);
        typedef k #(T) this_t;
        extern static function this_t create(string name);
        extern function void put(T v);
        extern task run();
      endclass
      function void k::put(T v); endfunction   // already has a body
    endpackage
    """
    res = compile(extra_text={"d.sv": decl})
    stubs = extern_stub_bodies(res.trees)
    assert set(stubs) == {"q"}
    text = stubs["q"]
    assert "function k::this_t k::create(string name);" in text
    assert "task k::run();" in text
    assert "k::put" not in text
    # Spliced into the package, the stubs make it elaborate.
    pkg = decl.replace("endpackage", "`include \"q_stubs.svh\"\nendpackage")
    res2 = compile(extra_text={"d.sv": pkg}, include_text={"q_stubs.svh": text})
    assert not res2.diagnostics.has_errors, res2.diagnostics.report()

    class Host(Intrinsics):
        def call(self, interp, sub, this, args, named):
            if sub.name == "create":
                return interp.construct(interp.owner_class(sub))
            return NOT_HANDLED

    comp = res2.compilation
    it = Interpreter(comp, Host())
    k = find_class(comp, "q::k")
    # find_method records k as the owner of its static methods
    o = it.call(it.find_method(k, "create"), None, ("x",))
    assert isinstance(o, Obj) and str(o.cls) == "k#(int)"
