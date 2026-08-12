"""Allow the CLI to be run as ``python -m pg_debug``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
