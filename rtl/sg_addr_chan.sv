// StreamGuard - address channel handler.
//
// One instance sits on AW and one on AR. It takes a single address
// request from the upstream device, asks for a translation decision,
// and then either forwards the request downstream with the output
// address or hands it to the terminate logic.
//
// One request is in flight at a time. That keeps the control simple
// and correct first; a fast path for TLB hits is a later optimisation.

module sg_addr_chan #(
  parameter int unsigned ADDR_W = 48,
  parameter int unsigned SID_W  = 8,
  parameter int unsigned PAY_W  = 1    // everything except the address
) (
  input  logic              clk,
  input  logic              rst_n,

  // New requests are only accepted while this is high
  input  logic              allow_i,

  // Upstream (device side)
  input  logic              s_valid,
  output logic              s_ready,
  input  logic [ADDR_W-1:0] s_addr,
  input  logic [SID_W-1:0]  s_sid,
  input  logic [PAY_W-1:0]  s_pay,

  // Translation request / response
  output logic              x_req_valid,
  input  logic              x_req_ready,
  output logic [ADDR_W-1:0] x_req_addr,
  output logic [SID_W-1:0]  x_req_sid,
  input  logic              x_rsp_valid,
  input  logic              x_rsp_fault,
  input  logic [ADDR_W-1:0] x_rsp_addr,

  // Decision strobe, one cycle per request
  output logic              dec_valid_o,
  output logic              dec_fault_o,

  // Downstream (memory side)
  output logic              m_valid,
  input  logic              m_ready,
  output logic [ADDR_W-1:0] m_addr,
  output logic [PAY_W-1:0]  m_pay,

  // Hand-off of a terminated request
  output logic              term_valid,
  input  logic              term_ready,
  output logic [PAY_W-1:0]  term_pay
);

  typedef enum logic [2:0] {
    S_IDLE,    // waiting for a request
    S_XREQ,    // translation request pending
    S_XWAIT,   // waiting for the decision
    S_FWD,     // presenting the request downstream
    S_TERM     // handing the request to the terminate logic
  } state_e;

  state_e            state_q;
  logic [ADDR_W-1:0] addr_q;
  logic [SID_W-1:0]  sid_q;
  logic [PAY_W-1:0]  pay_q;

  assign s_ready     = (state_q == S_IDLE) & allow_i;

  assign x_req_valid = (state_q == S_XREQ);
  assign x_req_addr  = addr_q;
  assign x_req_sid   = sid_q;

  assign dec_valid_o = (state_q == S_XWAIT) & x_rsp_valid;
  assign dec_fault_o = x_rsp_fault;

  assign m_valid     = (state_q == S_FWD);
  assign m_addr      = addr_q;
  assign m_pay       = pay_q;

  assign term_valid  = (state_q == S_TERM);
  assign term_pay    = pay_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state_q <= S_IDLE;
      addr_q  <= '0;
      sid_q   <= '0;
      pay_q   <= '0;
    end else begin
      case (state_q)
        S_IDLE: begin
          if (s_valid && s_ready) begin
            addr_q  <= s_addr;
            sid_q   <= s_sid;
            pay_q   <= s_pay;
            state_q <= S_XREQ;
          end
        end

        S_XREQ: begin
          if (x_req_ready) state_q <= S_XWAIT;
        end

        S_XWAIT: begin
          if (x_rsp_valid) begin
            if (x_rsp_fault) begin
              state_q <= S_TERM;
            end else begin
              addr_q  <= x_rsp_addr;
              state_q <= S_FWD;
            end
          end
        end

        S_FWD: begin
          if (m_ready) state_q <= S_IDLE;
        end

        S_TERM: begin
          if (term_ready) state_q <= S_IDLE;
        end

        default: state_q <= S_IDLE;
      endcase
    end
  end

`ifdef FORMAL
  // Proved for every behaviour of the device, the translation logic
  // and the downstream side (make formal). The proof keeps its own
  // record of the request in flight and of the decision given for it.
  logic              f_busy;        // a request is in flight
  logic              f_sent;        // ... its translation request was taken
  logic              f_pass;        // ... the decision was "forward"
  logic              f_fault;       // ... the decision was "terminate"
  logic [ADDR_W-1:0] f_saddr;       // address the device gave
  logic [SID_W-1:0]  f_sid;
  logic [PAY_W-1:0]  f_pay;
  logic [ADDR_W-1:0] f_oaddr;       // address the translation gave
  logic              f_past_valid;

  initial f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  initial assume (!rst_n);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      f_busy  <= 1'b0;
      f_sent  <= 1'b0;
      f_pass  <= 1'b0;
      f_fault <= 1'b0;
      f_saddr <= '0;
      f_sid   <= '0;
      f_pay   <= '0;
      f_oaddr <= '0;
    end else begin
      if (s_valid && s_ready) begin
        f_busy  <= 1'b1;
        f_pass  <= 1'b0;
        f_fault <= 1'b0;
        f_saddr <= s_addr;
        f_sid   <= s_sid;
        f_pay   <= s_pay;
      end
      if (x_req_valid && x_req_ready) f_sent <= 1'b1;
      // the decision is the first response after the request was taken
      if (f_sent && x_rsp_valid) begin
        f_sent  <= 1'b0;
        f_pass  <= ~x_rsp_fault;
        f_fault <= x_rsp_fault;
        f_oaddr <= x_rsp_addr;
      end
      if ((m_valid && m_ready) || (term_valid && term_ready)) f_busy <= 1'b0;
    end
  end

  always @(posedge clk) begin
    if (rst_n) begin
      // The state machine and the record agree (needed for induction)
      assert (state_q <= S_TERM);
      assert ((state_q == S_IDLE) == !f_busy);
      assert ((state_q == S_XWAIT) == f_sent);
      assert ((state_q == S_FWD) == (f_busy && f_pass));
      assert ((state_q == S_TERM) == (f_busy && f_fault));
      assert (!(f_pass && f_fault));
      if (state_q == S_XREQ || state_q == S_XWAIT) begin
        assert (addr_q == f_saddr);
      end
      if (f_busy) begin
        assert (pay_q == f_pay);
        assert (sid_q == f_sid);
      end

      // 1. Nothing goes downstream without a "forward" decision, and
      //    then only with the translated address and the attributes
      //    the device gave.
      if (m_valid) begin
        assert (f_pass);
        assert (m_addr == f_oaddr);
        assert (m_pay == f_pay);
      end
      // 2. A request is terminated only after a "terminate" decision.
      if (term_valid) begin
        assert (f_fault);
        assert (term_pay == f_pay);
      end
      assert (!(m_valid && term_valid));
      // 3. One request at a time.
      if (f_busy) assert (!s_ready);
      // 4. The translation is asked about the address and StreamID the
      //    device gave.
      if (x_req_valid) begin
        assert (x_req_addr == f_saddr);
        assert (x_req_sid == f_sid);
      end
    end

    // 5. Handshake rule: VALID stays up with a stable payload until READY.
    if (f_past_valid && rst_n && $past(rst_n)) begin
      if ($past(m_valid && !m_ready)) begin
        assert (m_valid);
        assert ($stable(m_addr) && $stable(m_pay));
      end
      if ($past(x_req_valid && !x_req_ready)) begin
        assert (x_req_valid);
        assert ($stable(x_req_addr) && $stable(x_req_sid));
      end
      if ($past(term_valid && !term_ready)) begin
        assert (term_valid);
        assert ($stable(term_pay));
      end
    end
  end
`endif

endmodule
