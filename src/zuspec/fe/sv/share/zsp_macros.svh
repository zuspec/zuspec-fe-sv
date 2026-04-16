
`ifndef INCLUDED_ZSP_MACROS_SVH
`define INCLUDED_ZSP_MACROS_SVH

`define zsp_component_utils(T) \
  function new(string name, zsp_component parent); \
    super.new(name, parent); \
  endfunction \
  static function T create(string name, zsp_component parent); \
    T ret = new(name, parent); \
    return ret; \
  endfunction


`endif /* INCLUDED_ZSP_MACROS_SVH */
