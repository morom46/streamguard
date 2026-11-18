// StreamGuard - event queue writer.
//
// Records a fault as a 32-byte event in the event queue: a circular
// buffer in memory that software reads. The record is written with one
// 4-beat AXI write at EVENTQ_BASE + index * 32, then EVENTQ_PROD is
// advanced.
//
//   word 0   [7:0] event number, [63:32] StreamID
//   word 1   for translation-related faults: [35] RnW (read),
//            [34] InD (instruction), [33] PnU (privileged)
//   word 2   for translation-related faults: the input address
//   word 3   zero
//
// Translation-related faults are F_TRANSLATION, F_ADDR_SIZE, F_ACCESS,
// F_PERMISSION and F_WALK_EABT. Configuration errors (C_BAD_STREAMID,
// C_BAD_STE, C_BAD_CD and the fetch aborts) only carry the event
// number and the StreamID.
//
// Nothing is written when the queue is disabled. When it is full the
// record is dropped and ovf_o tells the registers to raise the
// overflow flag. Either way the faulting transaction is terminated as
// usual: recording never changes the outcome of an access.

module sg_evtq #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8
) (
  input  logic              clk,
  input  logic              rst_n,

  // The fault to record. start_i is a one cycle pulse.
  input  logic              start_i,
  input  logic [7:0]        code_i,
  input  logic [SID_W-1:0]  sid_i,
  input  logic [ADDR_W-1:0] addr_i,
  input  logic              write_i,
  input  logic [2:0]        prot_i,
  output logic              done_o,           // one cycle pulse

  // Queue state from the registers
  input  logic              eventqen_i,
  input  logic              full_i,
  input  logic [47:5]       base_i,
  input  logic [3:0]        wr_idx_i,

  // Results for the registers, one cycle pulses
  output logic              prod_inc_o,
  output logic              ovf_o,
  output logic              abt_o,

  // AXI4 write channels
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

  typedef enum logic [2:0] {E_IDLE, E_AW, E_W, E_B, E_DONE} state_e;
  state_e            state_q;
  logic [1:0]        beat_q;
  logic [ADDR_W-1:0] awaddr_q;
  logic [3:0][63:0]  rec_q;

  // ---------------- record ----------------
  logic        xlat_class;
  logic [63:0] word0, word1, word2;

  assign xlat_class = (code_i == sg_pkg::FC_TRANSLATION) | (code_i == sg_pkg::FC_ADDR_SIZE) |
                      (code_i == sg_pkg::FC_ACCESS)      | (code_i == sg_pkg::FC_PERMISSION) |
                      (code_i == sg_pkg::FC_WALK_EABT);

  always_comb begin
    word0 = '0;
    word0[7:0]          = code_i;
    word0[32 +: SID_W]  = sid_i;

    word1 = '0;
    word2 = '0;
    if (xlat_class) begin
      word1[sg_pkg::EVT1_RNW] = ~write_i;
      word1[sg_pkg::EVT1_IND] = prot_i[2];
      word1[sg_pkg::EVT1_PNU] = prot_i[0];
      word2[ADDR_W-1:0]       = addr_i;
    end
  end

  // ---------------- control ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q    <= E_IDLE;
      beat_q     <= '0;
      awaddr_q   <= '0;
      prod_inc_o <= 1'b0;
      ovf_o      <= 1'b0;
      abt_o      <= 1'b0;
      rec_q      <= '0;
    end else begin
      prod_inc_o <= 1'b0;
      ovf_o      <= 1'b0;
      abt_o      <= 1'b0;

      case (state_q)
        E_IDLE: begin
          if (start_i) begin
            if (!eventqen_i) begin
              state_q <= E_DONE;                 // not recorded
            end else if (full_i) begin
              ovf_o   <= 1'b1;                   // record lost
              state_q <= E_DONE;
            end else begin
              rec_q[0] <= word0;
              rec_q[1] <= word1;
              rec_q[2] <= word2;
              rec_q[3] <= '0;
              awaddr_q <= {base_i, 5'b0} + {{(ADDR_W-9){1'b0}}, wr_idx_i, 5'b0};
              beat_q   <= '0;
              state_q  <= E_AW;
            end
          end
        end

        E_AW: if (t_axi_awready) state_q <= E_W;

        E_W: begin
          if (t_axi_wready) begin
            beat_q <= beat_q + 2'd1;
            if (beat_q == 2'd3) state_q <= E_B;
          end
        end

        E_B: begin
          if (t_axi_bvalid) begin
            if (t_axi_bresp == sg_pkg::RESP_OKAY) prod_inc_o <= 1'b1;
            else                                  abt_o      <= 1'b1;
            state_q <= E_DONE;
          end
        end

        E_DONE:  state_q <= E_IDLE;

        default: state_q <= E_IDLE;
      endcase
    end
  end

  assign done_o = (state_q == E_DONE);

  assign t_axi_awid    = 1'b0;
  assign t_axi_awaddr  = awaddr_q;
  assign t_axi_awlen   = 8'd3;              // four beats
  assign t_axi_awsize  = 3'b011;            // 8 bytes per beat
  assign t_axi_awburst = 2'b01;             // INCR
  assign t_axi_awprot  = 3'b011;            // privileged, non-secure, data
  assign t_axi_awvalid = (state_q == E_AW);

  assign t_axi_wdata   = rec_q[beat_q];
  assign t_axi_wstrb   = 8'hFF;
  assign t_axi_wlast   = (beat_q == 2'd3);
  assign t_axi_wvalid  = (state_q == E_W);

  assign t_axi_bready  = (state_q == E_B);

  logic unused;
  assign unused = &{1'b0, t_axi_bid, prot_i[1]};

endmodule
