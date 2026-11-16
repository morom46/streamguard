// StreamGuard - shared constants.
//
// Register offsets and field positions follow the SMMUv3 register map
// (Arm IHI 0070, chapter 6). Only the subset StreamGuard implements is
// listed here; everything else reads as zero and ignores writes.

package sg_pkg;

  // ---------------------------------------------------------------
  // Register offsets, page 0. PADDR is 17 bits so that page 1
  // (EVENTQ_PROD/CONS at 0x100A8/0x100AC) is reachable later.
  // ---------------------------------------------------------------
  localparam logic [16:0] REG_IDR0   = 17'h00000;
  localparam logic [16:0] REG_IDR1   = 17'h00004;
  localparam logic [16:0] REG_IDR5   = 17'h00014;
  localparam logic [16:0] REG_CR0    = 17'h00020;
  localparam logic [16:0] REG_CR0ACK = 17'h00024;
  localparam logic [16:0] REG_GBPA   = 17'h00044;
  localparam logic [16:0] REG_STRTAB_BASE_LO  = 17'h00080;
  localparam logic [16:0] REG_STRTAB_BASE_HI  = 17'h00084;
  localparam logic [16:0] REG_STRTAB_BASE_CFG = 17'h00088;

  // CR0 / CR0ACK bit positions
  localparam int unsigned CR0_SMMUEN  = 0;
  localparam int unsigned CR0_EVENTQEN = 2;
  localparam int unsigned CR0_CMDQEN  = 3;
  localparam logic [31:0] CR0_WMASK   = 32'h0000_000D;

  // GBPA bit positions
  localparam int unsigned GBPA_ABORT  = 20;
  localparam int unsigned GBPA_UPDATE = 31;

  // ---------------------------------------------------------------
  // ID register values. These advertise what this implementation
  // supports so that a driver can probe it the way it would probe
  // any SMMUv3.
  // ---------------------------------------------------------------
  // IDR0: S1P=1 (stage 1 only), TTF=0b10 (AArch64 tables),
  //       TTENDIAN=0b10 (little endian), STALL_MODEL=0b01 (no stall),
  //       ST_LEVEL=0b00 (linear stream table), ASID16=0 (8-bit ASID)
  localparam logic [31:0] IDR0_VALUE = 32'h0140_000A;
  // IDR1: SIDSIZE=8, EVENTQS=4, CMDQS=4 (16-entry queues)
  localparam logic [31:0] IDR1_VALUE = 32'h0084_0008;
  // IDR5: OAS=0b101 (48-bit output address), GRAN4K=1
  localparam logic [31:0] IDR5_VALUE = 32'h0000_0015;

  // ---------------------------------------------------------------
  // Stream Table Entry, first 64-bit word (IHI 0070, chapter 5)
  // ---------------------------------------------------------------
  localparam int unsigned STE_V        = 0;
  localparam int unsigned STE_CFG_LSB  = 1;      // Config[2:0] at [3:1]
  localparam int unsigned STE_S1FMT_LSB = 4;     // S1Fmt[1:0] at [5:4]
  // S1ContextPtr is [51:6]; this design uses [47:6]

  localparam logic [2:0] STE_CFG_ABORT  = 3'b000;
  localparam logic [2:0] STE_CFG_BYPASS = 3'b100;
  localparam logic [2:0] STE_CFG_S1     = 3'b101;

  // ---------------------------------------------------------------
  // Context Descriptor, first 64-bit word. The second word holds
  // TTB0 in [51:4].
  // ---------------------------------------------------------------
  localparam int unsigned CD_T0SZ_LSB = 0;       // T0SZ[5:0]
  localparam int unsigned CD_TG0_LSB  = 6;       // TG0[1:0], 0b00 = 4 KB
  localparam int unsigned CD_EPD0     = 14;
  localparam int unsigned CD_ENDI     = 15;
  localparam int unsigned CD_V        = 31;
  localparam int unsigned CD_AFFD     = 35;
  localparam int unsigned CD_AA64     = 41;

  // ---------------------------------------------------------------
  // Translation table descriptor (VMSAv8-64, stage 1, 4 KB granule)
  // ---------------------------------------------------------------
  localparam int unsigned DESC_VALID = 0;
  localparam int unsigned DESC_TABLE = 1;        // table (L0-L2) or page (L3)
  localparam int unsigned DESC_AP1   = 6;        // 1 = unprivileged access allowed
  localparam int unsigned DESC_AP2   = 7;        // 1 = read-only
  localparam int unsigned DESC_AF    = 10;
  localparam int unsigned DESC_PXN   = 53;
  localparam int unsigned DESC_UXN   = 54;

  // ---------------------------------------------------------------
  // Fault codes. The values are the SMMUv3 event numbers (IHI 0070,
  // chapter 7), so the event queue can use them directly later.
  // FC_NONE with a fault means "terminated, no event recorded"
  // (global abort or STE.Config = abort).
  // ---------------------------------------------------------------
  localparam logic [7:0] FC_NONE           = 8'h00;
  localparam logic [7:0] FC_BAD_STREAMID   = 8'h02;
  localparam logic [7:0] FC_STE_FETCH      = 8'h03;
  localparam logic [7:0] FC_BAD_STE        = 8'h04;
  localparam logic [7:0] FC_CD_FETCH       = 8'h09;
  localparam logic [7:0] FC_BAD_CD         = 8'h0A;
  localparam logic [7:0] FC_WALK_EABT      = 8'h0B;
  localparam logic [7:0] FC_TRANSLATION    = 8'h10;
  localparam logic [7:0] FC_ADDR_SIZE      = 8'h11;
  localparam logic [7:0] FC_ACCESS         = 8'h12;
  localparam logic [7:0] FC_PERMISSION     = 8'h13;

  // AXI response codes
  localparam logic [1:0] RESP_OKAY   = 2'b00;
  localparam logic [1:0] RESP_SLVERR = 2'b10;

endpackage
