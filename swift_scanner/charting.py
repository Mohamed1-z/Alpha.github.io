from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from swift_scanner.config import ScannerConfig
from swift_scanner.models import ScanResult, SignalEvent, Zone


SWIFT_STYLES = {
    "bullish": {
        "face": "#22c55e",
        "edge": "#15803d",
        "label": "SWIFT Long",
        "marker": "^",
        "text_factor": 1.002,
    },
    "bearish": {
        "face": "#ef4444",
        "edge": "#b91c1c",
        "label": "SWIFT Short",
        "marker": "v",
        "text_factor": 0.998,
    },
}


def sanitize_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


def _rectangle_patch(*args: Any, **kwargs: Any) -> Any:
    from matplotlib.patches import Rectangle

    return Rectangle(*args, **kwargs)


def draw_candles(ax: Any, frame) -> None:
    width = 0.62
    for offset, row in enumerate(frame.itertuples(index=False)):
        color = "#16a34a" if row.close >= row.open else "#dc2626"
        body_low = min(row.open, row.close)
        body_height = max(abs(row.close - row.open), 1e-9)
        ax.vlines(offset, row.low, row.high, color=color, linewidth=1.0, zorder=2)
        ax.add_patch(
            _rectangle_patch(
                (offset - width / 2, body_low),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.9,
                zorder=3,
            )
        )


def draw_swift_zone(
    ax: Any,
    zone: Zone,
    window_start: int,
    window_end: int,
    highlight: bool,
) -> None:
    if zone.right_index < window_start or zone.left_index > window_end:
        return

    style = SWIFT_STYLES[zone.direction]
    left = max(zone.left_index, window_start) - window_start - 0.5
    right = min(zone.right_index, window_end) - window_start + 0.5
    width = max(0.6, right - left)
    alpha = 0.28 if highlight else 0.18
    line_width = 2.0 if highlight else 1.2

    ax.add_patch(
        _rectangle_patch(
            (left, zone.bottom),
            width,
            zone.top - zone.bottom,
            facecolor=style["face"],
            edgecolor=style["edge"],
            alpha=alpha,
            linewidth=line_width,
            zorder=1,
        )
    )
    ax.hlines(
        y=zone.midpoint,
        xmin=left,
        xmax=right,
        colors="#64748b",
        linestyles="-",
        linewidth=1.0,
        zorder=4,
    )
    ax.text(
        right - 0.2,
        zone.midpoint,
        f'{style["label"]} | Mid {zone.midpoint:.5f}',
        ha="right",
        va="center",
        fontsize=8,
        color=style["edge"],
        bbox={"facecolor": "white", "alpha": 0.68, "edgecolor": "none", "pad": 1.5},
        zorder=5,
    )


def draw_trade_levels(ax: Any, event: SignalEvent, window_width: int) -> None:
    if event.stop_loss is None:
        return

    levels = [
        ("Entry", event.entry_price, "#2563eb"),
        ("TP1", event.take_profit_1, "#65a30d"),
        ("TP2", event.take_profit_2, "#16a34a"),
        ("TP3", event.take_profit_3 or event.take_profit, "#15803d"),
        ("SL", event.stop_loss, "#dc2626"),
    ]
    x_min = -0.5
    x_max = max(window_width - 5, 0.5)
    for label, price, color in levels:
        if price is None:
            continue
        ax.hlines(
            y=price,
            xmin=x_min,
            xmax=x_max,
            colors=color,
            linestyles="--",
            linewidth=1.4,
            zorder=4,
        )
        ax.text(
            x_max - 0.2,
            price,
            f"{label} {price:.5f}",
            ha="right",
            va="center",
            fontsize=8,
            color=color,
            bbox={"facecolor": "white", "alpha": 0.76, "edgecolor": "none", "pad": 1.5},
            zorder=7,
        )


def _format_strategy_note(event: SignalEvent) -> str:
    label_lower = (event.signal_label or "").lower()
    if "entry" in label_lower:
        return "Strategy: trade with the primary trend, use the SWIFT zone as the focus area, and keep risk tight."
    if "tp" in label_lower:
        return "Strategy: lock in profit and study how the prior entry structure developed."
    if "sl" in label_lower:
        return "Strategy: protect capital and learn from a failed setup."
    if event.market_change_alert:
        return "Strategy: note the direction change and wait for a clean re-entry aligned with trend."
    return "Strategy: study the zone, candle action, and risk placement for future trades."


def format_signal_explanation(event: SignalEvent) -> str:
    note = _format_strategy_note(event)
    lesson = event.lesson.strip() if event.lesson else ""
    if event.explanation and lesson:
        return f"{event.explanation} | {lesson} | {note}"
    if event.explanation:
        return f"{event.explanation} | {note}"

    direction = event.zone.direction.title()
    label = event.signal_label or event.zone.direction.title()
    is_entry = "entry" in label.lower()
    action = "Entry signal" if is_entry else "Signal"
    change_note = "Market change alert. " if event.market_change_alert else ""
    rr = f"RR {event.risk_reward_ratio:.2f}" if event.risk_reward_ratio is not None else ""
    tp_label = "TP3"
    tp_value = event.take_profit_3 if event.take_profit_3 is not None else event.take_profit
    if "tp1" in label.lower():
        tp_label = "TP1"
        tp_value = event.take_profit_1
    elif "tp2" in label.lower():
        tp_label = "TP2"
        tp_value = event.take_profit_2
    elif "tp3" in label.lower():
        tp_label = "TP3"
        tp_value = event.take_profit_3

    parts = [
        f"{change_note}{action}: {label}",
        f"Direction: {direction}",
    ]
    if event.entry_price is not None:
        parts.append(f"Entry {event.entry_price:.5f}")
    if event.stop_loss is not None:
        parts.append(f"SL {event.stop_loss:.5f}")
    if tp_value is not None:
        parts.append(f"{tp_label} {tp_value:.5f}")
    if rr:
        parts.append(rr)
    if lesson:
        parts.append(lesson)
    parts.append(note)
    return " | ".join(parts)


def generate_timeframe_example_image(output_dir: Path) -> Path:
    import matplotlib.pyplot as plt
    from datetime import datetime, timedelta
    import numpy as np

    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow()
    m5_times = [now - timedelta(minutes=5 * (23 - i)) for i in range(24)]
    m15_times = [now - timedelta(minutes=15 * (15 - i)) for i in range(16)]
    m5_prices = np.array([100.0 + (i * 0.26) + np.sin(i * 0.45) for i in range(len(m5_times))])
    m15_prices = np.array([100.0 + (i * 0.42) + np.sin(i * 0.32) for i in range(len(m15_times))])

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=False)
    for ax, times, prices, label in (
        (axes[0], m5_times, m5_prices, "M5"),
        (axes[1], m15_times, m15_prices, "M15"),
    ):
        x = list(range(len(prices)))
        ax.plot(x, prices, color="#0f172a", linewidth=2)
        ax.fill_between(x, prices, prices - 0.5, color="#22c55e", alpha=0.25)
        ax.scatter(x[::3], prices[::3], color="#0f172a", edgecolor="#ffffff", zorder=4)
        xticks = np.linspace(0, len(x) - 1, num=min(8, len(x)), dtype=int)
        ax.set_xticks(xticks)
        ax.set_xticklabels([times[i].strftime("%H:%M") for i in xticks], rotation=25)
        ax.set_title(f"Example {label} timeframe chart", fontsize=12, fontweight="bold")
        ax.set_ylabel("Price")
        ax.grid(alpha=0.18)
        ax.text(
            0.02,
            0.92,
            f"{label} series",
            transform=ax.transAxes,
            fontsize=10,
            bbox={"facecolor": "white", "alpha": 0.7, "edgecolor": "none", "pad": 3},
        )

    fig.suptitle("SWIFT Timeframe Example", fontsize=16, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    image_path = output_dir / "example_timeframes.png"
    fig.savefig(image_path, dpi=160)
    plt.close(fig)
    return image_path


def save_signal_chart(
    result: ScanResult,
    event: SignalEvent,
    config: ScannerConfig,
) -> Path:
    import matplotlib.pyplot as plt

    bars = result.bars
    window_start = max(0, len(bars) - config.chart_bars)
    window_end = len(bars) - 1
    frame = bars.iloc[window_start:].reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(14, 8))
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#ffffff")

    draw_candles(ax, frame)

    drawn_keys: set[str] = set()
    for zone in result.zones:
        draw_swift_zone(ax, zone, window_start, window_end, highlight=zone.key == event.zone.key)
        drawn_keys.add(zone.key)
    if event.zone.key not in drawn_keys:
        draw_swift_zone(ax, event.zone, window_start, window_end, highlight=True)

    event_x_index = event.zone.formed_index
    if event_x_index is None or event_x_index < window_start or event_x_index > window_end:
        event_x_index = event.zone.right_index
    event_x = event_x_index - window_start
    event_style = SWIFT_STYLES[event.zone.direction]
    ax.scatter(
        [event_x],
        [event.price],
        s=110,
        color=event_style["edge"],
        marker=event_style["marker"],
        zorder=6,
    )
    ax.annotate(
        event.signal_label or f'{event_style["label"]} signal',
        xy=(event_x, event.price),
        xytext=(event_x, event.price * float(event_style["text_factor"])),
        fontsize=10,
        color=event_style["edge"],
        arrowprops={"arrowstyle": "->", "color": event_style["edge"], "lw": 1.2},
        zorder=7,
    )
    draw_trade_levels(ax, event, len(frame))

    risk_text = ""
    if event.stop_loss is not None and event.take_profit is not None:
        rr = event.risk_reward_ratio if event.risk_reward_ratio is not None else 0
        tp_label = "TP3"
        tp_value = event.take_profit
        label_lower = (event.signal_label or "").lower()
        if "tp1" in label_lower:
            tp_label = "TP1"
            tp_value = event.take_profit_1
        elif "tp2" in label_lower:
            tp_label = "TP2"
            tp_value = event.take_profit_2
        elif "tp3" in label_lower:
            tp_label = "TP3"
            tp_value = event.take_profit_3
        elif event.take_profit_3 is not None:
            tp_label = "TP3"
            tp_value = event.take_profit_3
        risk_text = (
            f" | SL {event.stop_loss:,.5f} | {tp_label} {tp_value:,.5f} | RR {rr:.2f}"
        )
    title = (
        f"{result.symbol_request} ({result.symbol_actual}) | {result.timeframe} | "
        f"{event.signal_label or event_style['label']} | Close {result.latest_close:,.5f}"
        f"{risk_text}"
    )
    ax.set_title(title, fontsize=14, fontweight="bold", pad=16)
    explanation = format_signal_explanation(event)
    ax.text(
        0.01,
        0.96,
        explanation,
        transform=ax.transAxes,
        fontsize=9,
        color="#0f172a",
        va="top",
        ha="left",
        bbox={"facecolor": "#f8fafc", "alpha": 0.82, "edgecolor": "#94a3b8", "boxstyle": "round,pad=0.25"},
        zorder=10,
    )
    ax.grid(axis="y", alpha=0.12)
    ax.set_xlim(-1, len(frame))

    xticks = np.linspace(0, len(frame) - 1, num=min(8, len(frame)), dtype=int)
    labels = [frame.iloc[int(tick)]["time"].strftime("%m-%d %H:%M") for tick in xticks]
    ax.set_xticks(xticks)
    ax.set_xticklabels(labels, rotation=25, ha="right")
    ax.set_ylabel("Price")

    out_dir = config.output_dir / event.triggered_at.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{sanitize_name(event.symbol_request)}_"
        f"{sanitize_name(event.timeframe)}_"
        f"{sanitize_name(event.signal_source)}_"
        f"{sanitize_name(event.zone.direction)}_"
        f"{event.triggered_at.strftime('%Y%m%d_%H%M%S')}.png"
    )
    path = out_dir / filename
    fig.subplots_adjust(left=0.08, right=0.95, top=0.93, bottom=0.10)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path
