"""Make the repo root importable so `import server` works from the tests."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
