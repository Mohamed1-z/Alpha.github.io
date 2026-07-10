"""
swift_scanner/logging_setup.py

Adds structured, timestamped, rotating log files alongside your existing
print() statements — this does not remove or break any current print()
calls in watcher.py; it adds a parallel logger you can adopt gradually.

Usage in watcher.py (optional, additive):
    from swift_scanner.logging_setup import get_logger
    log = get_logger()
    log.info("Signal detected for {}", symbol)
    log.warning("Blocked {}: spread too wide", symbol)

Logs are written to logs/swift_scanner.log with daily rotation and 14-day
retention, plus colored console output.
"""

from __future__ import annotations

import sys
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

try:
    from loguru import logger
except ModuleNotFoundError:
    logger = None

_configured = False
_fallback_logger: logging.Logger | None = None


def get_logger():
    """Return the configured loguru logger. Safe to call multiple times —
    only configures sinks once per process."""
    global _configured
    global _fallback_logger
    if _configured:
        if logger is None:
            return _fallback_logger
        return logger

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    if logger is None:
        fallback_logger = logging.getLogger("swift_scanner")
        fallback_logger.setLevel(logging.DEBUG)
        fallback_logger.handlers.clear()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S"))
        fallback_logger.addHandler(console_handler)

        file_handler = TimedRotatingFileHandler(
            log_dir / "swift_scanner.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s")
        )
        fallback_logger.addHandler(file_handler)

        _fallback_logger = fallback_logger
        _configured = True
        return fallback_logger

    logger.remove()  # remove default handler to avoid duplicate console output

    # Console sink — colored, concise
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )

    # File sink — full detail, rotates daily, keeps 14 days
    logger.add(
        log_dir / "swift_scanner.log",
        level="DEBUG",
        rotation="00:00",
        retention="14 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        encoding="utf-8",
    )

    # Separate file just for trade executions and risk-management blocks —
    # useful for quickly auditing what happened during a losing day without
    # wading through every scan-cycle debug line.
    logger.add(
        log_dir / "trades.log",
        level="INFO",
        rotation="00:00",
        retention="90 days",
        filter=lambda record: "[Trade]" in record["message"] or "[RiskMgmt]" in record["message"] or "[Gate]" in record["message"],
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        encoding="utf-8",
    )

    _configured = True
    return logger
