from __future__ import annotations

import sys

import pytest


if __name__ == "__main__":
    print("START")
    rc = pytest.main(["-q", "tests/test_mt5_client.py", "tests/test_watcher.py"])
    print("END", rc)
    sys.exit(rc)
