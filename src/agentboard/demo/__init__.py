"""The bundled demo target: a tiny vitest project with one planted bug.

Single source of truth — the e2e gate tests and `agentboard demo` both run
against this exact directory, so the demo a stranger sees is the same code
CI proves deterministic on every push.
"""

import os

TARGET_DIR = os.path.join(os.path.dirname(__file__), "target")
