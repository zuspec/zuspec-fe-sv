
class zsp_reg_c #(type T);
    virtual interface zsp_reg_if #(T) vif;
    T val;

    function new(virtual interface zsp_reg_if #(T) vif);
        this.vif = vif;
    endfunction

    static function zsp_reg_c #(T) mk(virtual interface zsp_reg_if #(T) vif);
        mk = new(vif);
    endfunction

    task write(T val);
        this.val = val;
        vif.write(val);
    endtask

    function T read();
        return val;
    endfunction

endclass