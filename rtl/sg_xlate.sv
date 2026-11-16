// StreamGuard - translation control.
//
// Every address request from the AW and AR channels comes through
// here and gets one of two answers: forward it with this output
// address, or terminate it.
//
// SMMUEN = 0: global decision from GBPA (bypass or abort).
//
// SMMUEN = 1: full lookup for every request, with no caching yet:
//
//   1. StreamID in range of the stream table?
//   2. read the Stream Table Entry       -> abort / bypass / stage 1
//   3. read the Context Descriptor       -> T0SZ, TTB0, checks
//   4. walk the translation table        -> output address or fault
//
// That is up to six memory reads per transaction. It is the correct
// but slow baseline that the TLB and configuration cache will be
// measured against.
//
// A fault always terminates the transaction. The fault code and level
// are reported alongside the response for the event queue to record.

module sg_xlate #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic              smmuen_i,
  input  logic              gbpa_abort_i,
  input  logic [47:6]       strtab_base_i,
  input  logic [5:0]        strtab_log2size_i,

  // Request
  input  logic              req_valid,
  output logic              req_ready,
  input  logic [ADDR_W-1:0] req_addr,
  input  logic [SID_W-1:0]  req_sid,
  input  logic              req_write,
  input  logic [2:0]        req_prot,

  // Response, one per request, in order
  output logic              rsp_valid,
  output logic              rsp_fault,
  output logic [ADDR_W-1:0] rsp_addr,
  output logic [7:0]        rsp_fcode,
  output logic [1:0]        rsp_flevel,

  // Table memory read port (AXI4 read channels)
  output logic              t_axi_arid,
  output logic [ADDR_W-1:0] t_axi_araddr,
  output logic [7:0]        t_axi_arlen,
  output logic [2:0]        t_axi_arsize,
  output logic [1:0]        t_axi_arburst,
  output logic [2:0]        t_axi_arprot,
  output logic              t_axi_arvalid,
  input  logic              t_axi_arready,
  input  logic              t_axi_rid,
  input  logic [63:0]       t_axi_rdata,
  input  logic [1:0]        t_axi_rresp,
  input  logic              t_axi_rlast,
  input  logic              t_axi_rvalid,
  output logic              t_axi_rready
);

  typedef enum logic [3:0] {
    X_IDLE,
    X_STE_REQ, X_STE_WAIT, X_STE_DEC,
    X_CD_REQ,  X_CD_WAIT,  X_CD_DEC,
    X_WALK_START, X_WALK,
    X_RSP
  } state_e;

  state_e            state_q;

  logic [ADDR_W-1:0] addr_q;
  logic [SID_W-1:0]  sid_q;
  logic              write_q;
  logic [2:0]        prot_q;

  logic [63:0]       ste_q;
  logic [63:0]       cd0_q, cd1_q;

  // ---------------- table memory read port ----------------
  logic              mem_req, mem_two, mem_done, mem_err;
  logic [ADDR_W-1:0] mem_addr;
  logic [63:0]       mem_data0, mem_data1;

  sg_memrd #(.ADDR_W(ADDR_W)) u_memrd (
    .clk, .rst_n,
    .req_valid (mem_req),
    .req_addr  (mem_addr),
    .req_two   (mem_two),
    .done      (mem_done),
    .data0     (mem_data0),
    .data1     (mem_data1),
    .err       (mem_err),
    .t_axi_arid, .t_axi_araddr, .t_axi_arlen, .t_axi_arsize, .t_axi_arburst,
    .t_axi_arprot, .t_axi_arvalid, .t_axi_arready,
    .t_axi_rid, .t_axi_rdata, .t_axi_rresp, .t_axi_rlast, .t_axi_rvalid, .t_axi_rready
  );

  // ---------------- page table walker ----------------
  logic              ptw_start, ptw_done;
  logic [7:0]        ptw_fcode;
  logic [1:0]        ptw_flevel;
  logic [ADDR_W-1:0] ptw_pa;
  logic              ptw_mem_req;
  logic [ADDR_W-1:0] ptw_mem_addr;

  logic [5:0]        cd_t0sz;
  assign cd_t0sz = cd0_q[sg_pkg::CD_T0SZ_LSB +: 6];

  sg_ptw #(.ADDR_W(ADDR_W)) u_ptw (
    .clk, .rst_n,
    .start    (ptw_start),
    .va       (addr_q),
    .ttb      (cd1_q[47:12]),
    .t0sz     (cd_t0sz),
    .affd     (cd0_q[sg_pkg::CD_AFFD]),
    .is_write (write_q),
    .prot     (prot_q),
    .done     (ptw_done),
    .fcode    (ptw_fcode),
    .flevel   (ptw_flevel),
    .pa       (ptw_pa),
    .mem_req  (ptw_mem_req),
    .mem_addr (ptw_mem_addr),
    .mem_done (mem_done),
    .mem_data (mem_data0),
    .mem_err  (mem_err)
  );

  assign ptw_start = (state_q == X_WALK_START);

  // ---------------- memory request mux ----------------
  // Stream table entries and context descriptors are 64 bytes each.
  // Only the first word of the STE and the first two of the CD are
  // needed.
  logic [ADDR_W-1:0] ste_addr, cd_addr;
  assign ste_addr = {strtab_base_i, 6'b0} + {{(ADDR_W-SID_W-6){1'b0}}, sid_q, 6'b0};
  assign cd_addr  = {ste_q[47:6], 6'b0};

  always_comb begin
    case (state_q)
      X_STE_REQ, X_STE_WAIT: begin
        mem_req  = (state_q == X_STE_REQ) & ~sid_out_of_range;
        mem_addr = ste_addr;
        mem_two  = 1'b0;
      end
      X_CD_REQ, X_CD_WAIT: begin
        mem_req  = (state_q == X_CD_REQ);
        mem_addr = cd_addr;
        mem_two  = 1'b1;
      end
      default: begin
        mem_req  = ptw_mem_req;
        mem_addr = ptw_mem_addr;
        mem_two  = 1'b0;
      end
    endcase
  end

  // ---------------- configuration checks ----------------
  // StreamID must index inside the stream table (2^LOG2SIZE entries).
  logic sid_out_of_range;
  always_comb begin
    sid_out_of_range = 1'b0;
    for (int i = 0; i < SID_W; i++) begin
      if (i >= strtab_log2size_i && sid_q[i]) sid_out_of_range = 1'b1;
    end
  end

  logic [2:0] ste_cfg;
  logic       ste_bad;
  assign ste_cfg = ste_q[sg_pkg::STE_CFG_LSB +: 3];
  // Invalid, a configuration this design does not implement (stage 2),
  // or a context descriptor table format other than a single linear one
  assign ste_bad = ~ste_q[sg_pkg::STE_V] |
                   ~((ste_cfg == sg_pkg::STE_CFG_ABORT)  |
                     (ste_cfg == sg_pkg::STE_CFG_BYPASS) |
                     (ste_cfg == sg_pkg::STE_CFG_S1)) |
                   ((ste_cfg == sg_pkg::STE_CFG_S1) &
                    (ste_q[sg_pkg::STE_S1FMT_LSB +: 2] != 2'b00));

  logic cd_bad;
  assign cd_bad = ~cd0_q[sg_pkg::CD_V] |
                  ~cd0_q[sg_pkg::CD_AA64] |
                   cd0_q[sg_pkg::CD_ENDI] |
                  (cd0_q[sg_pkg::CD_TG0_LSB +: 2] != 2'b00) |
                  (cd_t0sz < 6'd16) | (cd_t0sz > 6'd39);

  // ---------------- control ----------------
  assign req_ready = (state_q == X_IDLE);

  // Finish with a fault / with success
  task automatic finish(input logic fault, input logic [7:0] code,
                        input logic [1:0] level, input logic [ADDR_W-1:0] oaddr);
    rsp_fault  <= fault;
    rsp_fcode  <= code;
    rsp_flevel <= level;
    rsp_addr   <= oaddr;
    state_q    <= X_RSP;
  endtask

  assign rsp_valid = (state_q == X_RSP);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q    <= X_IDLE;
      addr_q     <= '0;
      sid_q      <= '0;
      write_q    <= 1'b0;
      prot_q     <= '0;
      ste_q      <= '0;
      cd0_q      <= '0;
      cd1_q      <= '0;
      rsp_fault  <= 1'b0;
      rsp_fcode  <= sg_pkg::FC_NONE;
      rsp_flevel <= 2'd0;
      rsp_addr   <= '0;
    end else begin
      case (state_q)
        X_IDLE: begin
          if (req_valid) begin
            addr_q  <= req_addr;
            sid_q   <= req_sid;
            write_q <= req_write;
            prot_q  <= req_prot;
            if (!smmuen_i) begin
              // global bypass or abort, no event
              finish(gbpa_abort_i, sg_pkg::FC_NONE, 2'd0, req_addr);
            end else begin
              state_q <= X_STE_REQ;
            end
          end
        end

        X_STE_REQ: begin
          if (sid_out_of_range) finish(1'b1, sg_pkg::FC_BAD_STREAMID, 2'd0, addr_q);
          else                  state_q <= X_STE_WAIT;
        end

        X_STE_WAIT: begin
          if (mem_done) begin
            ste_q   <= mem_data0;
            state_q <= X_STE_DEC;
            if (mem_err) finish(1'b1, sg_pkg::FC_STE_FETCH, 2'd0, addr_q);
          end
        end

        X_STE_DEC: begin
          if (ste_bad)
            finish(1'b1, sg_pkg::FC_BAD_STE, 2'd0, addr_q);
          else if (ste_cfg == sg_pkg::STE_CFG_ABORT)
            finish(1'b1, sg_pkg::FC_NONE, 2'd0, addr_q);
          else if (ste_cfg == sg_pkg::STE_CFG_BYPASS)
            finish(1'b0, sg_pkg::FC_NONE, 2'd0, addr_q);
          else
            state_q <= X_CD_REQ;
        end

        X_CD_REQ: state_q <= X_CD_WAIT;

        X_CD_WAIT: begin
          if (mem_done) begin
            cd0_q   <= mem_data0;
            cd1_q   <= mem_data1;
            state_q <= X_CD_DEC;
            if (mem_err) finish(1'b1, sg_pkg::FC_CD_FETCH, 2'd0, addr_q);
          end
        end

        X_CD_DEC: begin
          if (cd_bad)
            finish(1'b1, sg_pkg::FC_BAD_CD, 2'd0, addr_q);
          else if (cd0_q[sg_pkg::CD_EPD0])
            // walks through TTB0 are disabled for this context
            finish(1'b1, sg_pkg::FC_TRANSLATION, 2'd0, addr_q);
          else
            state_q <= X_WALK_START;
        end

        X_WALK_START: state_q <= X_WALK;

        X_WALK: begin
          if (ptw_done)
            finish(ptw_fcode != sg_pkg::FC_NONE, ptw_fcode, ptw_flevel,
                   (ptw_fcode != sg_pkg::FC_NONE) ? addr_q : ptw_pa);
        end

        X_RSP: state_q <= X_IDLE;

        default: state_q <= X_IDLE;
      endcase
    end
  end

endmodule
