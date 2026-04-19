
`ifndef INCLUDED_ZSP_MARK_SVH
`define INCLUDED_ZSP_MARK_SVH

// Zuspec marker class — zero simulation behavior.
// Every method returns 1 (trivially satisfied as a constraint expression).
// The fe-sv front-end reads calls to these as IR metadata.
//
// Usage inside a constraint block:
//   constraint c_add {
//     if (opcode == 7'h33 && funct3 == 3'h0 && funct7b5 == 1'b0) {
//       alu_op == ALU_ADD;
//       zsp_mark #(logic[3:0])::valid(alu_op);
//     }
//   }
class zsp_mark #(type T = logic);
    // Marks field `f` as observable/meaningful under the enclosing guard.
    static function bit valid(T f);     return 1; endfunction

    // Marks field `f` as irrelevant under the enclosing guard (ODC hint).
    static function bit dont_care(T f); return 1; endfunction
endclass

`endif /* INCLUDED_ZSP_MARK_SVH */
