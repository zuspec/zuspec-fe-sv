
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

// Macro for action classes.
// Usage: `zsp_action_utils(MyAction_c, MyComponent_c)
`define zsp_action_utils(T, COMP_T) \
  function new(COMP_T comp = null); \
    super.new(comp); \
  endfunction \
  static function T create(COMP_T comp = null); \
    T ret = new(comp); \
    return ret; \
  endfunction


`endif /* INCLUDED_ZSP_MACROS_SVH */
