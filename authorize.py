"""Compatibility entry point for existing SSH authorization instructions."""

import sys

from host_helper.cli import main

if __name__ == "__main__":
    main(["authorize", *sys.argv[1:]])
