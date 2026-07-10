from __future__ import annotations

import subprocess
import sys

if __name__ == "__main__":
    print("START")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_mt5_client.py", "tests/test_watcher.py", "-q"],
        capture_output=True,
        text=True,
    )
    print("STDOUT:")
    print(result.stdout)
    print("STDERR:")
    print(result.stderr)
    print("RC", result.returncode)
    sys.exit(result.returncode)
