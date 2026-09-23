"""`python3 -m driveprep`. The CLI itself lives in driveprep.cli."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
