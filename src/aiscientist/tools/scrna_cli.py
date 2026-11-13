"""Compatibility module for already-staged platform jobs; scientific dispatch is retired."""
from .worker_cli import (ADOPT_RESULT_DATASET, EXPORT_RESULT_DATASET, INSTALL_DEPENDENCY,
                         RESULT_MARKER, main, run_tool)

__all__ = ["ADOPT_RESULT_DATASET", "EXPORT_RESULT_DATASET", "INSTALL_DEPENDENCY", "RESULT_MARKER", "main", "run_tool"]

if __name__ == "__main__":
    raise SystemExit(main())
