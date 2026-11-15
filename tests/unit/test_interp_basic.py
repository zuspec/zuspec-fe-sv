"""The SV class-subset interpreter: language features, self-contained."""
import pytest

from zuspec.fe.sv import CompileError, class_from_typedef, compile, find_class
from zuspec.fe.sv.interp import Cell, InterpError, InterpFatal, Obj, Struct


def test_arith_and_widths(sv):
    s = sv("""
    package p;
      function int f(int a, int b); return a * b - 7; endfunction
      function bit [7:0] trunc(int a); return a; endfunction
      function int neg(); int x = -5; return x / 2; endfunction
      function int sh(); int x = -16; return x >>> 2; endfunction
      function int ush(); bit [7:0] x = 8'hF0; return x >> 4; endfunction
      function int ternary(int a); return (a > 3) ? 2 * a : a - 1; endfunction
      function int ops(); int a = 6; a += 4; a <<= 1; a--; return a; endfunction
      function bit [15:0] cat(); bit [7:0] a = 8'hAB; return {a, 8'h01}; endfunction
      function bit [7:0] bits(); bit [7:0] v = 0; v[3:0] = 4'hF; v[7] = 1; return v; endfunction
    endpackage""")
    assert s.run("f", 3, 4) == 5
    assert s.run("trunc", 0x1FF) == 0xFF
    assert s.run("neg") == -2
    assert s.run("sh") == -4
    assert s.run("ush") == 0xF
    assert s.run("ternary", 5) == 10
    assert s.run("ternary", 2) == 1
    assert s.run("ops") == 19
    assert s.run("cat") == 0xAB01
    assert s.run("bits") == 0x8F


def test_named_and_default_args(sv):
    s = sv("""
    package p;
      class c #(int N = 1);
        static function int mk(int a = N, int b = 2 * N, int d = 3);
          return a * 100 + b * 10 + d;
        endfunction
      endclass
      function int t1(); return c#(4)::mk(.d(7)); endfunction
      function int t2(); return c#(1)::mk(5); endfunction
      function int t3(); return c#()::mk(.b(0), .a(9)); endfunction
    endpackage""")
    assert s.run("t1") == 4 * 100 + 8 * 10 + 7
    assert s.run("t2") == 5 * 100 + 2 * 10 + 3
    assert s.run("t3") == 9 * 100 + 0 + 3
    # from Python: positional, named, defaults
    sub = s.fn("t1")  # just to check package lookup
    assert sub.name == "t1"
    mk = find_class(s.comp, "p::c").find("mk")
    assert s.it.call(mk, None, (1,), {"d": 0}) == 100 + 20


def test_struct_value_semantics_and_ref(sv):
    s = sv("""
    package p;
      class k;
        typedef struct { int depth; int n; } params_t;
        static function params_t mk_params(int depth = 2, int n = 1);
          params_t p; p.depth = depth; p.n = n; return p;
        endfunction
        static function void bump(ref params_t p); p.depth = p.depth * 10; endfunction
        static function void bump_int(ref int x); x++; endfunction
        static function void outp(output int x, inout int y); x = 5; y = y + 1; endfunction
      endclass
      function int t();
        k::params_t a, b;
        int i = 3, j, m = 1;
        a = k::mk_params(.depth(4));
        b = a;              // copy
        b.depth = 99;
        k::bump(a);         // alias
        k::bump_int(i);
        k::outp(j, m);
        return a.depth * 1000 + i * 100 + j * 10 + m + (b.depth == 99 ? 0 : 1000000);
      endfunction
    endpackage""")
    assert s.run("t") == 40 * 1000 + 4 * 100 + 5 * 10 + 2
    # struct values seen from Python
    mk = find_class(s.comp, "p::k").find("mk_params")
    v = s.it.call(mk, None, (), {"n": 7})
    assert isinstance(v, Struct)
    assert v.fields == {"depth": 2, "n": 7} and v.depth == 2 and v["n"] == 7
    # ref from Python through a Cell
    c = Cell(41)
    s.it.call(find_class(s.comp, "p::k").find("bump_int"), None, (c,))
    assert c.get() == 42


def test_dynamic_arrays_foreach_for_patterns(sv):
    s = sv("""
    package p;
      class item; int v; function new(int v); this.v = v; endfunction endclass
      typedef item pair_t[2];
      function int sum_pairs(pair_t pairs[]);
        int s = 0;
        foreach (pairs[i]) s += pairs[i][0].v * pairs[i][1].v;
        return s;
      endfunction
      function int t();
        int a[];
        int q[$];
        int s = 0;
        item x = new(2), y = new(3), z = new(4);
        a = new[4];
        foreach (a[i]) a[i] = i * i;
        for (int i = 0; i + 1 < a.size(); i++) s += a[i];
        q.push_back(10); q.push_front(20);
        s += q[0] + q.size();
        a = new[6](a);
        return s * 1000 + a.size() * 100 + sum_pairs('{ '{x, y}, '{y, z} });
      endfunction
      function int fixed();
        int f[4] = '{1, 2, 3, 4};
        int g[2][3];
        int s = 0;
        foreach (g[i, j]) g[i][j] = i * 10 + j;
        foreach (f[i]) s += f[i];
        return s * 100 + g[1][2];
      endfunction
    endpackage""")
    assert s.run("t") == (0 + 1 + 4 + 20 + 2) * 1000 + 600 + (2 * 3 + 3 * 4)
    assert s.run("fixed") == 10 * 100 + 12


def test_strings_sformatf_enums(sv):
    s = sv("""
    package p;
      typedef enum { RED, GREEN = 5, BLUE } color_t;
      function string t(int i);
        string a = "u";
        color_t c = GREEN;
        string r = $sformatf("%s[%0d]:%s:%h:%0b", a, i, c.name(), 8'hA, 3'd5);
        if (r == "x" || a != "u") return "bad";
        return {r, "_", a.toupper(), $sformatf("%0d", r.len())};
      endfunction
      function int e(); color_t c = RED; c = c.next(); return int'(c) + (BLUE > GREEN); endfunction
      function int bits_of(); return $bits(bit [12:0]) + $bits(color_t); endfunction
      function int clog(); return $clog2(17); endfunction
      function int cmp(); string a = "abc", b = "abd"; return (a < b) + 2 * a.compare(b); endfunction
    endpackage""")
    r = s.run("t", 3)
    assert r == "u[3]:GREEN:0a:101_U17"
    assert s.run("e") == 5 + 1
    assert s.run("bits_of") == 13 + 32
    assert s.run("clog") == 5
    assert s.run("cmp") == 1 - 2


def test_classes_virtual_super_mixin_interface(sv):
    s = sv("""
    package p;
      interface class shape_if;
        pure virtual function int area();
      endclass
      virtual class base;
        int log[$];
        int id = 7;
        function new(); log.push_back(1); endfunction
        virtual function void build(); log.push_back(10); endfunction
        pure virtual function string kind();
        function string describe(); return {kind(), "!"}; endfunction
      endclass
      class sq extends base implements shape_if;
        int side;
        function new(int side = 2); super.new(); this.side = side; log.push_back(2); endfunction
        virtual function void build(); super.build(); log.push_back(20); endfunction
        virtual function string kind(); return "sq"; endfunction
        virtual function int area(); return side * side; endfunction
      endclass
      class role #(type B = base) extends B;    // a mixin
        virtual function string kind(); return {"role:", super.kind()}; endfunction
      endclass
      class chain;
        string parts[$];
        function chain add(string s); parts.push_back(s); return this; endfunction
      endclass
      function string t();
        sq a = new(3);
        shape_if s = a;
        base b = a;
        role #(sq) r = new();
        chain c = new();
        b.build();
        void'(c.add("x").add("y").add("z"));
        if (r == null || c.parts.size() != 3) return "bad";
        return $sformatf("%0d|%0d|%s|%s|%0d|%0d|%p", s.area(), a.id, b.describe(), r.kind(),
                         r.area(), b.log.size(), b.log);
      endfunction
    endpackage""")
    assert s.run("t") == "9|7|sq!|role:sq|4|4|'{1, 2, 10, 20}"
    # The object as seen from Python
    sq = find_class(s.comp, "p::sq")
    o = s.it.construct(sq, (5,))
    assert isinstance(o, Obj) and o.cls is not None
    assert o.get("side") == 5 and o.get("id") == 7 and o.get("log") == [1, 2]
    s.it.call_method(o, "build")
    assert o.get("log") == [1, 2, 10, 20]
    assert s.it.call_method(o, "describe") == "sq!"
    # allocate + run_ctor separately
    o2 = s.it.allocate(sq)
    assert o2.get("id") == 0 and o2.get("log") == []
    s.it.run_ctor(o2)
    assert o2.get("side") == 2 and o2.get("id") == 7


def test_statics_per_specialization(sv):
    s = sv("""
    package p;
      class cnt #(type T = int);
        static int n;
        static cnt #(T) inst;
        static function cnt #(T) get();
          if (inst == null) inst = new();
          n++;
          return inst;
        endfunction
      endclass
      function int t();
        cnt #(int) a = cnt #(int)::get();
        cnt #(int) b = cnt #(int)::get();
        cnt #(byte) c = cnt #(byte)::get();
        return (a == b) * 100 + cnt #(int)::n * 10 + cnt #(byte)::n;
      endfunction
    endpackage
    module __top; typedef p::cnt #(int) T; typedef p::cnt #(byte) U; endmodule
    """)
    assert s.run("t") == 100 + 20 + 1
    ti = class_from_typedef(s.comp, "__top", "T")
    tb = class_from_typedef(s.comp, "__top", "U")
    assert s.it.static_field(ti, "n") == 2
    assert s.it.static_field(tb, "n") == 1
    s.it.set_static_field(tb, "n", 9)
    assert s.it.static_field(tb, "n") == 9
    assert isinstance(s.it.static_field(ti, "inst"), Obj)


def test_class_scoped_typedef_handles(sv):
    s = sv("""
    package p;
      interface class maker #(type B = int);
        pure virtual function B make(int x);
      endclass
      class maker_of #(type B = int, type D = B) implements maker #(B);
        typedef maker #(B) maker_t;
        static maker_t me;
        static function maker_t get();
          maker_of #(B, D) m;
          if (me == null) begin m = new(); me = m; end
          return me;
        endfunction
        virtual function B make(int x); D d = new(x); return d; endfunction
      endclass
      class thing; int v; function new(int v); this.v = v; endfunction endclass
      class big extends thing; function new(int v); super.new(v * 2); endfunction endclass
      class holder;
        typedef struct { int n; maker #(thing) h; } params_t;
        static function params_t mk(int n = 1, maker #(thing) h = null);
          params_t p; p.n = n; p.h = h; return p;
        endfunction
        static function void init(ref params_t p);
          if (p.h == null) p.h = maker_of #(thing, big)::get();
        endfunction
      endclass
      function int t();
        holder::params_t p = holder::mk(.n(3));
        holder::init(p);
        return p.h.make(p.n).v;
      endfunction
    endpackage""")
    assert s.run("t") == 6


def test_errors(sv):
    s = sv("""
    package p;
      class c; int v; endclass
      function int nul(); c x; return x.v; endfunction
      function void fat(); $fatal(1, "boom %0d", 3); endfunction
      function void msg(); $display("hi %s", "there"); $warning("w"); endfunction
    endpackage""")
    with pytest.raises(InterpError, match="null object"):
        s.run("nul")
    with pytest.raises(InterpFatal, match="boom 3"):
        s.run("fat")
    s.run("msg")
    assert s.it.messages[-2:] == [("display", "hi there"), ("warning", "w")]


def test_compile_errors():
    res = compile(extra_text={"bad.sv": "package q; function int f(); return x; endfunction endpackage"})
    comp, diags = res
    assert diags.has_errors and diags.errors
    with pytest.raises(CompileError, match="x"):
        res.check()
