// StreamGuard - command queue consumer.
//
// Software writes 16-byte commands into a circular buffer in memory
// and advances CMDQ_PROD. This module fetches the command at
// CMDQ_CONS, carries it out and advances CMDQ_CONS, one command at a
// time.
//
// Commands only run while the translation control is idle (go_i), and
// no lookup starts until the command is finished. So an invalidation
// can never land in the middle of a lookup, and once software sees
// CMD_SYNC consumed, every later lookup uses the new state.
//
//   opcode  command              effect
//   0x01    CMD_PREFETCH_CONFIG  none (prefetch is optional)
//   0x02    CMD_PREFETCH_ADDR    none
//   0x03    CMD_CFGI_STE         drop the cached configuration of one StreamID
//   0x04    CMD_CFGI_STE_RANGE   ... of 2^(Range+1) StreamIDs (Range = 31: all)
//   0x05    CMD_CFGI_CD          as CFGI_STE (one context descriptor per stream)
//   0x06    CMD_CFGI_CD_ALL      as CFGI_STE
//   0x10    CMD_TLBI_NH_ALL      drop all TLB and walk cache entries
//   0x11    CMD_TLBI_NH_ASID     ... those of one ASID
//   0x12    CMD_TLBI_NH_VA       ... those of one ASID covering one address
//   0x13    CMD_TLBI_NH_VAA      ... those of any ASID covering one address
//   0x30    CMD_TLBI_NSNH_ALL    drop all TLB and walk cache entries
//   0x46    CMD_SYNC             none: earlier commands are already complete
//
// TLBI_NH_VA and TLBI_NH_VAA with Leaf = 1 only drop leaf (TLB)
// entries and keep the walk cache.
//
// Any other opcode stops the queue with CERROR_ILL: CMDQ_CONS stays on
// the offending command and GERROR.CMDQ_ERR becomes active. A bus
// error on the fetch does the same with CERROR_ABT. Processing resumes
// when software acknowledges the error in GERRORN.

module sg_cmdq #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  // Queue state from the registers
  input  logic              cmdqen_i,
  input  logic              pending_i,        // PROD != CONS
  input  logic              err_active_i,     // unacknowledged command error
  input  logic [47:5]       base_i,
  input  logic [3:0]        rd_idx_i,

  // Hand-shake with the translation control
  output logic              want_o,           // a command is waiting to run
  input  logic              go_i,             // run it now
  output logic              done_o,           // one cycle pulse

  // Memory read (the port is shared with the table reads)
  output logic              mem_req_o,
  output logic [ADDR_W-1:0] mem_addr_o,
  input  logic              mem_done_i,
  input  logic [63:0]       mem_data0_i,
  input  logic [63:0]       mem_data1_i,
  input  logic              mem_err_i,

  // Results for the registers, one cycle pulses
  output logic              cons_inc_o,
  output logic              err_o,
  output logic [6:0]        err_code_o,

  // Invalidation requests, one cycle pulses
  output logic              tlb_inv_o,
  output logic              wc_inv_o,
  output logic              inv_asid_en_o,    // only entries with inv_asid_o
  output logic              inv_va_en_o,      // only entries covering inv_va_o
  output logic [7:0]        inv_asid_o,
  output logic [47:12]      inv_va_o,
  output logic              cfg_inv_o,
  output logic [SID_W-1:0]  cfg_inv_sid_o,
  output logic [SID_W-1:0]  cfg_inv_mask_o    // StreamID bits that must match
);

  typedef enum logic [1:0] {C_IDLE, C_FETCH, C_WAIT, C_EXEC} state_e;
  state_e      state_q;
  logic [63:0] cmd0_q, cmd1_q;
  logic        abt_q;

  assign want_o = cmdqen_i & pending_i & ~err_active_i & (state_q == C_IDLE);

  assign mem_req_o  = (state_q == C_FETCH);
  assign mem_addr_o = {base_i, 5'b0} + {{(ADDR_W-8){1'b0}}, rd_idx_i, 4'b0};

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q <= C_IDLE;
      cmd0_q  <= '0;
      cmd1_q  <= '0;
      abt_q   <= 1'b0;
    end else begin
      case (state_q)
        C_IDLE:  if (go_i) state_q <= C_FETCH;
        C_FETCH: state_q <= C_WAIT;
        C_WAIT: begin
          if (mem_done_i) begin
            cmd0_q  <= mem_data0_i;
            cmd1_q  <= mem_data1_i;
            abt_q   <= mem_err_i;
            state_q <= C_EXEC;
          end
        end
        C_EXEC:  state_q <= C_IDLE;
        default: state_q <= C_IDLE;
      endcase
    end
  end

  // ---------------- decode ----------------
  logic [7:0] opcode;
  logic       leaf;
  logic       legal, do_tlb, do_wc, do_cfg, asid_en, va_en;

  assign opcode = cmd0_q[7:0];
  assign leaf   = cmd1_q[0];

  always_comb begin
    legal   = 1'b1;
    do_tlb  = 1'b0;
    do_wc   = 1'b0;
    do_cfg  = 1'b0;
    asid_en = 1'b0;
    va_en   = 1'b0;
    case (opcode)
      sg_pkg::CMD_PREFETCH_CONFIG,
      sg_pkg::CMD_PREFETCH_ADDR,
      sg_pkg::CMD_SYNC: ;

      sg_pkg::CMD_CFGI_STE,
      sg_pkg::CMD_CFGI_STE_RANGE,
      sg_pkg::CMD_CFGI_CD,
      sg_pkg::CMD_CFGI_CD_ALL: do_cfg = 1'b1;

      sg_pkg::CMD_TLBI_NH_ALL,
      sg_pkg::CMD_TLBI_NSNH_ALL: begin
        do_tlb = 1'b1;
        do_wc  = 1'b1;
      end
      sg_pkg::CMD_TLBI_NH_ASID: begin
        do_tlb  = 1'b1;
        do_wc   = 1'b1;
        asid_en = 1'b1;
      end
      sg_pkg::CMD_TLBI_NH_VA: begin
        do_tlb  = 1'b1;
        do_wc   = ~leaf;
        asid_en = 1'b1;
        va_en   = 1'b1;
      end
      sg_pkg::CMD_TLBI_NH_VAA: begin
        do_tlb  = 1'b1;
        do_wc   = ~leaf;
        va_en   = 1'b1;
      end
      default: legal = 1'b0;
    endcase
  end

  // CFGI_STE_RANGE covers the 2^(Range+1) StreamIDs that share the
  // bits above Range. Every other CFGI command names one StreamID.
  always_comb begin
    cfg_inv_mask_o = '1;
    if (opcode == sg_pkg::CMD_CFGI_STE_RANGE) begin
      for (int i = 0; i < SID_W; i++) begin
        cfg_inv_mask_o[i] = (5'(i) > cmd1_q[4:0]);
      end
    end
  end

  // ---------------- execute ----------------
  logic exec, ok;
  assign exec = (state_q == C_EXEC);
  assign ok   = exec & ~abt_q & legal;

  assign done_o     = exec;
  assign cons_inc_o = ok;
  assign err_o      = exec & ~ok;
  assign err_code_o = abt_q ? sg_pkg::CERROR_ABT : sg_pkg::CERROR_ILL;

  assign tlb_inv_o     = ok & do_tlb;
  assign wc_inv_o      = ok & do_wc;
  assign cfg_inv_o     = ok & do_cfg;
  assign inv_asid_en_o = asid_en;
  assign inv_va_en_o   = va_en;
  assign inv_asid_o    = cmd0_q[55:48];          // 8-bit ASIDs
  assign inv_va_o      = cmd1_q[47:12];
  assign cfg_inv_sid_o = cmd0_q[32 +: SID_W];

endmodule
