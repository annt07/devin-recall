"""`python -m recall ...`: same as the `recall` command (used by hooks installed from a dev checkout)."""
import sys

from recall.cli import main

sys.exit(main())
