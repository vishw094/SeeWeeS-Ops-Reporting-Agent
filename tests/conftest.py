from __future__ import annotations

import os
import sys

# Put src/ on the import path so tests can `import tools.dq_tools` etc.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
