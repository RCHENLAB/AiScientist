"""The model-callable domain tools (the future AiScientist-tools package).

The platform reaches this package only through :mod:`aiscientist.tools.sdk` (the tool contract) and
:mod:`aiscientist.tools.api` (everything else it needs), and the tools import nothing from the
platform; ``tests/test_repo_boundaries.py`` holds both directions. Keep this file free of imports:
the gateway imports it at start-up and the HPC3 job images import it inside their containers.
"""
