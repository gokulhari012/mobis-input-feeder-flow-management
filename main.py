"""MOBIS – Input Feeder Flow Management.

Usage:
    python main.py                      # normal start
    python main.py --simulate           # force PLC + scanner simulation (no hardware)
    python main.py --data-dir .\\data   # use a custom data directory
"""

import sys

from mobis.app.application import run

if __name__ == "__main__":
    sys.exit(run())
