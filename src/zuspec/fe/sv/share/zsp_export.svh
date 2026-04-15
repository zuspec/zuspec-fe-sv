
typedef class zsp_component;

class zsp_export #(type T) extends zsp_component;
    T           m_if;

    function new(string name, zsp_component parent);
        super.new(name, parent);
    endfunction

    function void connect(zsp_export #(T) exp);
        // Passes an implementation up
        m_if = exp.m_if;
    endfunction

endclass

