// StreamGuard - translation control.
//
// Every address request from the AW and AR channels comes through
// here and gets one of two answers: forward it with this output
// address, or terminate it.
//
// SMMUEN = 0: global decision from GBPA (bypass or abort).
//
// SMMUEN = 1: look the request up, using three caches before going to
// memory:
//
//   configuration cache   StreamID -> abort / bypass / stage 1 with
//                         ASID, T0SZ, AFFD, TTB0
//   TLB                   ASID + VA -> leaf translation and permissions
//   walk cache            ASID + VA[47:21] -> level 3 table address
//
//   1. configuration: from the cache, or read the Stream Table Entry
//      and Context Descriptor (and fill the cache)
//   2. TLB hit: permission check, done. No memory reads.
//   3. TLB miss: walk the table, starting at level 3 if the walk cache
//      hits, and fill the TLB and walk cache from the walk
//
// Only successful lookups are cached. A leaf that ends in a permission
// fault is cached too, because the permission check runs again on
// every hit against the access that hits.
//
// Invalidation (inv_i) clears all three caches at once. If it arrives
// while a lookup is in progress, that lookup may already hold table
// data read before the software change, so it fills nothing.
//
// A fault always terminates the transaction. The fault code and level
// are reported alongside the response for the event queue to record.

module sg_xlate #(
  parameter int unsigned ADDR_W      = 48,
  parameter int unsigned SID_W       = 8,
  parameter int unsigned TLB_ENTRIES = 8,
  parameter int unsigned WC_ENTRIES  = 4,
  parameter int unsigned CFG_ENTRIES = 4
) (
  input  logic              clk,
  input  logic              rst_n,

  input  logic              smmuen_i,
  input  logic              gbpa_abort_i,
  input  logic [47:6]       strtab_base_i,
  input  logic [5:0]        strtab_log2size_i,

  // Cache control and counters
  input  logic              cache_dis_i,
  input  logic              inv_i,
  input  logic              cnt_clr_i,
  output logic [31:0]       cnt_tlb_hit_o,
  output logic [31:0]       cnt_tlb_miss_o,
  output logic [31:0]       cnt_wc_hit_o,
  output logic [31:0]       cnt_cfg_hit_o,
  output logic [31:0]       cnt_cfg_miss_o,

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
    X_LOOKUP,
    X_STE_REQ, X_STE_WAIT, X_STE_DEC,
    X_CD_REQ,  X_CD_WAIT,  X_CD_DEC,
    X_WALK_START, X_WALK,
    X_RSP
  } state_e;

  // Configuration kinds held in the configuration cache
  localparam logic [1:0] K_ABORT  = 2'd0;
  localparam logic [1:0] K_BYPASS = 2'd1;
  localparam logic [1:0] K_S1     = 2'd2;

  // Configuration cache data: {kind, asid, affd, t0sz, ttb[47:12]}
  localparam int unsigned CFG_D_W = 2 + 8 + 1 + 6 + 36;

  state_e            state_q;

  logic [ADDR_W-1:0] addr_q;
  logic [SID_W-1:0]  sid_q;
  logic              write_q;
  logic [2:0]        prot_q;

  logic [63:0]       ste_q;
  logic [63:0]       cd0_q, cd1_q;

  // Configuration in use for the current request
  logic              cfg_have_q;
  logic [1:0]        cur_kind_q;
  logic [7:0]        cur_asid_q;
  logic              cur_affd_q;
  logic [5:0]        cur_t0sz_q;
  logic [47:12]      cur_ttb_q;

  // Walk cache result carried into the walk
  logic              wc_hit_q;
  logic [47:12]      wc_table_q;

  // Set by an invalidation that arrives during a lookup
  logic              inv_pend_q;
  logic              fill_ok;

  assign fill_ok = ~cache_dis_i & ~inv_i & ~inv_pend_q;

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
  logic [63:0]       ptw_leaf;
  logic              ptw_l3tab_valid;
  logic [47:12]      ptw_l3tab;
  logic              ptw_mem_req;
  logic [ADDR_W-1:0] ptw_mem_addr;

  sg_ptw #(.ADDR_W(ADDR_W)) u_ptw (
    .clk, .rst_n,
    .start       (ptw_start),
    .va          (addr_q),
    .ttb         (cur_ttb_q),
    .t0sz        (cur_t0sz_q),
    .affd        (cur_affd_q),
    .is_write    (write_q),
    .prot        (prot_q),
    .start_l3    (wc_hit_q),
    .l3_table    (wc_table_q),
    .done        (ptw_done),
    .fcode       (ptw_fcode),
    .flevel      (ptw_flevel),
    .pa          (ptw_pa),
    .leaf_desc   (ptw_leaf),
    .l3tab_valid (ptw_l3tab_valid),
    .l3tab       (ptw_l3tab),
    .mem_req     (ptw_mem_req),
    .mem_addr    (ptw_mem_addr),
    .mem_done    (mem_done),
    .mem_data    (mem_data0),
    .mem_err     (mem_err)
  );

  assign ptw_start = (state_q == X_WALK_START);

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

  logic [5:0] cd_t0sz;
  logic [7:0] cd_asid;
  logic       cd_bad;
  assign cd_t0sz = cd0_q[sg_pkg::CD_T0SZ_LSB +: 6];
  assign cd_asid = cd0_q[sg_pkg::CD_ASID_LSB +: 8];
  assign cd_bad  = ~cd0_q[sg_pkg::CD_V] |
                   ~cd0_q[sg_pkg::CD_AA64] |
                    cd0_q[sg_pkg::CD_ENDI] |
                   (cd0_q[sg_pkg::CD_TG0_LSB +: 2] != 2'b00) |
                   (cd_t0sz < 6'd16) | (cd_t0sz > 6'd39);

  // ---------------- configuration cache ----------------
  logic               cfgc_hit_raw, cfgc_hit, cfgc_fill;
  logic [CFG_D_W-1:0] cfgc_data, cfgc_fill_data;

  sg_tagcache #(.ENTRIES(CFG_ENTRIES), .TAG_W(SID_W), .DATA_W(CFG_D_W)) u_cfgc (
    .clk, .rst_n,
    .inv_all   (inv_i),
    .lk_tag    (sid_q),
    .hit       (cfgc_hit_raw),
    .hit_data  (cfgc_data),
    .fill      (cfgc_fill),
    .fill_tag  (sid_q),
    .fill_data (cfgc_fill_data)
  );

  assign cfgc_hit = cfgc_hit_raw & ~cache_dis_i;

  always_comb begin
    cfgc_fill      = 1'b0;
    cfgc_fill_data = '0;
    if (state_q == X_STE_DEC && !ste_bad && ste_cfg != sg_pkg::STE_CFG_S1) begin
      cfgc_fill      = fill_ok;
      cfgc_fill_data = {(ste_cfg == sg_pkg::STE_CFG_ABORT) ? K_ABORT : K_BYPASS,
                        8'b0, 1'b0, 6'b0, 36'b0};
    end else if (state_q == X_CD_DEC && !cd_bad && !cd0_q[sg_pkg::CD_EPD0]) begin
      cfgc_fill      = fill_ok;
      cfgc_fill_data = {K_S1, cd_asid, cd0_q[sg_pkg::CD_AFFD], cd_t0sz, cd1_q[47:12]};
    end
  end

  // Effective configuration in X_LOOKUP: the one already fetched for
  // this request, else the cached one
  logic         eff_valid;
  logic [1:0]   eff_kind;
  logic [7:0]   eff_asid;
  logic         eff_affd;
  logic [5:0]   eff_t0sz;
  logic [47:12] eff_ttb;

  assign eff_valid = cfg_have_q | cfgc_hit;
  assign {eff_kind, eff_asid, eff_affd, eff_t0sz, eff_ttb} =
           cfg_have_q ? {cur_kind_q, cur_asid_q, cur_affd_q, cur_t0sz_q, cur_ttb_q}
                      : cfgc_data;

  // ---------------- TLB ----------------
  logic              tlb_hit_raw, tlb_hit, tlb_fill, tlb_denied;
  logic [1:0]        tlb_level;
  logic [47:0]       tlb_pa;
  logic              tlb_ap1, tlb_ap2, tlb_pxn, tlb_uxn;

  sg_tlb #(.ENTRIES(TLB_ENTRIES), .ASID_W(8)) u_tlb (
    .clk, .rst_n,
    .inv_all    (inv_i),
    .lk_asid    (eff_asid),
    .lk_va      (addr_q[47:0]),
    .hit        (tlb_hit_raw),
    .hit_level  (tlb_level),
    .hit_pa     (tlb_pa),
    .hit_ap1    (tlb_ap1),
    .hit_ap2    (tlb_ap2),
    .hit_pxn    (tlb_pxn),
    .hit_uxn    (tlb_uxn),
    .fill       (tlb_fill),
    .fill_asid  (cur_asid_q),
    .fill_level (ptw_flevel),
    .fill_va    (addr_q[47:12]),
    .fill_pa    (ptw_leaf[47:12]),
    .fill_ap1   (ptw_leaf[sg_pkg::DESC_AP1]),
    .fill_ap2   (ptw_leaf[sg_pkg::DESC_AP2]),
    .fill_pxn   (ptw_leaf[sg_pkg::DESC_PXN]),
    .fill_uxn   (ptw_leaf[sg_pkg::DESC_UXN])
  );

  assign tlb_hit = tlb_hit_raw & ~cache_dis_i;

  // A walk that found a usable leaf: successful, or stopped only by
  // the permission check
  assign tlb_fill = (state_q == X_WALK) & ptw_done & fill_ok &
                    ((ptw_fcode == sg_pkg::FC_NONE) | (ptw_fcode == sg_pkg::FC_PERMISSION));

  sg_perm u_hit_perm (
    .is_write (write_q),
    .prot     (prot_q),
    .ap1      (tlb_ap1),
    .ap2      (tlb_ap2),
    .pxn      (tlb_pxn),
    .uxn      (tlb_uxn),
    .denied   (tlb_denied)
  );

  // ---------------- walk cache ----------------
  logic         wc_hit_raw, wc_hit, wc_fill;
  logic [47:12] wc_data;

  sg_tagcache #(.ENTRIES(WC_ENTRIES), .TAG_W(8 + 27), .DATA_W(36)) u_wc (
    .clk, .rst_n,
    .inv_all   (inv_i),
    .lk_tag    ({eff_asid, addr_q[47:21]}),
    .hit       (wc_hit_raw),
    .hit_data  (wc_data),
    .fill      (wc_fill),
    .fill_tag  ({cur_asid_q, addr_q[47:21]}),
    .fill_data (ptw_l3tab)
  );

  assign wc_hit  = wc_hit_raw & ~cache_dis_i;
  assign wc_fill = (state_q == X_WALK) & ptw_done & fill_ok & ptw_l3tab_valid;

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

  // ---------------- control ----------------
  assign req_ready = (state_q == X_IDLE);
  assign rsp_valid = (state_q == X_RSP);

  // Finish with a fault / with success
  task automatic finish(input logic fault, input logic [7:0] code,
                        input logic [1:0] level, input logic [ADDR_W-1:0] oaddr);
    rsp_fault  <= fault;
    rsp_fcode  <= code;
    rsp_flevel <= level;
    rsp_addr   <= oaddr;
    state_q    <= X_RSP;
  endtask

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
      cfg_have_q <= 1'b0;
      cur_kind_q <= K_ABORT;
      cur_asid_q <= '0;
      cur_affd_q <= 1'b0;
      cur_t0sz_q <= '0;
      cur_ttb_q  <= '0;
      wc_hit_q   <= 1'b0;
      wc_table_q <= '0;
      rsp_fault  <= 1'b0;
      rsp_fcode  <= sg_pkg::FC_NONE;
      rsp_flevel <= 2'd0;
      rsp_addr   <= '0;
    end else begin
      case (state_q)
        X_IDLE: begin
          if (req_valid) begin
            addr_q     <= req_addr;
            sid_q      <= req_sid;
            write_q    <= req_write;
            prot_q     <= req_prot;
            cfg_have_q <= 1'b0;
            if (!smmuen_i) begin
              // global bypass or abort, no event
              finish(gbpa_abort_i, sg_pkg::FC_NONE, 2'd0, req_addr);
            end else begin
              state_q <= X_LOOKUP;
            end
          end
        end

        X_LOOKUP: begin
          if (!eff_valid) begin
            state_q <= X_STE_REQ;
          end else begin
            cfg_have_q <= 1'b1;
            cur_kind_q <= eff_kind;
            cur_asid_q <= eff_asid;
            cur_affd_q <= eff_affd;
            cur_t0sz_q <= eff_t0sz;
            cur_ttb_q  <= eff_ttb;
            case (eff_kind)
              K_ABORT:  finish(1'b1, sg_pkg::FC_NONE, 2'd0, addr_q);
              K_BYPASS: finish(1'b0, sg_pkg::FC_NONE, 2'd0, addr_q);
              default: begin
                if (tlb_hit) begin
                  if (tlb_denied)
                    finish(1'b1, sg_pkg::FC_PERMISSION, tlb_level, addr_q);
                  else
                    finish(1'b0, sg_pkg::FC_NONE, tlb_level, tlb_pa);
                end else begin
                  wc_hit_q   <= wc_hit;
                  wc_table_q <= wc_data;
                  state_q    <= X_WALK_START;
                end
              end
            endcase
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
          if (cd_bad) begin
            finish(1'b1, sg_pkg::FC_BAD_CD, 2'd0, addr_q);
          end else if (cd0_q[sg_pkg::CD_EPD0]) begin
            // walks through TTB0 are disabled for this context
            finish(1'b1, sg_pkg::FC_TRANSLATION, 2'd0, addr_q);
          end else begin
            // configuration known: go back and try the TLB with it
            cfg_have_q <= 1'b1;
            cur_kind_q <= K_S1;
            cur_asid_q <= cd_asid;
            cur_affd_q <= cd0_q[sg_pkg::CD_AFFD];
            cur_t0sz_q <= cd_t0sz;
            cur_ttb_q  <= cd1_q[47:12];
            state_q    <= X_LOOKUP;
          end
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

  // ---------------- invalidation during a lookup ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      inv_pend_q <= 1'b0;
    end else if (state_q == X_RSP) begin
      inv_pend_q <= 1'b0;
    end else if (inv_i && state_q != X_IDLE) begin
      inv_pend_q <= 1'b1;
    end
  end

  // ---------------- counters ----------------
  logic [31:0] c_tlb_hit_q, c_tlb_miss_q, c_wc_hit_q, c_cfg_hit_q, c_cfg_miss_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      c_tlb_hit_q  <= '0;
      c_tlb_miss_q <= '0;
      c_wc_hit_q   <= '0;
      c_cfg_hit_q  <= '0;
      c_cfg_miss_q <= '0;
    end else if (cnt_clr_i) begin
      c_tlb_hit_q  <= '0;
      c_tlb_miss_q <= '0;
      c_wc_hit_q   <= '0;
      c_cfg_hit_q  <= '0;
      c_cfg_miss_q <= '0;
    end else if (state_q == X_LOOKUP) begin
      if (!cfg_have_q) begin
        if (cfgc_hit) c_cfg_hit_q  <= c_cfg_hit_q + 1'b1;
        else          c_cfg_miss_q <= c_cfg_miss_q + 1'b1;
      end
      if (eff_valid && eff_kind == K_S1) begin
        if (tlb_hit) begin
          c_tlb_hit_q <= c_tlb_hit_q + 1'b1;
        end else begin
          c_tlb_miss_q <= c_tlb_miss_q + 1'b1;
          if (wc_hit) c_wc_hit_q <= c_wc_hit_q + 1'b1;
        end
      end
    end
  end

  assign cnt_tlb_hit_o  = c_tlb_hit_q;
  assign cnt_tlb_miss_o = c_tlb_miss_q;
  assign cnt_wc_hit_o   = c_wc_hit_q;
  assign cnt_cfg_hit_o  = c_cfg_hit_q;
  assign cnt_cfg_miss_o = c_cfg_miss_q;

endmodule
