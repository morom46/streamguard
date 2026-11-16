#!/usr/bin/env bash
# Create .venv with the simulator, testbench libraries and synthesis tool.
#
# Needs: python3 (3.10+) with venv, make, g++, perl.
#   Ubuntu / WSL:  sudo apt install python3-venv build-essential perl
#
# Usage:  ./scripts/setup_tools.sh && source .venv/bin/activate

set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# The PyPI Verilator build keeps its scripts inside the Python package
# and does not report a version number. cocotb needs a `verilator`
# command on PATH and checks the version, so wrap it.
VROOT=$(python -c "import verilator, os; print(os.path.dirname(verilator.__file__))")
VVER=$(python -c "from importlib.metadata import version; a = version('verilator').split('.'); print(f'{a[0]}.{int(a[1]):03d}')")
chmod +x "$VROOT"/bin/*

cat > .venv/bin/verilator <<WRAP
#!/usr/bin/env bash
export VERILATOR_ROOT="$VROOT"
if [ "\${1:-}" = "--version" ]; then
  echo "Verilator $VVER (PyPI build)"
  exit 0
fi
exec perl "$VROOT/bin/verilator" "\$@"
WRAP
chmod +x .venv/bin/verilator

echo "Tools installed:"
verilator --version
python -c "import cocotb; print('cocotb', cocotb.__version__)"
echo "Run:  source .venv/bin/activate && make test"
