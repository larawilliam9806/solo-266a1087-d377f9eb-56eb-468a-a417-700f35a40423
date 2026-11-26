"""Root pytest bootstrap.

Ensures the project root is importable so grader/test invocations work no
matter the current working directory.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
