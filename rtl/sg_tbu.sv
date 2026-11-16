// StreamGuard - translation buffer unit (datapath).
//
// Sits in-line on an AXI4 link between a device (upstream, s_axi_*)
// and the memory system (downstream, m_axi_*).
//
//   * AW and AR each go through an address channel handler that gets
//     a forward/terminate decision from the translation logic.
//   * W data follows the decision made for its AW.
//   * A terminated transaction never reaches downstream. The device
//     gets an SLVERR response generated here.
//
// Response ordering. AXI requires responses with the same ID to
// return in request order. A locally generated error response could
// overtake the response of an earlier transaction that is still in
// flight downstream. To rule that out without tracking IDs, a
// terminated transaction
//   - blocks new requests on its channel until its response is sent
//   - only responds once nothing is outstanding downstream
// Terminations are the rare case, so the lost throughput is accepted.

module sg_tbu #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned DATA_W = 64,
  parameter int unsigned ID_W   = 4,
  parameter int unsigned SID_W  = 8
) (
  input  logic                clk,
  input  logic                rst_n,

  // ---------------- upstream: device side ----------------
  input  logic [ID_W-1:0]     s_axi_awid,
  input  logic [ADDR_W-1:0]   s_axi_awaddr,
  input  logic [7:0]          s_axi_awlen,
  input  logic [2:0]          s_axi_awsize,
  input  logic [1:0]          s_axi_awburst,
  input  logic                s_axi_awlock,
  input  logic [3:0]          s_axi_awcache,
  input  logic [2:0]          s_axi_awprot,
  input  logic [3:0]          s_axi_awqos,
  input  logic [SID_W-1:0]    s_axi_awsid,
  input  logic                s_axi_awvalid,
  output logic                s_axi_awready,

  input  logic [DATA_W-1:0]   s_axi_wdata,
  input  logic [DATA_W/8-1:0] s_axi_wstrb,
  input  logic                s_axi_wlast,
  input  logic                s_axi_wvalid,
  output logic                s_axi_wready,

  output logic [ID_W-1:0]     s_axi_bid,
  output logic [1:0]          s_axi_bresp,
  output logic                s_axi_bvalid,
  input  logic                s_axi_bready,

  input  logic [ID_W-1:0]     s_axi_arid,
  input  logic [ADDR_W-1:0]   s_axi_araddr,
  input  logic [7:0]          s_axi_arlen,
  input  logic [2:0]          s_axi_arsize,
  input  logic [1:0]          s_axi_arburst,
  input  logic                s_axi_arlock,
  input  logic [3:0]          s_axi_arcache,
  input  logic [2:0]          s_axi_arprot,
  input  logic [3:0]          s_axi_arqos,
  input  logic [SID_W-1:0]    s_axi_arsid,
  input  logic                s_axi_arvalid,
  output logic                s_axi_arready,

  output logic [ID_W-1:0]     s_axi_rid,
  output logic [DATA_W-1:0]   s_axi_rdata,
  output logic [1:0]          s_axi_rresp,
  output logic                s_axi_rlast,
  output logic                s_axi_rvalid,
  input  logic                s_axi_rready,

  // ---------------- downstream: memory side ----------------
  output logic [ID_W-1:0]     m_axi_awid,
  output logic [ADDR_W-1:0]   m_axi_awaddr,
  output logic [7:0]          m_axi_awlen,
  output logic [2:0]          m_axi_awsize,
  output logic [1:0]          m_axi_awburst,
  output logic                m_axi_awlock,
  output logic [3:0]          m_axi_awcache,
  output logic [2:0]          m_axi_awprot,
  output logic [3:0]          m_axi_awqos,
  output logic                m_axi_awvalid,
  input  logic                m_axi_awready,

  output logic [DATA_W-1:0]   m_axi_wdata,
  output logic [DATA_W/8-1:0] m_axi_wstrb,
  output logic                m_axi_wlast,
  output logic                m_axi_wvalid,
  input  logic                m_axi_wready,

  input  logic [ID_W-1:0]     m_axi_bid,
  input  logic [1:0]          m_axi_bresp,
  input  logic                m_axi_bvalid,
  output logic                m_axi_bready,

  output logic [ID_W-1:0]     m_axi_arid,
  output logic [ADDR_W-1:0]   m_axi_araddr,
  output logic [7:0]          m_axi_arlen,
  output logic [2:0]          m_axi_arsize,
  output logic [1:0]          m_axi_arburst,
  output logic                m_axi_arlock,
  output logic [3:0]          m_axi_arcache,
  output logic [2:0]          m_axi_arprot,
  output logic [3:0]          m_axi_arqos,
  output logic                m_axi_arvalid,
  input  logic                m_axi_arready,

  input  logic [ID_W-1:0]     m_axi_rid,
  input  logic [DATA_W-1:0]   m_axi_rdata,
  input  logic [1:0]          m_axi_rresp,
  input  logic                m_axi_rlast,
  input  logic                m_axi_rvalid,
  output logic                m_axi_rready,

  // ---------------- translation request / response ----------------
  output logic                x_req_valid,
  input  logic                x_req_ready,
  output logic [ADDR_W-1:0]   x_req_addr,
  output logic [SID_W-1:0]    x_req_sid,
  output logic                x_req_write,
  input  logic                x_rsp_valid,
  input  logic                x_rsp_fault,
  input  logic [ADDR_W-1:0]   x_rsp_addr
);

  // id + len + size + burst + lock + cache + prot + qos
  localparam int unsigned PAY_W    = ID_W + 25;
  localparam int unsigned WQ_DEPTH = 4;
  localparam int unsigned OUTST_W  = 6;

  // =================================================================
  // Address channel handlers
  // =================================================================
  logic [PAY_W-1:0]  aw_s_pay, aw_m_pay, aw_term_pay;
  logic [PAY_W-1:0]  ar_s_pay, ar_m_pay, ar_term_pay;

  logic              aw_allow, ar_allow;
  logic              aw_xreq_valid, aw_xreq_ready, aw_xrsp_valid;
  logic              ar_xreq_valid, ar_xreq_ready, ar_xrsp_valid;
  logic [ADDR_W-1:0] aw_xreq_addr, ar_xreq_addr;
  logic [SID_W-1:0]  aw_xreq_sid,  ar_xreq_sid;
  logic              aw_dec_valid, aw_dec_fault;
  logic              ar_dec_valid, ar_dec_fault;
  logic              aw_term_valid, aw_term_ready;
  logic              ar_term_valid, ar_term_ready;

  assign aw_s_pay = {s_axi_awid, s_axi_awlen, s_axi_awsize, s_axi_awburst,
                     s_axi_awlock, s_axi_awcache, s_axi_awprot, s_axi_awqos};
  assign ar_s_pay = {s_axi_arid, s_axi_arlen, s_axi_arsize, s_axi_arburst,
                     s_axi_arlock, s_axi_arcache, s_axi_arprot, s_axi_arqos};

  assign {m_axi_awid, m_axi_awlen, m_axi_awsize, m_axi_awburst,
          m_axi_awlock, m_axi_awcache, m_axi_awprot, m_axi_awqos} = aw_m_pay;
  assign {m_axi_arid, m_axi_arlen, m_axi_arsize, m_axi_arburst,
          m_axi_arlock, m_axi_arcache, m_axi_arprot, m_axi_arqos} = ar_m_pay;

  sg_addr_chan #(.ADDR_W(ADDR_W), .SID_W(SID_W), .PAY_W(PAY_W)) u_aw (
    .clk, .rst_n,
    .allow_i     (aw_allow),
    .s_valid     (s_axi_awvalid),
    .s_ready     (s_axi_awready),
    .s_addr      (s_axi_awaddr),
    .s_sid       (s_axi_awsid),
    .s_pay       (aw_s_pay),
    .x_req_valid (aw_xreq_valid),
    .x_req_ready (aw_xreq_ready),
    .x_req_addr  (aw_xreq_addr),
    .x_req_sid   (aw_xreq_sid),
    .x_rsp_valid (aw_xrsp_valid),
    .x_rsp_fault (x_rsp_fault),
    .x_rsp_addr  (x_rsp_addr),
    .dec_valid_o (aw_dec_valid),
    .dec_fault_o (aw_dec_fault),
    .m_valid     (m_axi_awvalid),
    .m_ready     (m_axi_awready),
    .m_addr      (m_axi_awaddr),
    .m_pay       (aw_m_pay),
    .term_valid  (aw_term_valid),
    .term_ready  (aw_term_ready),
    .term_pay    (aw_term_pay)
  );

  sg_addr_chan #(.ADDR_W(ADDR_W), .SID_W(SID_W), .PAY_W(PAY_W)) u_ar (
    .clk, .rst_n,
    .allow_i     (ar_allow),
    .s_valid     (s_axi_arvalid),
    .s_ready     (s_axi_arready),
    .s_addr      (s_axi_araddr),
    .s_sid       (s_axi_arsid),
    .s_pay       (ar_s_pay),
    .x_req_valid (ar_xreq_valid),
    .x_req_ready (ar_xreq_ready),
    .x_req_addr  (ar_xreq_addr),
    .x_req_sid   (ar_xreq_sid),
    .x_rsp_valid (ar_xrsp_valid),
    .x_rsp_fault (x_rsp_fault),
    .x_rsp_addr  (x_rsp_addr),
    .dec_valid_o (ar_dec_valid),
    .dec_fault_o (ar_dec_fault),
    .m_valid     (m_axi_arvalid),
    .m_ready     (m_axi_arready),
    .m_addr      (m_axi_araddr),
    .m_pay       (ar_m_pay),
    .term_valid  (ar_term_valid),
    .term_ready  (ar_term_ready),
    .term_pay    (ar_term_pay)
  );

  // =================================================================
  // Translation request arbiter: AW and AR share one translation
  // port. Round robin, one request in flight at a time.
  // =================================================================
  logic arb_busy_q, arb_owner_wr_q, arb_last_wr_q;
  logic arb_pick_wr;

  assign arb_pick_wr   = aw_xreq_valid & (~ar_xreq_valid | ~arb_last_wr_q);

  assign x_req_valid   = ~arb_busy_q & (aw_xreq_valid | ar_xreq_valid);
  assign x_req_addr    = arb_pick_wr ? aw_xreq_addr : ar_xreq_addr;
  assign x_req_sid     = arb_pick_wr ? aw_xreq_sid  : ar_xreq_sid;
  assign x_req_write   = arb_pick_wr;

  assign aw_xreq_ready = ~arb_busy_q &  arb_pick_wr & x_req_ready;
  assign ar_xreq_ready = ~arb_busy_q & ~arb_pick_wr & ar_xreq_valid & x_req_ready;

  assign aw_xrsp_valid = x_rsp_valid & arb_busy_q &  arb_owner_wr_q;
  assign ar_xrsp_valid = x_rsp_valid & arb_busy_q & ~arb_owner_wr_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      arb_busy_q     <= 1'b0;
      arb_owner_wr_q <= 1'b0;
      arb_last_wr_q  <= 1'b0;
    end else begin
      if (x_req_valid && x_req_ready) begin
        arb_busy_q     <= 1'b1;
        arb_owner_wr_q <= arb_pick_wr;
        arb_last_wr_q  <= arb_pick_wr;
      end else if (arb_busy_q && x_rsp_valid) begin
        arb_busy_q     <= 1'b0;
      end
    end
  end

  // =================================================================
  // Write data routing
  //
  // AXI4 allows W beats to arrive before their AW, so W cannot simply
  // be passed through. Each AW decision is queued, and a W burst is
  // only consumed once its decision is at the head of the queue:
  // forwarded for a pass, dropped for a terminate.
  // =================================================================
  logic [WQ_DEPTH-1:0]       wq_mem_q;
  logic [$clog2(WQ_DEPTH)-1:0] wq_wptr_q, wq_rptr_q;
  logic [$clog2(WQ_DEPTH):0] wq_cnt_q;
  logic                      wq_empty, wq_full, wq_head_term;
  logic                      wq_push, wq_pop;

  assign wq_empty     = (wq_cnt_q == '0);
  assign wq_full      = (wq_cnt_q == WQ_DEPTH[$clog2(WQ_DEPTH):0]);
  assign wq_head_term = wq_mem_q[wq_rptr_q];
  assign wq_push      = aw_dec_valid;
  assign wq_pop       = s_axi_wvalid & s_axi_wready & s_axi_wlast;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wq_mem_q  <= '0;
      wq_wptr_q <= '0;
      wq_rptr_q <= '0;
      wq_cnt_q  <= '0;
    end else begin
      if (wq_push) begin
        wq_mem_q[wq_wptr_q] <= aw_dec_fault;
        wq_wptr_q           <= wq_wptr_q + 1'b1;
      end
      if (wq_pop) begin
        wq_rptr_q <= wq_rptr_q + 1'b1;
      end
      case ({wq_push, wq_pop})
        2'b10:   wq_cnt_q <= wq_cnt_q + 1'b1;
        2'b01:   wq_cnt_q <= wq_cnt_q - 1'b1;
        default: ;
      endcase
    end
  end

  assign m_axi_wdata  = s_axi_wdata;
  assign m_axi_wstrb  = s_axi_wstrb;
  assign m_axi_wlast  = s_axi_wlast;
  assign m_axi_wvalid = s_axi_wvalid & ~wq_empty & ~wq_head_term;
  assign s_axi_wready = ~wq_empty & (wq_head_term | m_axi_wready);

  // =================================================================
  // Outstanding transaction counters (downstream side)
  // =================================================================
  logic [OUTST_W-1:0] wr_outst_q, rd_outst_q;
  logic               wr_inc, wr_dec, rd_inc, rd_dec;

  assign wr_inc = m_axi_awvalid & m_axi_awready;
  assign wr_dec = m_axi_bvalid  & m_axi_bready;
  assign rd_inc = m_axi_arvalid & m_axi_arready;
  assign rd_dec = m_axi_rvalid  & m_axi_rready & m_axi_rlast;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wr_outst_q <= '0;
      rd_outst_q <= '0;
    end else begin
      case ({wr_inc, wr_dec})
        2'b10:   wr_outst_q <= wr_outst_q + 1'b1;
        2'b01:   wr_outst_q <= wr_outst_q - 1'b1;
        default: ;
      endcase
      case ({rd_inc, rd_dec})
        2'b10:   rd_outst_q <= rd_outst_q + 1'b1;
        2'b01:   rd_outst_q <= rd_outst_q - 1'b1;
        default: ;
      endcase
    end
  end

  // =================================================================
  // Write terminate: drop the burst, then answer with SLVERR
  // =================================================================
  logic            wterm_busy_q, wterm_wdone_q;
  logic [ID_W-1:0] wterm_id_q;
  logic            b_local;

  assign aw_term_ready = ~wterm_busy_q;
  assign aw_allow      = ~wterm_busy_q & ~wq_full & (wr_outst_q != '1);

  assign b_local       = wterm_busy_q & wterm_wdone_q & (wr_outst_q == '0);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wterm_busy_q  <= 1'b0;
      wterm_wdone_q <= 1'b0;
      wterm_id_q    <= '0;
    end else begin
      if (aw_term_valid && aw_term_ready) begin
        wterm_busy_q <= 1'b1;
        wterm_id_q   <= aw_term_pay[PAY_W-1 -: ID_W];
      end
      // The last beat of the dropped burst can be consumed in the
      // same cycle as the hand-off, so this is tracked on its own.
      if (wq_pop && wq_head_term) begin
        wterm_wdone_q <= 1'b1;
      end
      if (b_local && s_axi_bready) begin
        wterm_busy_q  <= 1'b0;
        wterm_wdone_q <= 1'b0;
      end
    end
  end

  assign s_axi_bvalid = b_local | m_axi_bvalid;
  assign s_axi_bid    = b_local ? wterm_id_q  : m_axi_bid;
  assign s_axi_bresp  = b_local ? sg_pkg::RESP_SLVERR : m_axi_bresp;
  assign m_axi_bready = ~b_local & s_axi_bready;

  // =================================================================
  // Read terminate: answer every beat of the burst with SLVERR
  // =================================================================
  logic            rterm_busy_q;
  logic [ID_W-1:0] rterm_id_q;
  logic [7:0]      rterm_cnt_q;      // beats still to send, minus one
  logic            r_local;

  assign ar_term_ready = ~rterm_busy_q;
  assign ar_allow      = ~rterm_busy_q & (rd_outst_q != '1);

  assign r_local       = rterm_busy_q & (rd_outst_q == '0);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rterm_busy_q <= 1'b0;
      rterm_id_q   <= '0;
      rterm_cnt_q  <= '0;
    end else begin
      if (ar_term_valid && ar_term_ready) begin
        rterm_busy_q <= 1'b1;
        rterm_id_q   <= ar_term_pay[PAY_W-1 -: ID_W];
        rterm_cnt_q  <= ar_term_pay[PAY_W-ID_W-1 -: 8];
      end else if (r_local && s_axi_rready) begin
        if (rterm_cnt_q == '0) rterm_busy_q <= 1'b0;
        else                   rterm_cnt_q  <= rterm_cnt_q - 1'b1;
      end
    end
  end

  assign s_axi_rvalid = r_local | m_axi_rvalid;
  assign s_axi_rid    = r_local ? rterm_id_q          : m_axi_rid;
  assign s_axi_rdata  = r_local ? '0                  : m_axi_rdata;
  assign s_axi_rresp  = r_local ? sg_pkg::RESP_SLVERR         : m_axi_rresp;
  assign s_axi_rlast  = r_local ? (rterm_cnt_q == '0) : m_axi_rlast;
  assign m_axi_rready = ~r_local & s_axi_rready;

  // The read decision strobe is not needed until fault reporting.
  logic unused;
  assign unused = &{1'b0, ar_dec_valid, ar_dec_fault};

  // =================================================================
  // Assertions (simulation only)
  // =================================================================
`ifdef SG_ASSERT
  sg_valid_stable #(.W(ADDR_W + PAY_W)) u_chk_m_aw (
    .clk, .rst_n, .valid(m_axi_awvalid), .ready(m_axi_awready),
    .payload({m_axi_awaddr, aw_m_pay}));
  sg_valid_stable #(.W(ADDR_W + PAY_W)) u_chk_m_ar (
    .clk, .rst_n, .valid(m_axi_arvalid), .ready(m_axi_arready),
    .payload({m_axi_araddr, ar_m_pay}));
  sg_valid_stable #(.W(DATA_W + DATA_W/8 + 1)) u_chk_m_w (
    .clk, .rst_n, .valid(m_axi_wvalid), .ready(m_axi_wready),
    .payload({m_axi_wdata, m_axi_wstrb, m_axi_wlast}));
  sg_valid_stable #(.W(ID_W + 2)) u_chk_s_b (
    .clk, .rst_n, .valid(s_axi_bvalid), .ready(s_axi_bready),
    .payload({s_axi_bid, s_axi_bresp}));
  sg_valid_stable #(.W(ID_W + DATA_W + 3)) u_chk_s_r (
    .clk, .rst_n, .valid(s_axi_rvalid), .ready(s_axi_rready),
    .payload({s_axi_rid, s_axi_rdata, s_axi_rresp, s_axi_rlast}));

  // A local error response must never coincide with a downstream one.
  a_b_no_clash: assert property (@(posedge clk) disable iff (!rst_n)
    !(b_local && m_axi_bvalid))
    else $error("local B response overlaps a downstream B response");
  a_r_no_clash: assert property (@(posedge clk) disable iff (!rst_n)
    !(r_local && m_axi_rvalid))
    else $error("local R response overlaps a downstream R response");

  // The W decision queue must never overflow or underflow.
  a_wq_no_overflow: assert property (@(posedge clk) disable iff (!rst_n)
    !(wq_push && wq_full && !wq_pop))
    else $error("W decision queue overflow");
`endif

endmodule
