// StreamGuard - APB4 programming interface.
//
// Zero wait state APB4 completer holding the software-visible
// registers. Unimplemented offsets are RAZ/WI, so PSLVERR is never
// raised.
//
// Milestone 1 implements the ID registers, CR0/CR0ACK and GBPA.

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
  output logic        gbpa_abort_o
);

  logic [31:0] cr0_q, cr0ack_q;
  logic        gbpa_abort_q, gbpa_update_q;

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

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      cr0_q         <= '0;
      cr0ack_q      <= '0;
      gbpa_abort_q  <= 1'b0;   // reset state: bypass, not abort
      gbpa_update_q <= 1'b0;
    end else begin
      // CR0ACK follows CR0 one cycle later. Software is expected to
      // poll CR0ACK before relying on a CR0 change.
      cr0ack_q      <= cr0_q;
      gbpa_update_q <= 1'b0;

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
          default: ;
        endcase
      end
    end
  end

  always_comb begin
    case (addr)
      sg_pkg::REG_IDR0:   prdata = sg_pkg::IDR0_VALUE;
      sg_pkg::REG_IDR1:   prdata = sg_pkg::IDR1_VALUE;
      sg_pkg::REG_IDR5:   prdata = sg_pkg::IDR5_VALUE;
      sg_pkg::REG_CR0:    prdata = cr0_q;
      sg_pkg::REG_CR0ACK: prdata = cr0ack_q;
      sg_pkg::REG_GBPA:   prdata = gbpa_rd;
      default:    prdata = '0;
    endcase
  end

  // The datapath acts on the acknowledged value, so what software
  // reads in CR0ACK is what the hardware is doing.
  assign smmuen_o     = cr0ack_q[sg_pkg::CR0_SMMUEN];
  assign cmdqen_o     = cr0ack_q[sg_pkg::CR0_CMDQEN];
  assign eventqen_o   = cr0ack_q[sg_pkg::CR0_EVENTQEN];
  assign gbpa_abort_o = gbpa_abort_q;

endmodule
