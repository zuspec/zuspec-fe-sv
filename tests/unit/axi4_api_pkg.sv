// An AXI4-sized API and payload package for the SV -> IR -> SV round trip
// (test_sv_round_trip.py). Taken from fw-proto-axi-new's core
// (src/core/fw_proto_axi4_defs_pkg.sv, axi4_types.svh, axi4_*_if.svh) at the
// default parameters (ADDR_W 32, DATA_W 64, ID_W 4, user widths 1), in 2-state
// types. The mapper lists classes, templates and protocols apart, so the
// emitter has to restore the declaration order.
package axi4_api_pkg;
    typedef enum bit [1:0] {
        AXI4_BURST_FIXED = 2'b00, AXI4_BURST_INCR = 2'b01,
        AXI4_BURST_WRAP  = 2'b10, AXI4_BURST_RSVD = 2'b11
    } axi4_burst_e;
    typedef enum bit [2:0] {
        AXI4_SIZE_1  = 3'd0, AXI4_SIZE_2  = 3'd1, AXI4_SIZE_4  = 3'd2, AXI4_SIZE_8   = 3'd3,
        AXI4_SIZE_16 = 3'd4, AXI4_SIZE_32 = 3'd5, AXI4_SIZE_64 = 3'd6, AXI4_SIZE_128 = 3'd7
    } axi4_size_e;
    typedef enum bit [1:0] {
        AXI4_RESP_OKAY   = 2'b00, AXI4_RESP_EXOKAY = 2'b01,
        AXI4_RESP_SLVERR = 2'b10, AXI4_RESP_DECERR = 2'b11
    } axi4_resp_e;

    typedef bit [31:0] addr_t;
    typedef bit [63:0] data_t;
    typedef bit [7:0]  strb_t;
    typedef bit [3:0]  id_t;
    typedef addr_t     axaddr_t;

    typedef struct packed {
        id_t         id;
        axaddr_t     addr;
        bit [7:0]    len;
        axi4_size_e  size;
        axi4_burst_e burst;
        bit          lock;
        bit [3:0]    cache;
        bit [2:0]    prot;
        bit [3:0]    qos;
        bit [3:0]    region;
        bit [0:0]    user;
    } aw_t;
    typedef aw_t ar_t;
    typedef struct packed {
        data_t       data;
        strb_t       strb;
        bit          last;
        bit [0:0]    user;
    } w_t;
    typedef struct packed {
        id_t         id;
        axi4_resp_e  resp;
        bit [0:0]    user;
    } b_t;
    typedef struct packed {
        id_t         id;
        data_t       data;
        axi4_resp_e  resp;
        bit          last;
        bit [0:0]    user;
    } r_t;
    typedef struct packed {
        id_t         id;
        axi4_burst_e burst;
        axi4_size_e  size;
        bit          lock;
        bit [3:0]    cache;
        bit [2:0]    prot;
        bit [3:0]    qos;
        bit [3:0]    region;
        bit [0:0]    user;
    } attr_t;

    class axi4_xact_base;
        int unsigned n_write;
        int unsigned n_read;
    endclass

    (* fw_api *)
    interface class axi4_xact_if;
        pure virtual task write(input addr_t addr, input data_t data[$], input strb_t strb[$],
                                input attr_t attr, output axi4_resp_e resp);
        pure virtual task read(input addr_t addr, input int unsigned beats, input attr_t attr,
                               output data_t data[$], output axi4_resp_e resp);
    endclass

    (* fw_api *)
    interface class axi4_burst_req_if;
        (* fw_default *) pure virtual task write_burst(input aw_t aw, input w_t w[$]);
        pure virtual task read_burst(input ar_t ar);
    endclass

    (* fw_api *)
    interface class axi4_burst_rsp_if;
        pure virtual task write_resp(input b_t b);
        pure virtual task read_resp(input r_t r);
    endclass

    class axi4_xact_mem #(int ADDR_W = 32, int DATA_W = 64) extends axi4_xact_base
            implements axi4_xact_if, axi4_burst_req_if;
        bit [ADDR_W-1:0] base;
        (* fw_opt = 2 *) bit [DATA_W-1:0] mem[$];
        axi4_burst_rsp_if rsp;
        virtual task write(input addr_t addr, input data_t data[$], input strb_t strb[$],
                           input attr_t attr, output axi4_resp_e resp);
        endtask
        virtual task read(input addr_t addr, input int unsigned beats, input attr_t attr,
                          output data_t data[$], output axi4_resp_e resp);
        endtask
        virtual task write_burst(input aw_t aw, input w_t w[$]); endtask
        virtual task read_burst(input ar_t ar); endtask
        virtual function int unsigned depth(); return 0; endfunction
    endclass

    class axi4_xact_user;
        axi4_xact_mem #(48, 128) wide;
        axi4_xact_mem #() dflt;
    endclass
endpackage
