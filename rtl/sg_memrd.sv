// StreamGuard - table memory read port.
//
// Small AXI4 read-only manager used to fetch stream table entries,
// context descriptors and translation table descriptors. Each request
// reads one or two 64-bit words starting at an 8-byte aligned address.
//
// Every read uses ID 0 and only one read is ever outstanding.

module sg_memrd #(
  parameter int unsigned ADDR_W = 48
) (
  input  logic              clk,
  input  logic              rst_n,

  // Request. req_valid starts a read when idle; req_addr and req_two
  // must then stay stable until done.
  input  logic              req_valid,
  input  logic [ADDR_W-1:0] req_addr,
  input  logic              req_two,      // read two words instead of one
  output logic              done,         // one cycle pulse
  output logic [63:0]       data0,
  output logic [63:0]       data1,
  output logic              err,          // any beat came back not OKAY

  // AXI4 read channels
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

  typedef enum logic [1:0] {M_IDLE, M_ADDR, M_DATA, M_DONE} state_e;
  state_e state_q;
  logic   beat_q;

  assign t_axi_arid    = 1'b0;
  assign t_axi_araddr  = req_addr;
  assign t_axi_arlen   = {7'b0, req_two};
  assign t_axi_arsize  = 3'b011;            // 8 bytes per beat
  assign t_axi_arburst = 2'b01;             // INCR
  assign t_axi_arprot  = 3'b011;            // privileged, non-secure, data
  assign t_axi_arvalid = (state_q == M_ADDR);
  assign t_axi_rready  = (state_q == M_DATA);

  assign done = (state_q == M_DONE);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q <= M_IDLE;
      beat_q  <= 1'b0;
      data0   <= '0;
      data1   <= '0;
      err     <= 1'b0;
    end else begin
      case (state_q)
        M_IDLE: begin
          if (req_valid) begin
            state_q <= M_ADDR;
            beat_q  <= 1'b0;
            err     <= 1'b0;
          end
        end

        M_ADDR: begin
          if (t_axi_arready) state_q <= M_DATA;
        end

        M_DATA: begin
          if (t_axi_rvalid) begin
            if (beat_q) data1 <= t_axi_rdata;
            else        data0 <= t_axi_rdata;
            if (t_axi_rresp != 2'b00) err <= 1'b1;
            beat_q <= 1'b1;
            if (t_axi_rlast) state_q <= M_DONE;
          end
        end

        M_DONE: state_q <= M_IDLE;

        default: state_q <= M_IDLE;
      endcase
    end
  end

  logic unused;
  assign unused = &{1'b0, t_axi_rid};

`ifdef FORMAL
  // AXI read manager rules, proved for every behaviour of the memory
  // (make formal).
  //
  // Assumed (the contract of this module's ports):
  //   * the requester keeps req_addr and req_two stable until done
  //   * the memory returns RLAST on the last beat of the burst
  logic        f_ar_done;       // address handshake happened
  logic [1:0]  f_beats;         // data beats taken
  logic [63:0] f_d0, f_d1;      // what the memory returned
  logic        f_err;
  logic        f_past_valid;

  initial f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  initial assume (!rst_n);

  always @(posedge clk) begin
    if (f_past_valid && state_q != M_IDLE) begin
      assume (req_addr == $past(req_addr));
      assume (req_two == $past(req_two));
    end
    if (t_axi_rvalid && t_axi_rready) assume (t_axi_rlast == (beat_q == req_two));
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      f_ar_done <= 1'b0;
      f_beats   <= '0;
      f_d0      <= '0;
      f_d1      <= '0;
      f_err     <= 1'b0;
    end else begin
      if (state_q == M_IDLE) begin
        f_ar_done <= 1'b0;
        f_beats   <= '0;
        f_err     <= 1'b0;
      end
      if (t_axi_arvalid && t_axi_arready) f_ar_done <= 1'b1;
      if (t_axi_rvalid && t_axi_rready) begin
        f_beats <= f_beats + 2'd1;
        if (f_beats == 2'd0) f_d0 <= t_axi_rdata;
        if (f_beats == 2'd1) f_d1 <= t_axi_rdata;
        if (t_axi_rresp != 2'b00) f_err <= 1'b1;
      end
    end
  end

  always @(posedge clk) begin
    if (rst_n) begin
      // the state machine and the record agree (needed for induction)
      if (state_q == M_ADDR) assert (!f_ar_done && f_beats == 2'd0 && !beat_q && !f_err && !err);
      if (state_q == M_DATA) begin
        assert (f_ar_done);
        assert (f_beats == {1'b0, beat_q});
        assert (f_beats <= {1'b0, req_two});
        assert (err == f_err);
        if (beat_q) assert (data0 == f_d0);
      end

      // 1. One address request per read, and data is only accepted
      //    after it.
      if (t_axi_arvalid) assert (!f_ar_done);
      if (t_axi_rready)  assert (f_ar_done);
      assert (t_axi_arlen == {7'b0, req_two});

      // 2. What is handed back is what the memory returned.
      if (done) begin
        assert (f_beats == {1'b0, req_two} + 2'd1);
        assert (data0 == f_d0);
        if (req_two) assert (data1 == f_d1);
        assert (err == f_err);
      end
    end

    // 3. Handshake rule on the address channel.
    if (f_past_valid && rst_n && $past(rst_n) && $past(t_axi_arvalid && !t_axi_arready)) begin
      assert (t_axi_arvalid);
      assert ($stable(t_axi_araddr) && $stable(t_axi_arlen));
    end
  end
`endif

endmodule
