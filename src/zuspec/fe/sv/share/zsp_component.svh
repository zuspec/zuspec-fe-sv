
typedef class zsp_object;

class zsp_component extends zsp_object;
    zsp_component           m_parent;
    zsp_component           m_children[$];
    zsp_domain_clock_c      clock_d;

    function new(string name, zsp_component parent);
        m_parent = parent;
        if (parent != null) begin
            parent.m_children.push_back(this);
        end
    endfunction

    virtual function void build();
    endfunction

    virtual function void connect();
    endfunction

    virtual task run();
    endtask

endclass

