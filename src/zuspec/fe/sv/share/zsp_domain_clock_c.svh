class zsp_domain_clock_c;
    virtual zsp_clock_if m_clk;

    function new(virtual zsp_clock_if clk);
        m_clk = clk;
    endfunction

    static function zsp_domain_clock_c mk(virtual zsp_clock_if clk);
        mk = new(clk);
    endfunction

endclass
