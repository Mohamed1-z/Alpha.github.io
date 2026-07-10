# Pine Script Alignment

Compared against the pasted `SWIFT MERGED` Pine Script strategy.

## Matched

- Main signal defaults match the Pine inputs.
- Main MA crossover triggers use close MA crossing open MA.
- ALMA, TEMA, and HullMA are implemented for the configured signal bases.
- Alternate timeframe signals use the chart timeframe multiplied by the configured multiplier.
- The optional SWIFT range filter can confirm main entries.
- The main state machine tracks entry, TP1, TP2, TP3, SL, last main direction, and main-position-closed state.
- Alert events include entry, SL, TP1, TP2, and TP3 values.

## Intentional Differences

- Pine `strategy.entry` and `strategy.exit` place TradingView backtest orders. The Python app is an MT5 scanner and alert generator; it does not submit trades.
- Pine chart labels, fills, tables, and supply/demand drawing objects are not recreated one-for-one. The Python app saves signal charts with candles and SWIFT risk levels instead.
- The Pine supply/demand section only draws zones and does not feed the strategy's actionable entry/exit logic, so it was not carried into the Python signal engine.
- TradingView `request.security(..., lookahead_on)` behavior is approximated with pandas resampling and forward alignment over MT5 candles.
