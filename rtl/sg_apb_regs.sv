// StreamGuard - APB4 programming interface.
//
// Zero wait state APB4 completer holding the software-visible
// registers. Unimplemented offsets are RAZ/WI, so PSLVERR is never
// raised.
//
// Implements the ID registers, CR0/CR0ACK, GBPA, IRQ_CTRL, GERROR,
// the stream table base, the command and event queue registers, and
// StreamGuard-specific registers for the caches:
//
//   SG_CTRL   [0] CACHE_DIS: no lookups hit and nothing is filled
//   SG_INV    write 1 to [0]: invalidate the TLB, walk cache and
//             configuration cache; write 1 to [1]: clear the counters.
//             Reads as zero.
//   SG_*_HIT / _MISS   event counters, read only
//
// Queues. A queue has 2^LOG2SIZE entries (at most 16). PROD and CONS
// each hold an index in [LOG2SIZE-1:0] and a wrap bit at [LOG2SIZE]:
//
//   empty   index equal, wrap equal
//   full    index equal, wrap different
//
// Command queue: software writes commands and moves CMDQ_PROD; the
// hardware moves CMDQ_CONS. Event queue: the hardware writes records
// and moves EVENTQ_PROD; software moves EVENTQ_CONS. All pointer
// arithmetic is done here, so the queue logic only sees "pending",
// "full" and the index to use.
//
// Errors. GERROR bits toggle when an error occurs; software toggles
// the same bit in GERRORN to acknowledge. An error is active while the
// two differ.

module sg_apb_regs (
  input  logic        clk,
  input  logic        rst_n,

  // APB4
  input  logic [16:0] paddr,
  input  logic        psel,
  input  logic        penable,
  input  logic        pwrite,
  input  logic [31:0] pwdata,
  input  logic [3:0]  pstrb,
  output logic        pready,
  output logic [31:0] prdata,
  output logic        pslverr,

  // Control outputs to the datapath
  output logic        smmuen_o,
  output logic        cmdqen_o,
  output logic        eventqen_o,
  output logic        gbpa_abort_o,

  // Stream table location: base address [47:6] and log2(entries)
  output logic [47:6] strtab_base_o,
  output logic [5:0]  strtab_log2size_o,

  // Command queue
  output logic [47:5] cmdq_base_o,
  output logic        cmdq_pending_o,      // PROD != CONS
  output logic [3:0]  cmdq_rd_idx_o,
  output logic        cmdq_err_active_o,
  input  logic        cmdq_cons_inc_i,     // command consumed
  input  logic        cmdq_err_i,          // command error
  input  logic [6:0]  cmdq_err_code_i,

  // Event queue
  output logic [47:5] evtq_base_o,
  output logic        evtq_full_o,
  output logic [3:0]  evtq_wr_idx_o,
  input  logic        evtq_prod_inc_i,     // record written
  input  logic        evtq_ovf_i,          // record lost, queue full
  input  logic        evtq_abt_i,          // record write failed on the bus

  // Interrupts, one cycle pulses
  output logic        irq_eventq_o,        // queue went from empty to not empty
  output logic        irq_gerror_o,        // a GERROR bit became active

  // Caches
  output logic        cache_dis_o,
  output logic        inv_all_o,          // one cycle pulse
  output logic        cnt_clr_o,          // one cycle pulse
  input  logic [31:0] cnt_tlb_hit_i,
  input  logic [31:0] cnt_tlb_miss_i,
  input  logic [31:0] cnt_wc_hit_i,
  input  logic [31:0] cnt_cfg_hit_i,
  input  logic [31:0] cnt_cfg_miss_i
);

  logic [31:0] cr0_q, cr0ack_q;
  logic        gbpa_abort_q, gbpa_update_q;
  logic [31:0] strtab_lo_q, strtab_hi_q, strtab_cfg_q;
  logic        cache_dis_q;

  logic [31:0] irq_ctrl_q, irq_ctrlack_q;
  logic [31:0] gerror_q, gerrorn_q;

  logic [31:0] cmdq_lo_q, cmdq_hi_q;
  logic [4:0]  cmdq_prod_q, cmdq_cons_q;
  logic [6:0]  cmdq_errcode_q;

  logic [31:0] evtq_lo_q, evtq_hi_q;
  logic [4:0]  evtq_prod_q, evtq_cons_q;
  logic        evtq_ovflg_q, evtq_ovack_q;

  logic        wr_en;
  logic [16:0] addr;

  assign pready  = 1'b1;
  assign pslverr = 1'b0;

  // A write lands in the access phase (PSEL & PENABLE).
  assign wr_en = psel & penable & pwrite;
  assign addr  = {paddr[16:2], 2'b00};

  // Merge write data into the current value byte by byte.
  function automatic [31:0] strb_merge(input [31:0] cur,
                                       input [31:0] wdata,
                                       input [3:0]  strb);
    for (int i = 0; i < 4; i++) begin
      strb_merge[8*i +: 8] = strb[i] ? wdata[8*i +: 8] : cur[8*i +: 8];
    end
  endfunction

  logic [31:0] gbpa_rd, gbpa_wr;
  assign gbpa_rd = {gbpa_update_q, 10'b0, gbpa_abort_q, 20'b0};
  assign gbpa_wr = strb_merge(gbpa_rd, pwdata, pstrb);

  // ---------------- queue pointer arithmetic ----------------
  // LOG2SIZE above the supported maximum is treated as the maximum.
  logic [2:0] cmdq_l2, evtq_l2;
  logic [4:0] cmdq_pmask, cmdq_imask, evtq_pmask, evtq_imask;
  logic [4:0] cmdq_diff, evtq_diff;
  logic       evtq_empty;

  assign cmdq_l2 = (cmdq_lo_q[4:0] > sg_pkg::QUEUE_MAX_LOG2) ? 3'd4 : cmdq_lo_q[2:0];
  assign evtq_l2 = (evtq_lo_q[4:0] > sg_pkg::QUEUE_MAX_LOG2) ? 3'd4 : evtq_lo_q[2:0];

  // index bits [LOG2SIZE-1:0], pointer bits (index and wrap) [LOG2SIZE:0]
  assign cmdq_imask = 5'((6'd1 << cmdq_l2) - 6'd1);
  assign cmdq_pmask = 5'((6'd2 << cmdq_l2) - 6'd1);
  assign evtq_imask = 5'((6'd1 << evtq_l2) - 6'd1);
  assign evtq_pmask = 5'((6'd2 << evtq_l2) - 6'd1);

  assign cmdq_diff = (cmdq_prod_q ^ cmdq_cons_q) & cmdq_pmask;
  assign evtq_diff = (evtq_prod_q ^ evtq_cons_q) & evtq_pmask;

  assign cmdq_pending_o = (cmdq_diff != 5'd0);
  assign cmdq_rd_idx_o  = 4'(cmdq_cons_q & cmdq_imask);

  assign evtq_empty    = (evtq_diff == 5'd0);
  assign evtq_full_o   = (evtq_diff == (5'd1 << evtq_l2));     // only the wrap bit differs
  assign evtq_wr_idx_o = 4'(evtq_prod_q & evtq_imask);

  // ---------------- errors ----------------
  logic cmdq_err_active, evtq_abt_active;
  logic cmdq_err_new, evtq_abt_new;

  assign cmdq_err_active = gerror_q[sg_pkg::GERROR_CMDQ_ERR] ^ gerrorn_q[sg_pkg::GERROR_CMDQ_ERR];
  assign evtq_abt_active = gerror_q[sg_pkg::GERROR_EVENTQ_ABT] ^ gerrorn_q[sg_pkg::GERROR_EVENTQ_ABT];
  assign cmdq_err_new    = cmdq_err_i & ~cmdq_err_active;
  assign evtq_abt_new    = evtq_abt_i & ~evtq_abt_active;

  assign cmdq_err_active_o = cmdq_err_active;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      cr0_q          <= '0;
      cr0ack_q       <= '0;
      gbpa_abort_q   <= 1'b0;   // reset state: bypass, not abort
      gbpa_update_q  <= 1'b0;
      strtab_lo_q    <= '0;
      strtab_hi_q    <= '0;
      strtab_cfg_q   <= '0;
      cache_dis_q    <= 1'b0;
      irq_ctrl_q     <= '0;
      irq_ctrlack_q  <= '0;
      gerror_q       <= '0;
      gerrorn_q      <= '0;
      cmdq_lo_q      <= '0;
      cmdq_hi_q      <= '0;
      cmdq_prod_q    <= '0;
      cmdq_cons_q    <= '0;
      cmdq_errcode_q <= sg_pkg::CERROR_NONE;
      evtq_lo_q      <= '0;
      evtq_hi_q      <= '0;
      evtq_prod_q    <= '0;
      evtq_cons_q    <= '0;
      evtq_ovflg_q   <= 1'b0;
      evtq_ovack_q   <= 1'b0;
      irq_eventq_o   <= 1'b0;
      irq_gerror_o   <= 1'b0;
    end else begin
      // CR0ACK follows CR0 one cycle later. Software is expected to
      // poll CR0ACK before relying on a CR0 change. IRQ_CTRLACK works
      // the same way.
      cr0ack_q      <= cr0_q;
      irq_ctrlack_q <= irq_ctrl_q;
      gbpa_update_q <= 1'b0;

      // ---- software writes ----
      if (wr_en) begin
        case (addr)
          sg_pkg::REG_CR0: cr0_q <= strb_merge(cr0_q, pwdata, pstrb) & sg_pkg::CR0_WMASK;
          sg_pkg::REG_GBPA: begin
            // GBPA only changes when written with UPDATE set. UPDATE
            // then reads back as 1 until the new value is in effect.
            if (gbpa_wr[sg_pkg::GBPA_UPDATE]) begin
              gbpa_abort_q  <= gbpa_wr[sg_pkg::GBPA_ABORT];
              gbpa_update_q <= 1'b1;
            end
          end
          sg_pkg::REG_IRQ_CTRL:
            irq_ctrl_q <= strb_merge(irq_ctrl_q, pwdata, pstrb) & 32'h0000_0005;
          sg_pkg::REG_GERRORN:
            gerrorn_q  <= strb_merge(gerrorn_q, pwdata, pstrb) & 32'h0000_0005;
          // STRTAB_BASE.ADDR is [51:6]; bits above 47 are not
          // implemented (OAS is 48 bits). Only LOG2SIZE is kept from
          // STRTAB_BASE_CFG: the table format is always linear.
          sg_pkg::REG_STRTAB_BASE_LO:
            strtab_lo_q  <= strb_merge(strtab_lo_q, pwdata, pstrb) & 32'hFFFF_FFC0;
          sg_pkg::REG_STRTAB_BASE_HI:
            strtab_hi_q  <= strb_merge(strtab_hi_q, pwdata, pstrb) & 32'h0000_FFFF;
          sg_pkg::REG_STRTAB_BASE_CFG:
            strtab_cfg_q <= strb_merge(strtab_cfg_q, pwdata, pstrb) & 32'h0000_003F;
          // Queue base registers: LOG2SIZE [4:0], ADDR [47:5]
          sg_pkg::REG_CMDQ_BASE_LO:
            cmdq_lo_q <= strb_merge(cmdq_lo_q, pwdata, pstrb);
          sg_pkg::REG_CMDQ_BASE_HI:
            cmdq_hi_q <= strb_merge(cmdq_hi_q, pwdata, pstrb) & 32'h0000_FFFF;
          sg_pkg::REG_CMDQ_PROD:
            if (pstrb[0]) cmdq_prod_q <= pwdata[4:0];
          // CMDQ_CONS and EVENTQ_PROD belong to the hardware. Software
          // may only set them while the queue is disabled.
          sg_pkg::REG_CMDQ_CONS:
            if (pstrb[0] && !cr0ack_q[sg_pkg::CR0_CMDQEN]) cmdq_cons_q <= pwdata[4:0];
          sg_pkg::REG_EVENTQ_BASE_LO:
            evtq_lo_q <= strb_merge(evtq_lo_q, pwdata, pstrb);
          sg_pkg::REG_EVENTQ_BASE_HI:
            evtq_hi_q <= strb_merge(evtq_hi_q, pwdata, pstrb) & 32'h0000_FFFF;
          sg_pkg::REG_EVENTQ_PROD:
            if (!cr0ack_q[sg_pkg::CR0_EVENTQEN]) begin
              if (pstrb[0]) evtq_prod_q  <= pwdata[4:0];
              if (pstrb[3]) evtq_ovflg_q <= pwdata[31];
            end
          sg_pkg::REG_EVENTQ_CONS: begin
            if (pstrb[0]) evtq_cons_q  <= pwdata[4:0];
            if (pstrb[3]) evtq_ovack_q <= pwdata[31];
          end
          sg_pkg::REG_SG_CTRL:
            if (pstrb[0]) cache_dis_q <= pwdata[sg_pkg::SG_CTRL_CACHE_DIS];
          default: ;
        endcase
      end

      // ---- hardware updates (win over a software write) ----
      if (cmdq_cons_inc_i) begin
        cmdq_cons_q <= (cmdq_cons_q + 5'd1) & cmdq_pmask;
      end
      if (cmdq_err_new) begin
        gerror_q[sg_pkg::GERROR_CMDQ_ERR] <= ~gerror_q[sg_pkg::GERROR_CMDQ_ERR];
        cmdq_errcode_q                    <= cmdq_err_code_i;
      end

      if (evtq_prod_inc_i) begin
        evtq_prod_q <= (evtq_prod_q + 5'd1) & evtq_pmask;
      end
      // A lost record raises the overflow flag, unless an earlier
      // overflow is still waiting for software to acknowledge it.
      if (evtq_ovf_i && (evtq_ovflg_q == evtq_ovack_q)) begin
        evtq_ovflg_q <= ~evtq_ovflg_q;
      end
      if (evtq_abt_new) begin
        gerror_q[sg_pkg::GERROR_EVENTQ_ABT] <= ~gerror_q[sg_pkg::GERROR_EVENTQ_ABT];
      end

      // ---- interrupts ----
      irq_eventq_o <= irq_ctrlack_q[sg_pkg::IRQ_EVENTQ_EN] & evtq_prod_inc_i & evtq_empty;
      irq_gerror_o <= irq_ctrlack_q[sg_pkg::IRQ_GERROR_EN] & (cmdq_err_new | evtq_abt_new);
    end
  end

  assign inv_all_o = wr_en && (addr == sg_pkg::REG_SG_INV) && pstrb[0] &&
                     pwdata[sg_pkg::SG_INV_ALL];
  assign cnt_clr_o = wr_en && (addr == sg_pkg::REG_SG_INV) && pstrb[0] &&
                     pwdata[sg_pkg::SG_INV_COUNTERS];

  always_comb begin
    case (addr)
      sg_pkg::REG_IDR0:   prdata = sg_pkg::IDR0_VALUE;
      sg_pkg::REG_IDR1:   prdata = sg_pkg::IDR1_VALUE;
      sg_pkg::REG_IDR5:   prdata = sg_pkg::IDR5_VALUE;
      sg_pkg::REG_CR0:    prdata = cr0_q;
      sg_pkg::REG_CR0ACK: prdata = cr0ack_q;
      sg_pkg::REG_GBPA:   prdata = gbpa_rd;
      sg_pkg::REG_IRQ_CTRL:        prdata = irq_ctrl_q;
      sg_pkg::REG_IRQ_CTRLACK:     prdata = irq_ctrlack_q;
      sg_pkg::REG_GERROR:          prdata = gerror_q;
      sg_pkg::REG_GERRORN:         prdata = gerrorn_q;
      sg_pkg::REG_STRTAB_BASE_LO:  prdata = strtab_lo_q;
      sg_pkg::REG_STRTAB_BASE_HI:  prdata = strtab_hi_q;
      sg_pkg::REG_STRTAB_BASE_CFG: prdata = strtab_cfg_q;
      sg_pkg::REG_CMDQ_BASE_LO:    prdata = cmdq_lo_q;
      sg_pkg::REG_CMDQ_BASE_HI:    prdata = cmdq_hi_q;
      sg_pkg::REG_CMDQ_PROD:       prdata = {27'b0, cmdq_prod_q};
      sg_pkg::REG_CMDQ_CONS:       prdata = {1'b0, cmdq_errcode_q, 19'b0, cmdq_cons_q};
      sg_pkg::REG_EVENTQ_BASE_LO:  prdata = evtq_lo_q;
      sg_pkg::REG_EVENTQ_BASE_HI:  prdata = evtq_hi_q;
      sg_pkg::REG_EVENTQ_PROD:     prdata = {evtq_ovflg_q, 26'b0, evtq_prod_q};
      sg_pkg::REG_EVENTQ_CONS:     prdata = {evtq_ovack_q, 26'b0, evtq_cons_q};
      sg_pkg::REG_SG_CTRL:         prdata = {31'b0, cache_dis_q};
      sg_pkg::REG_SG_TLB_HIT:      prdata = cnt_tlb_hit_i;
      sg_pkg::REG_SG_TLB_MISS:     prdata = cnt_tlb_miss_i;
      sg_pkg::REG_SG_WC_HIT:       prdata = cnt_wc_hit_i;
      sg_pkg::REG_SG_CFG_HIT:      prdata = cnt_cfg_hit_i;
      sg_pkg::REG_SG_CFG_MISS:     prdata = cnt_cfg_miss_i;
      default:    prdata = '0;
    endcase
  end

  // The datapath acts on the acknowledged value, so what software
  // reads in CR0ACK is what the hardware is doing.
  assign smmuen_o     = cr0ack_q[sg_pkg::CR0_SMMUEN];
  assign cmdqen_o     = cr0ack_q[sg_pkg::CR0_CMDQEN];
  assign eventqen_o   = cr0ack_q[sg_pkg::CR0_EVENTQEN];
  assign gbpa_abort_o = gbpa_abort_q;

  assign strtab_base_o     = {strtab_hi_q[15:0], strtab_lo_q[31:6]};
  assign strtab_log2size_o = strtab_cfg_q[5:0];
  assign cmdq_base_o       = {cmdq_hi_q[15:0], cmdq_lo_q[31:5]};
  assign evtq_base_o       = {evtq_hi_q[15:0], evtq_lo_q[31:5]};
  assign cache_dis_o       = cache_dis_q;

endmodule
