"""Runs after every other test module: writes the functional coverage
report to build/coverage.md and fails if a bin was never hit."""

from pathlib import Path

import cocotb

from sg_coverage import cov


@cocotb.test()
async def coverage_closed(dut):
    report = Path(__file__).resolve().parent.parent / "build" / "coverage.md"
    report.parent.mkdir(exist_ok=True)
    report.write_text(cov.report())

    bins, hit = cov.totals
    dut._log.info("functional coverage: %d of %d bins hit, report in %s", hit, bins, report)
    holes = cov.holes()
    for point, bin_name in holes:
        dut._log.error("never hit: %s -> %s", point, bin_name)
    assert not holes, f"{len(holes)} coverage bins were never hit"
