
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

    // Handle 
    local function do_build(zsp_component comp);
        comp.build(null);
        foreach (comp.m_children[i]) begin
            do_build(comp.m_children[i]);
        end
    endfunction

    local function do_connect(zsp_component comp);
        if (clock_d != null) begin
            clock_d = m_parent.clock_d;
        end
        // Connect bottom-up
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