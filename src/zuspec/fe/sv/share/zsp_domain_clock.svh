
typedef class zsp_component;
typedef class zsp_domain_clock_if;

class zsp_domain_clock extends zsp_component implements zsp_domain_clock_if;

    function new(string name, zsp_component parent);
        super.new(name, parent);

        // Hook in as a clock domain?
        parent.m_clock_domains.push_back(this);
    endfunction

endclass

