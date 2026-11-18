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
// Invalidation. Commands from the command queue (sg_cmdq) run only
// between lookups: a waiting command is taken before the next request,
// and no request is accepted until it has finished. The register
// invalidation (inv_i) clears all three caches at any time; if it
// arrives while a lookup is in progress, that lookup may already hold
// table data read before the software change, so it fills nothing.
//
// Faults. A fault always terminates the transaction. Before the
// response is given, the fault is written to the event queue
// (sg_evtq), except:
//
//   * global abort and STE.Config = abort record nothing
//   * F_TRANSLATION, F_ADDR_SIZE, F_ACCESS and F_PERMISSION are only
//     recorded when the context descriptor has CD.R set

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

  // Command queue
  input  logic              cmdqen_i,
  input  logic              cmdq_pending_i,
  input  logic              cmdq_err_active_i,
  input  logic [47:5]       cmdq_base_i,
  input  logic [3:0]        cmdq_rd_idx_i,
  output logic              cmdq_cons_inc_o,
  output logic              cmdq_err_o,
  output logic [6:0]        cmdq_err_code_o,

  // Event queue
  input  logic              eventqen_i,
  input  logic              evtq_full_i,
  input  logic [47:5]       evtq_base_i,
  input  logic [3:0]        evtq_wr_idx_i,
  output logic              evtq_prod_inc_o,
  output logic              evtq_ovf_o,
  output logic              evtq_abt_o,

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

  // Memory port (AXI4): reads fetch tables and commands, writes store
  // event records
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
  output logic              t_axi_rready,

  output logic              t_axi_awid,
  output logic [ADDR_W-1:0] t_axi_awaddr,
  output logic [7:0]        t_axi_awlen,
  output logic [2:0]        t_axi_awsize,
  output logic [1:0]        t_axi_awburst,
  output logic [2:0]        t_axi_awprot,
  output logic              t_axi_awvalid,
  input  logic              t_axi_awready,
  output logic [63:0]       t_axi_wdata,
  output logic [7:0]        t_axi_wstrb,
  output logic              t_axi_wlast,
  output logic              t_axi_wvalid,
  input  logic              t_axi_wready,
  input  logic              t_axi_bid,
  input  logic [1:0]        t_axi_bresp,
  input  logic              t_axi_bvalid,
  output logic              t_axi_bready
);

  typedef enum logic [3:0] {
    X_IDLE,
    X_LOOKUP,
    X_STE_REQ, X_STE_WAIT, X_STE_DEC,
    X_CD_REQ,  X_CD_WAIT,  X_CD_DEC,
    X_WALK_START, X_WALK,
    X_EVT_START, X_EVT_WAIT,
    X_RSP,
    X_CMD
  } state_e;

  // Configuration kinds held in the configuration cache
  localparam logic [1:0] K_ABORT  = 2'd0;
  localparam logic [1:0] K_BYPASS = 2'd1;
  localparam logic [1:0] K_S1     = 2'd2;

  // Configuration cache data: {kind, asid, affd, rec, t0sz, ttb[47:12]}
  localparam int unsigned CFG_D_W = 2 + 8 + 1 + 1 + 6 + 36;

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
  logic              cur_rec_q;          // CD.R: record stage 1 faults
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

  // ---------------- command queue ----------------
  logic              cmd_want, cmd_go, cmd_done;
  logic              cmd_mem_req;
  logic [ADDR_W-1:0] cmd_mem_addr;
  logic              cmd_tlb_inv, cmd_wc_inv, cmd_cfg_inv;
  logic              cmd_asid_en, cmd_va_en;
  logic [7:0]        cmd_asid;
  logic [47:12]      cmd_va;
  logic [SID_W-1:0]  cmd_cfg_sid, cmd_cfg_mask;

  sg_cmdq #(.ADDR_W(ADDR_W), .SID_W(SID_W)) u_cmdq (
    .clk, .rst_n,
    .cmdqen_i       (cmdqen_i),
    .pending_i      (cmdq_pending_i),
    .err_active_i   (cmdq_err_active_i),
    .base_i         (cmdq_base_i),
    .rd_idx_i       (cmdq_rd_idx_i),
    .want_o         (cmd_want),
    .go_i           (cmd_go),
    .done_o         (cmd_done),
    .mem_req_o      (cmd_mem_req),
    .mem_addr_o     (cmd_mem_addr),
    .mem_done_i     (mem_done),
    .mem_data0_i    (mem_data0),
    .mem_data1_i    (mem_data1),
    .mem_err_i      (mem_err),
    .cons_inc_o     (cmdq_cons_inc_o),
    .err_o          (cmdq_err_o),
    .err_code_o     (cmdq_err_code_o),
    .tlb_inv_o      (cmd_tlb_inv),
    .wc_inv_o       (cmd_wc_inv),
    .inv_asid_en_o  (cmd_asid_en),
    .inv_va_en_o    (cmd_va_en),
    .inv_asid_o     (cmd_asid),
    .inv_va_o       (cmd_va),
    .cfg_inv_o      (cmd_cfg_inv),
    .cfg_inv_sid_o  (cmd_cfg_sid),
    .cfg_inv_mask_o (cmd_cfg_mask)
  );

  // A waiting command goes before the next request
  assign cmd_go = (state_q == X_IDLE) & cmd_want;

  // ---------------- event queue ----------------
  logic evt_start, evt_done;

  sg_evtq #(.ADDR_W(ADDR_W), .SID_W(SID_W)) u_evtq (
    .clk, .rst_n,
    .start_i    (evt_start),
    .code_i     (rsp_fcode),
    .sid_i      (sid_q),
    .addr_i     (addr_q),
    .write_i    (write_q),
    .prot_i     (prot_q),
    .done_o     (evt_done),
    .eventqen_i (eventqen_i),
    .full_i     (evtq_full_i),
    .base_i     (evtq_base_i),
    .wr_idx_i   (evtq_wr_idx_i),
    .prod_inc_o (evtq_prod_inc_o),
    .ovf_o      (evtq_ovf_o),
    .abt_o      (evtq_abt_o),
    .t_axi_awid, .t_axi_awaddr, .t_axi_awlen, .t_axi_awsize, .t_axi_awburst,
    .t_axi_awprot, .t_axi_awvalid, .t_axi_awready,
    .t_axi_wdata, .t_axi_wstrb, .t_axi_wlast, .t_axi_wvalid, .t_axi_wready,
    .t_axi_bid, .t_axi_bresp, .t_axi_bvalid, .t_axi_bready
  );

  assign evt_start = (state_q == X_EVT_START);

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
    .inv_sel   (cmd_cfg_inv),
    .inv_tag   (cmd_cfg_sid),
    .inv_mask  (cmd_cfg_mask),
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
                        8'b0, 1'b0, 1'b0, 6'b0, 36'b0};
    end else if (state_q == X_CD_DEC && !cd_bad && !cd0_q[sg_pkg::CD_EPD0]) begin
      cfgc_fill      = fill_ok;
      cfgc_fill_data = {K_S1, cd_asid, cd0_q[sg_pkg::CD_AFFD], cd0_q[sg_pkg::CD_R],
                        cd_t0sz, cd1_q[47:12]};
    end
  end

  // Effective configuration in X_LOOKUP: the one already fetched for
  // this request, else the cached one
  logic         eff_valid;
  logic [1:0]   eff_kind;
  logic [7:0]   eff_asid;
  logic         eff_affd;
  logic         eff_rec;
  logic [5:0]   eff_t0sz;
  logic [47:12] eff_ttb;

  assign eff_valid = cfg_have_q | cfgc_hit;
  assign {eff_kind, eff_asid, eff_affd, eff_rec, eff_t0sz, eff_ttb} =
           cfg_have_q ? {cur_kind_q, cur_asid_q, cur_affd_q, cur_rec_q, cur_t0sz_q, cur_ttb_q}
                      : cfgc_data;

  // ---------------- TLB ----------------
  logic              tlb_hit_raw, tlb_hit, tlb_fill, tlb_denied;
  logic [1:0]        tlb_level;
  logic [47:0]       tlb_pa;
  logic              tlb_ap1, tlb_ap2, tlb_pxn, tlb_uxn;

  sg_tlb #(.ENTRIES(TLB_ENTRIES), .ASID_W(8)) u_tlb (
    .clk, .rst_n,
    .inv_all    (inv_i),
    .inv_sel    (cmd_tlb_inv),
    .inv_asid_en(cmd_asid_en),
    .inv_asid   (cmd_asid),
    .inv_va_en  (cmd_va_en),
    .inv_va     (cmd_va),
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
    .inv_sel   (cmd_wc_inv),
    .inv_tag   ({cmd_asid, cmd_va[47:21]}),
    .inv_mask  ({{8{cmd_asid_en}}, {27{cmd_va_en}}}),
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
      X_CMD: begin
        mem_req  = cmd_mem_req;
        mem_addr = cmd_mem_addr;
        mem_two  = 1'b1;                  // a command is two words
      end
      default: begin
        mem_req  = ptw_mem_req;
        mem_addr = ptw_mem_addr;
        mem_two  = 1'b0;
      end
    endcase
  end

  // ---------------- control ----------------
  assign req_ready = (state_q == X_IDLE) & ~cmd_want;
  assign rsp_valid = (state_q == X_RSP);

  // Finish the lookup. A fault with `rec` set is written to the event
  // queue before the response is given.
  task automatic finish(input logic fault, input logic [7:0] code,
                        input logic [1:0] level, input logic [ADDR_W-1:0] oaddr,
                        input logic rec);
    rsp_fault  <= fault;
    rsp_fcode  <= code;
    rsp_flevel <= level;
    rsp_addr   <= oaddr;
    state_q    <= (fault && rec && eventqen_i) ? X_EVT_START : X_RSP;
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
      cur_rec_q  <= 1'b0;
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
          if (cmd_want) begin
            state_q <= X_CMD;
          end else if (req_valid) begin
            addr_q     <= req_addr;
            sid_q      <= req_sid;
            write_q    <= req_write;
            prot_q     <= req_prot;
            cfg_have_q <= 1'b0;
            if (!smmuen_i) begin
              // global bypass or abort, no event
              finish(gbpa_abort_i, sg_pkg::FC_NONE, 2'd0, req_addr, 1'b0);
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
            cur_rec_q  <= eff_rec;
            cur_t0sz_q <= eff_t0sz;
            cur_ttb_q  <= eff_ttb;
            case (eff_kind)
              K_ABORT:  finish(1'b1, sg_pkg::FC_NONE, 2'd0, addr_q, 1'b0);
              K_BYPASS: finish(1'b0, sg_pkg::FC_NONE, 2'd0, addr_q, 1'b0);
              default: begin
                if (tlb_hit) begin
                  if (tlb_denied)
                    finish(1'b1, sg_pkg::FC_PERMISSION, tlb_level, addr_q, eff_rec);
                  else
                    finish(1'b0, sg_pkg::FC_NONE, tlb_level, tlb_pa, 1'b0);
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
          if (sid_out_of_range) finish(1'b1, sg_pkg::FC_BAD_STREAMID, 2'd0, addr_q, 1'b1);
          else                  state_q <= X_STE_WAIT;
        end

        X_STE_WAIT: begin
          if (mem_done) begin
            ste_q   <= mem_data0;
            state_q <= X_STE_DEC;
            if (mem_err) finish(1'b1, sg_pkg::FC_STE_FETCH, 2'd0, addr_q, 1'b1);
          end
        end

        X_STE_DEC: begin
          if (ste_bad)
            finish(1'b1, sg_pkg::FC_BAD_STE, 2'd0, addr_q, 1'b1);
          else if (ste_cfg == sg_pkg::STE_CFG_ABORT)
            finish(1'b1, sg_pkg::FC_NONE, 2'd0, addr_q, 1'b0);
          else if (ste_cfg == sg_pkg::STE_CFG_BYPASS)
            finish(1'b0, sg_pkg::FC_NONE, 2'd0, addr_q, 1'b0);
          else
            state_q <= X_CD_REQ;
        end

        X_CD_REQ: state_q <= X_CD_WAIT;

        X_CD_WAIT: begin
          if (mem_done) begin
            cd0_q   <= mem_data0;
            cd1_q   <= mem_data1;
            state_q <= X_CD_DEC;
            if (mem_err) finish(1'b1, sg_pkg::FC_CD_FETCH, 2'd0, addr_q, 1'b1);
          end
        end

        X_CD_DEC: begin
          if (cd_bad) begin
            finish(1'b1, sg_pkg::FC_BAD_CD, 2'd0, addr_q, 1'b1);
          end else if (cd0_q[sg_pkg::CD_EPD0]) begin
            // walks through TTB0 are disabled for this context
            finish(1'b1, sg_pkg::FC_TRANSLATION, 2'd0, addr_q, cd0_q[sg_pkg::CD_R]);
          end else begin
            // configuration known: go back and try the TLB with it
            cfg_have_q <= 1'b1;
            cur_kind_q <= K_S1;
            cur_asid_q <= cd_asid;
            cur_affd_q <= cd0_q[sg_pkg::CD_AFFD];
            cur_rec_q  <= cd0_q[sg_pkg::CD_R];
            cur_t0sz_q <= cd_t0sz;
            cur_ttb_q  <= cd1_q[47:12];
            state_q    <= X_LOOKUP;
          end
        end

        X_WALK_START: state_q <= X_WALK;

        X_WALK: begin
          if (ptw_done)
            finish(ptw_fcode != sg_pkg::FC_NONE, ptw_fcode, ptw_flevel,
                   (ptw_fcode != sg_pkg::FC_NONE) ? addr_q : ptw_pa,
                   (ptw_fcode == sg_pkg::FC_WALK_EABT) | cur_rec_q);
        end

        X_EVT_START: state_q <= X_EVT_WAIT;

        X_EVT_WAIT: if (evt_done) state_q <= X_RSP;

        X_RSP: state_q <= X_IDLE;

        X_CMD: if (cmd_done) state_q <= X_IDLE;

        default: state_q <= X_IDLE;
      endcase
    end
  end

  // ---------------- invalidation during a lookup ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      inv_pend_q <= 1'b0;
    end else if (state_q == X_IDLE || state_q == X_CMD || state_q == X_RSP) begin
      inv_pend_q <= 1'b0;                // no lookup in progress
    end else if (inv_i) begin
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
