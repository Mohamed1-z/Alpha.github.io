"""
swift_scanner/config_validation.py

Validates your ScannerConfig values at startup using pydantic, catching
typos or out-of-range values (e.g. negative SL, a daily loss limit of
300% by mistake) before they cause a bad trade.

This does NOT replace your existing ScannerConfig dataclass — it validates
an already-built ScannerConfig instance and raises a clear error if
something looks wrong, rather than failing silently or trading on garbage.

Usage in your startup script (app.py or wherever ScannerConfig is built):
    from swift_scanner.config_validation import validate_config
    config = ScannerConfig()
    validate_config(config)   # raises ValueError with a clear message if invalid
"""

from __future__ import annotations

try:
    from pydantic import BaseModel, ValidationError, field_validator
except ModuleNotFoundError:
    BaseModel = None
    ValidationError = None

    def field_validator(*_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


class RiskConfigCheck(BaseModel if BaseModel is not None else object):
    """Validates the risk-related fields most likely to cause real damage if misconfigured."""
    daily_max_loss_percent: float
    max_correlated_open_positions: int
    risk_percent_per_trade: float
    order_volume: float
    reduce_position_fraction: float

    @field_validator("daily_max_loss_percent")
    @classmethod
    def daily_loss_must_be_sane(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"daily_max_loss_percent must be positive, got {v}")
        if v > 20:
            raise ValueError(
                f"daily_max_loss_percent is {v}% — this is unusually high and likely a typo "
                f"(e.g. did you mean {v/10}%?)"
            )
        return v

    @field_validator("max_correlated_open_positions")
    @classmethod
    def correlation_cap_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_correlated_open_positions must be >= 1, got {v}")
        return v

    @field_validator("risk_percent_per_trade")
    @classmethod
    def risk_per_trade_must_be_sane(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"risk_percent_per_trade must be positive, got {v}")
        if v > 5:
            raise ValueError(
                f"risk_percent_per_trade is {v}% per trade — this is aggressive for retail trading "
                f"and likely unintentional. Recommended range is 0.5-2%."
            )
        return v

    @field_validator("order_volume")
    @classmethod
    def order_volume_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"order_volume must be positive, got {v}")
        return v

    @field_validator("reduce_position_fraction")
    @classmethod
    def reduce_fraction_must_be_valid(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError(f"reduce_position_fraction must be between 0 and 1, got {v}")
        return v

    if BaseModel is None:

        def __init__(
            self,
            daily_max_loss_percent: float,
            max_correlated_open_positions: int,
            risk_percent_per_trade: float,
            order_volume: float,
            reduce_position_fraction: float,
        ) -> None:
            self.daily_loss_must_be_sane(float(daily_max_loss_percent))
            self.correlation_cap_must_be_positive(int(max_correlated_open_positions))
            self.risk_per_trade_must_be_sane(float(risk_percent_per_trade))
            self.order_volume_must_be_positive(float(order_volume))
            self.reduce_fraction_must_be_valid(float(reduce_position_fraction))


class SymbolRiskCheck(BaseModel if BaseModel is not None else object):
    """Validates per-symbol SL/TP values to catch inverted or zero-distance setups."""
    symbol: str
    sl_value: float
    tp1_value: float
    tp2_value: float
    tp3_value: float

    @field_validator("sl_value", "tp1_value", "tp2_value", "tp3_value")
    @classmethod
    def must_be_positive(cls, v: float, info) -> float:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v

    if BaseModel is None:

        def __init__(
            self,
            symbol: str,
            sl_value: float,
            tp1_value: float,
            tp2_value: float,
            tp3_value: float,
        ) -> None:
            for field_name, value in {
                "sl_value": sl_value,
                "tp1_value": tp1_value,
                "tp2_value": tp2_value,
                "tp3_value": tp3_value,
            }.items():
                if float(value) <= 0:
                    raise ValueError(f"{field_name} must be positive, got {value}")


def validate_config(config) -> None:
    """Validate a ScannerConfig instance. Raises ValueError with a clear,
    actionable message if any risk-critical field looks wrong. Call this
    once at startup before the scanner begins trading."""
    errors: list[str] = []

    tp_target_level = str(getattr(config, "tp_target_level", "tp3") or "tp3").strip().lower()
    if tp_target_level not in {"auto", "tp1", "tp2", "tp3"}:
        errors.append(
            f"  - tp_target_level must be one of: auto, tp1, tp2, tp3 (got {tp_target_level})"
        )

    try:
        RiskConfigCheck(
            daily_max_loss_percent=getattr(config, "daily_max_loss_percent", 3.0),
            max_correlated_open_positions=getattr(config, "max_correlated_open_positions", 1),
            risk_percent_per_trade=getattr(config, "risk_percent_per_trade", 1.0),
            order_volume=getattr(config, "order_volume", 0.02),
            reduce_position_fraction=getattr(config, "reduce_position_fraction", 0.5),
        )
    except Exception as e:
        if ValidationError is not None and isinstance(e, ValidationError):
            for err in e.errors():
                errors.append(f"  - {err['msg']}")
        else:
            errors.append(f"  - {e}")

    symbol_configs = getattr(config, "symbol_scan_configs", None)
    if symbol_configs:
        for symbol, sym_cfg in symbol_configs.items():
            try:
                SymbolRiskCheck(
                    symbol=symbol,
                    sl_value=getattr(sym_cfg, "main_sl_value", 1.0) or 1.0,
                    tp1_value=getattr(sym_cfg, "main_tp1_value", 1.0) or 1.0,
                    tp2_value=getattr(sym_cfg, "main_tp2_value", 1.0) or 1.0,
                    tp3_value=getattr(sym_cfg, "main_tp3_value", 1.0) or 1.0,
                )
                tp1, tp2, tp3 = sym_cfg.main_tp1_value, sym_cfg.main_tp2_value, sym_cfg.main_tp3_value
                if not (tp1 <= tp2 <= tp3):
                    errors.append(
                        f"  - {symbol}: TP values are not in ascending order (TP1={tp1}, TP2={tp2}, TP3={tp3}). "
                        f"This will cause TP2/TP3 logic to behave unexpectedly."
                    )
            except Exception as e:
                if ValidationError is not None and isinstance(e, ValidationError):
                    for err in e.errors():
                        errors.append(f"  - {symbol}: {err['msg']}")
                else:
                    errors.append(f"  - {symbol}: {e}")

    if errors:
        raise ValueError(
            "Config validation failed — fix these before starting the scanner:\n" + "\n".join(errors)
        )

    print("[ConfigValidation] All risk-critical config values passed validation.")
