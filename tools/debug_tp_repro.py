import pandas as pd
from swift_scanner.config import ScannerConfig
from swift_scanner.indicator import scan_swift


def make_frame(rows):
    frame = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


config = ScannerConfig(
    main_use_alternate_signals=False,
    main_basis_length=1,
    main_basis_type="ALMA",
    use_trend_filter=False,
)

data = make_frame(
    [
        ("2026-05-25 00:00:00+00:00", 79.0, 79.2, 78.8, 79.1, 1000),
        ("2026-05-25 00:15:00+00:00", 79.1, 79.4, 78.3, 78.5, 1000),
        ("2026-05-25 00:30:00+00:00", 78.5, 78.9, 78.2, 78.8, 1000),
    ]
)

result = scan_swift(data, "USOIL", "USOIL", "M15", config)
print("events", len(result.fresh_events))
for event in result.fresh_events:
    print("---")
    print("label:", event.signal_label)
    print("direction:", event.zone.direction)
    print("price:", event.price)
    print("entry:", event.entry_price)
    print("stop_loss:", event.stop_loss)
    print("tp1:", event.take_profit_1)
    print("tp2:", event.take_profit_2)
    print("tp3:", event.take_profit_3)
    print("rr:", event.risk_reward_ratio)
