import os
import tempfile
from pathlib import Path

import pytest

# Keep session/temp data out of the real /data volume while testing.
_TEST_DATA = Path(tempfile.mkdtemp(prefix="gsb-test-data-"))
os.environ.setdefault("DATA_DIR", str(_TEST_DATA))


@pytest.fixture()
def data_dir() -> Path:
    return _TEST_DATA
