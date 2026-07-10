from __future__ import annotations

import pandas as pd

from swift_scanner.config import ScannerConfig
from swift_scanner.indicator import scan_swift


def make_frame(rows):
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


if __name__ == "__main__":
    data = make_frame(
        [
            ("2026-05-25 00:00:00+00:00", 10.0, 10.1, 9.7, 9.8, 1000),
            ("2026-05-25 00:15:00+00:00", 10.0, 10.25, 9.9, 10.2, 1000),
            ("2026-05-25 00:30:00+00:00", 10.0, 10.4, 9.7, 9.8, 1000),
            ("2026-05-25 00:45:00+00:00", 10.0, 10.35, 10.0, 10.3, 1000),
        ]
    )
    config = ScannerConfig(
        main_use_alternate_signals=False,
        main_basis_length=1,
    )
    result = scan_swift(data, "TEST", "TEST", "M15", config)
    for event in result.fresh_events:
        print(event.to_dict())
        if event.lesson:
            print(f"Lesson text: {event.lesson}")
