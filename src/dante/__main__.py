"""`python -m dante` -- same as the `dante` command."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
