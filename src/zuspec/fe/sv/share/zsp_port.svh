
typedef class zsp_component;
typedef class zsp_export;

class zsp_port #(type T) extends zsp_component;
    T           m_if;

    function new(string name, zsp_component parent);
        super.new(name, parent);
    endfunction

    function void connect(zsp_export #(T) exp);
        m_if = exp.m_if;
    endfunction
endclass

