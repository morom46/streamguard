// StreamGuard - top level.
//
// A stream-aware I/O address translation unit built from a subset of
// the public Arm SMMUv3 architecture (IHI 0070).
//
//   s_axi_*  AXI4 from the device whose accesses are being policed
//   m_axi_*  AXI4 towards the memory system
//   t_axi_*  AXI4 read channels used to fetch stream table entries,
//            context descriptors and translation tables
//   s_apb_*  APB4 programming interface
//
// See docs/architecture.md for the block diagram and milestone plan.

module streamguard #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned DATA_W = 64,
  parameter int unsigned ID_W   = 4,
  parameter int unsigned SID_W  = 8,
  parameter int unsigned TLB_ENTRIES = 8,
  parameter int unsigned WC_ENTRIES  = 4,
  parameter int unsigned CFG_ENTRIES = 4
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
  input  logic [SID_W-1:0]    s_axi_awuser,
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
  input  logic [SID_W-1:0]    s_axi_aruser,
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


  // ---------------- table memory read port ----------------
  output logic                t_axi_arid,
  output logic [ADDR_W-1:0]   t_axi_araddr,
  output logic [7:0]          t_axi_arlen,
  output logic [2:0]          t_axi_arsize,
  output logic [1:0]          t_axi_arburst,
  output logic [2:0]          t_axi_arprot,
  output logic                t_axi_arvalid,
  input  logic                t_axi_arready,
  input  logic                t_axi_rid,
  input  logic [63:0]         t_axi_rdata,
  input  logic [1:0]          t_axi_rresp,
  input  logic                t_axi_rlast,
  input  logic                t_axi_rvalid,
  output logic                t_axi_rready,

  // ---------------- APB4 programming interface ----------------
  input  logic [16:0]         s_apb_paddr,
  input  logic                s_apb_psel,
  input  logic                s_apb_penable,
  input  logic                s_apb_pwrite,
  input  logic [31:0]         s_apb_pwdata,
  input  logic [3:0]          s_apb_pstrb,
  output logic                s_apb_pready,
  output logic [31:0]         s_apb_prdata,
  output logic                s_apb_pslverr
);

  logic              smmuen, cmdqen, eventqen, gbpa_abort;

  logic [47:6]       strtab_base;
  logic [5:0]        strtab_log2size;

  logic              cache_dis, inv_all, cnt_clr;
  logic [31:0]       cnt_tlb_hit, cnt_tlb_miss, cnt_wc_hit, cnt_cfg_hit, cnt_cfg_miss;

  logic              x_req_valid, x_req_ready, x_req_write;
  logic [2:0]        x_req_prot;
  logic [7:0]        x_rsp_fcode;
  logic [1:0]        x_rsp_flevel;
  logic [ADDR_W-1:0] x_req_addr;
  logic [SID_W-1:0]  x_req_sid;
  logic              x_rsp_valid, x_rsp_fault;
  logic [ADDR_W-1:0] x_rsp_addr;

  sg_apb_regs u_regs (
    .clk, .rst_n,
    .paddr        (s_apb_paddr),
    .psel         (s_apb_psel),
    .penable      (s_apb_penable),
    .pwrite       (s_apb_pwrite),
    .pwdata       (s_apb_pwdata),
    .pstrb        (s_apb_pstrb),
    .pready       (s_apb_pready),
    .prdata       (s_apb_prdata),
    .pslverr      (s_apb_pslverr),
    .smmuen_o     (smmuen),
    .cmdqen_o     (cmdqen),
    .eventqen_o   (eventqen),
    .gbpa_abort_o (gbpa_abort),
    .strtab_base_o     (strtab_base),
    .strtab_log2size_o (strtab_log2size),
    .cache_dis_o       (cache_dis),
    .inv_all_o         (inv_all),
    .cnt_clr_o         (cnt_clr),
    .cnt_tlb_hit_i     (cnt_tlb_hit),
    .cnt_tlb_miss_i    (cnt_tlb_miss),
    .cnt_wc_hit_i      (cnt_wc_hit),
    .cnt_cfg_hit_i     (cnt_cfg_hit),
    .cnt_cfg_miss_i    (cnt_cfg_miss)
  );

  sg_tbu #(
    .ADDR_W(ADDR_W), .DATA_W(DATA_W), .ID_W(ID_W), .SID_W(SID_W)
  ) u_tbu (
    .clk, .rst_n,
    .s_axi_awid    (s_axi_awid),
    .s_axi_awaddr  (s_axi_awaddr),
    .s_axi_awlen   (s_axi_awlen),
    .s_axi_awsize  (s_axi_awsize),
    .s_axi_awburst (s_axi_awburst),
    .s_axi_awlock  (s_axi_awlock),
    .s_axi_awcache (s_axi_awcache),
    .s_axi_awprot  (s_axi_awprot),
    .s_axi_awqos   (s_axi_awqos),
    .s_axi_awuser   (s_axi_awuser),
    .s_axi_awvalid (s_axi_awvalid),
    .s_axi_awready (s_axi_awready),
    .s_axi_wdata   (s_axi_wdata),
    .s_axi_wstrb   (s_axi_wstrb),
    .s_axi_wlast   (s_axi_wlast),
    .s_axi_wvalid  (s_axi_wvalid),
    .s_axi_wready  (s_axi_wready),
    .s_axi_bid     (s_axi_bid),
    .s_axi_bresp   (s_axi_bresp),
    .s_axi_bvalid  (s_axi_bvalid),
    .s_axi_bready  (s_axi_bready),
    .s_axi_arid    (s_axi_arid),
    .s_axi_araddr  (s_axi_araddr),
    .s_axi_arlen   (s_axi_arlen),
    .s_axi_arsize  (s_axi_arsize),
    .s_axi_arburst (s_axi_arburst),
    .s_axi_arlock  (s_axi_arlock),
    .s_axi_arcache (s_axi_arcache),
    .s_axi_arprot  (s_axi_arprot),
    .s_axi_arqos   (s_axi_arqos),
    .s_axi_aruser   (s_axi_aruser),
    .s_axi_arvalid (s_axi_arvalid),
    .s_axi_arready (s_axi_arready),
    .s_axi_rid     (s_axi_rid),
    .s_axi_rdata   (s_axi_rdata),
    .s_axi_rresp   (s_axi_rresp),
    .s_axi_rlast   (s_axi_rlast),
    .s_axi_rvalid  (s_axi_rvalid),
    .s_axi_rready  (s_axi_rready),
    .m_axi_awid    (m_axi_awid),
    .m_axi_awaddr  (m_axi_awaddr),
    .m_axi_awlen   (m_axi_awlen),
    .m_axi_awsize  (m_axi_awsize),
    .m_axi_awburst (m_axi_awburst),
    .m_axi_awlock  (m_axi_awlock),
    .m_axi_awcache (m_axi_awcache),
    .m_axi_awprot  (m_axi_awprot),
    .m_axi_awqos   (m_axi_awqos),
    .m_axi_awvalid (m_axi_awvalid),
    .m_axi_awready (m_axi_awready),
    .m_axi_wdata   (m_axi_wdata),
    .m_axi_wstrb   (m_axi_wstrb),
    .m_axi_wlast   (m_axi_wlast),
    .m_axi_wvalid  (m_axi_wvalid),
    .m_axi_wready  (m_axi_wready),
    .m_axi_bid     (m_axi_bid),
    .m_axi_bresp   (m_axi_bresp),
    .m_axi_bvalid  (m_axi_bvalid),
    .m_axi_bready  (m_axi_bready),
    .m_axi_arid    (m_axi_arid),
    .m_axi_araddr  (m_axi_araddr),
    .m_axi_arlen   (m_axi_arlen),
    .m_axi_arsize  (m_axi_arsize),
    .m_axi_arburst (m_axi_arburst),
    .m_axi_arlock  (m_axi_arlock),
    .m_axi_arcache (m_axi_arcache),
    .m_axi_arprot  (m_axi_arprot),
    .m_axi_arqos   (m_axi_arqos),
    .m_axi_arvalid (m_axi_arvalid),
    .m_axi_arready (m_axi_arready),
    .m_axi_rid     (m_axi_rid),
    .m_axi_rdata   (m_axi_rdata),
    .m_axi_rresp   (m_axi_rresp),
    .m_axi_rlast   (m_axi_rlast),
    .m_axi_rvalid  (m_axi_rvalid),
    .m_axi_rready  (m_axi_rready),
    .x_req_valid, .x_req_ready, .x_req_addr, .x_req_sid, .x_req_write,
    .x_req_prot,
    .x_rsp_valid, .x_rsp_fault, .x_rsp_addr
  );

  sg_xlate #(
    .ADDR_W(ADDR_W), .SID_W(SID_W),
    .TLB_ENTRIES(TLB_ENTRIES), .WC_ENTRIES(WC_ENTRIES), .CFG_ENTRIES(CFG_ENTRIES)
  ) u_xlate (
    .clk, .rst_n,
    .smmuen_i     (smmuen),
    .gbpa_abort_i (gbpa_abort),
    .strtab_base_i     (strtab_base),
    .strtab_log2size_i (strtab_log2size),
    .cache_dis_i       (cache_dis),
    .inv_i             (inv_all),
    .cnt_clr_i         (cnt_clr),
    .cnt_tlb_hit_o     (cnt_tlb_hit),
    .cnt_tlb_miss_o    (cnt_tlb_miss),
    .cnt_wc_hit_o      (cnt_wc_hit),
    .cnt_cfg_hit_o     (cnt_cfg_hit),
    .cnt_cfg_miss_o    (cnt_cfg_miss),
    .req_valid    (x_req_valid),
    .req_ready    (x_req_ready),
    .req_addr     (x_req_addr),
    .req_sid      (x_req_sid),
    .req_write    (x_req_write),
    .req_prot     (x_req_prot),
    .rsp_valid    (x_rsp_valid),
    .rsp_fault    (x_rsp_fault),
    .rsp_addr     (x_rsp_addr),
    .rsp_fcode    (x_rsp_fcode),
    .rsp_flevel   (x_rsp_flevel),
    .t_axi_arid, .t_axi_araddr, .t_axi_arlen, .t_axi_arsize, .t_axi_arburst,
    .t_axi_arprot, .t_axi_arvalid, .t_axi_arready,
    .t_axi_rid, .t_axi_rdata, .t_axi_rresp, .t_axi_rlast, .t_axi_rvalid, .t_axi_rready
  );

  // Queue enables and the fault report are used when the queues arrive.
  logic unused;
  assign unused = &{1'b0, cmdqen, eventqen, x_rsp_fcode, x_rsp_flevel};

endmodule
