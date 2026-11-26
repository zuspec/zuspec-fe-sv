# AXI4 core: round-trip fixture

A copy of `fw-proto-axi-new/src/core` at commit `2e2cf3f`, used by
`test_sv_round_trip_axi4_core.py`: struct-valued class parameters, typedefs
in classes written over them (`logic [w1(P.ID_W)-1:0] id`), member-type
references (`axi4_types #(P)::aw_t`, `t::aw_t`), package functions and
localparams, and class and struct-member attributes.

`fw_proto_pkg.sv` is a stub holding only `fw_rv_ch #(T)`, the one class the
core uses from `fw_proto_pkg`. Refresh the other files by copying them again.
