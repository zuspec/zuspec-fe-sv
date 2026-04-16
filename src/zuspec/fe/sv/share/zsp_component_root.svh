
class zsp_component_root #(type T) extends T;

    function new();
        super.new("", null);
    endfunction

    virtual task run();
        // Sequence build
        do_build(this);
        // Sequence connect
        do_connect(this);

        // Run super
        fork
            super.run();
        join_none
        foreach (m_children[i]) begin
            do_run(m_children[i]);
        end
    endtask

    local function void do_build(zsp_component comp);
        comp.build();
        foreach (comp.m_children[i]) begin
            do_build(comp.m_children[i]);
        end
    endfunction

    local function void do_connect(zsp_component comp);
        foreach (comp.m_children[i]) begin
            do_connect(comp.m_children[i]);
        end
        comp.connect();
    endfunction

    local task do_run(zsp_component comp);
        fork
            comp.run();
        join_none
        foreach (comp.m_children[i]) begin
            do_run(comp.m_children[i]);
        end
    endtask

endclass