"""Package entry point so ``python -m fctrace`` works.

``python -m fctrace.cli`` remains valid; both dispatch to the same
``cli.main()``, as does the ``fctrace`` console script declared in
pyproject.toml.
"""
import sys

from fctrace.cli import main

if __name__ == '__main__':
    sys.exit(main())
