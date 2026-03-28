"""`python -m gotta` shim."""

from __future__ import annotations

from gotta.cli.entry import main


if __name__ == "__main__":
    raise SystemExit(main())
