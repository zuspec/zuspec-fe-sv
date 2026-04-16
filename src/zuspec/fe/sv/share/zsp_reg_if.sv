
interface zsp_reg_if #(
    parameter type T
    ) (
        input           clock,
        output T        val
    );

    task write(T _val);
        @(posedge clock);
        val <= _val;
    endtask

endinterface