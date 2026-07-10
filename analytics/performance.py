"""
analytics/performance.py

Generates a performance report from backtest trade logs (CSV files produced
by backtest/engine.py) or from your live alert_history.jsonl.

Computes win rate, profit factor, Sharpe-style consistency ratio, max
drawdown, and expectancy — the standard metrics professional trading desks
use to evaluate a system before increasing size or going live.

Usage:
    python analytics/performance.py --csv backtest/results/XAUUSD_M15_trades.csv
    python analytics/performance.py --history signals/alert_history.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd


def load_trades_from_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "pnl_r" not in df.columns:
        raise ValueError("CSV must contain a 'pnl_r' column (R-multiple outcome per trade).")
    return df


def load_trades_from_history(path: str | Path) -> pd.DataFrame:
    """Parse alert_history.jsonl from the live system into a trade dataframe.
    Expects each line to be a JSON record; adapts to whatever fields are present."""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        raise ValueError(f"No valid records found in {path}")
    return pd.DataFrame(records)


def compute_metrics(df: pd.DataFrame, r_column: str = "pnl_r") -> dict:
    closed = df[df["outcome"] != "open"].copy() if "outcome" in df.columns else df.copy()
    if closed.empty:
        return {"error": "No closed trades to analyze."}

    r_values = closed[r_column].astype(float)
    wins = r_values[r_values > 0]
    losses = r_values[r_values <= 0]

    total_trades = len(r_values)
    win_rate = (len(wins) / total_trades * 100.0) if total_trades else 0.0
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    expectancy = (win_rate / 100.0) * avg_win + (1 - win_rate / 100.0) * avg_loss

    gains_sum = wins.sum() if len(wins) else 0.0
    losses_sum = abs(losses.sum()) if len(losses) else 0.0
    profit_factor = (gains_sum / losses_sum) if losses_sum > 0 else float("inf") if gains_sum > 0 else 0.0

    cumulative = r_values.cumsum()
    peak = cumulative.cummax()
    drawdown = peak - cumulative
    max_drawdown = drawdown.max() if not drawdown.empty else 0.0

    std_r = r_values.std()
    sharpe_like = (r_values.mean() / std_r) if std_r and not math.isnan(std_r) and std_r > 0 else 0.0

    streak = 0
    max_streak = 0
    for r in r_values:
        if r <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2),
        "avg_win_r": round(float(avg_win), 3),
        "avg_loss_r": round(float(avg_loss), 3),
        "expectancy_r": round(float(expectancy), 3),
        "profit_factor": round(float(profit_factor), 2) if profit_factor != float("inf") else "inf",
        "total_r": round(float(r_values.sum()), 2),
        "max_drawdown_r": round(float(max_drawdown), 2),
        "sharpe_like_ratio": round(float(sharpe_like), 3),
        "max_consecutive_losses": max_streak,
    }


def print_report(metrics: dict, label: str = "Performance Report") -> None:
    print(f"\n=== {label} ===")
    if "error" in metrics:
        print(metrics["error"])
        return
    for key, value in metrics.items():
        label_fmt = key.replace("_", " ").title()
        print(f"{label_fmt:<25}: {value}")

    print("\n--- Interpretation ---")
    wr = metrics.get("win_rate_pct", 0)
    pf = metrics.get("profit_factor", 0)
    exp = metrics.get("expectancy_r", 0)

    pf_val = 999 if isinstance(pf, str) else pf

    if exp <= 0:
        print("Expectancy is negative or zero — this system loses money over time at current parameters.")
    elif pf_val < 1.2:
        print("Profit factor is thin — system is marginally profitable, sensitive to slippage/spread costs.")
    elif pf_val < 1.5:
        print("Profit factor is solid but not exceptional — typical of a realistic retail system.")
    else:
        print("Profit factor is strong — verify this isn't overfit to the test period before trusting it live.")

    if wr < 40 and exp > 0:
        print("Low win rate but positive expectancy — trend-following profile (few big wins, many small losses).")
    elif wr > 60:
        print("High win rate — check R:R isn't being sacrificed for win rate (small wins, occasional large losses).")


def main():
    parser = argparse.ArgumentParser(description="Generate a performance report from trade data.")
    parser.add_argument("--csv", help="Path to a backtest trade CSV (from backtest/engine.py)")
    parser.add_argument("--history", help="Path to alert_history.jsonl from the live system")
    args = parser.parse_args()

    if not args.csv and not args.history:
        parser.error("Provide either --csv or --history")

    if args.csv:
        df = load_trades_from_csv(args.csv)
        metrics = compute_metrics(df)
        print_report(metrics, label=f"Backtest Report: {Path(args.csv).stem}")

    if args.history:
        df = load_trades_from_history(args.history)
        metrics = compute_metrics(df)
        print_report(metrics, label="Live Alert History Report")


if __name__ == "__main__":
    main()
