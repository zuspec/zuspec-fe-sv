
typedef class zsp_build_ctxt;
typedef class zsp_component_ctxt;
typedef class zsp_domain_clock;
typedef class zsp_object;

class zsp_component extends zsp_object;
    zsp_component           m_parent;
    zsp_component           m_children[$];
    zsp_domain_clock        clock_d;
    zsp_domain_clock        m_clock_domains[$];

    function new(string name, zsp_component parent);
        m_parent = parent;
        if (parent != null) begin
            parent.m_children.push_back(this);
        end
    endfunction

    virtual function void build(zsp_component_ctxt ctxt);
        // Get init from map

        // Pass an initialization object (consts), not parent
//        ::create("abc", cfg);
    endfunction

    virtual function void connect(zsp_component_ctxt ctxt);
    endfunction

    virtual task run();
    endtask

endclass

