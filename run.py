"""Compatibility entry point for existing cron jobs; prefer `host-helper sync`."""

import sys

from host_helper.cli import main

if __name__ == "__main__":
    main(["sync", *sys.argv[1:]])
