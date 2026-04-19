
`ifndef INCLUDED_ZSP_ACTION_C_SVH
`define INCLUDED_ZSP_ACTION_C_SVH

// Base class for Zuspec action classes.
// Parameterized by the component type T that owns this action.
// At simulation time the class has no behavioral effect beyond
// holding fields and constraints as defined in the subclass.
// The fe-sv front-end uses this base class to identify action classes.
class zsp_action_c #(type T = zsp_component);
    T m_comp;

    function new(T comp = null);
        m_comp = comp;
    endfunction
endclass

`endif /* INCLUDED_ZSP_ACTION_C_SVH */
