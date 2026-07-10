# SWIFT MERGED Watchlist Scanner

This project converts the provided `SWIFT MERGED` Pine Script strategy into a Python scanner that:

- scans an MT5 watchlist across multiple timeframes
- calculates the main SWIFT signal
- supports ALMA, TEMA, and HullMA signal bases
- supports the Pine-style alternate timeframe multiplier
- tracks main TP1, TP2, TP3, and SL levels
- saves a chart image for each fresh SWIFT event
- prints local alerts to the console
- can optionally send alert images to Telegram
- can export a TP/SL outcome report from alert history

The Python package folder is still named `order_blocks` for compatibility with the existing app layout, but the old order-block detector has been removed from the runtime scanner.

## Watchlist

The default watchlist is:

- `BTCUSD`
- `USOIL`
- `XAUUSD`
- `NASDAQ`
- `US30`
- `EURUSD`
- `USDJPY`
- `GBPUSD`
- `AUDUSD`

The scanner tries to match those requests to your broker's actual MT5 symbol names, including common aliases like `USTEC`, `NAS100`, `US100`, `WTIUSD`, or symbols with suffixes.

## Detection Timeframes

The scanner detects on these timeframes by default:

- `M15`
- `M30`
- `H1`

Override them from the command line:

```bash
python app.py --timeframes M15 M30 H1
```

## SWIFT Logic

The Python scanner mirrors the actionable parts of the pasted Pine strategy:

- main signal crossover: selected MA of close crosses selected MA of open
- optional range-filter confirmation for main entries
- TP/SL state progression through TP1, TP2, TP3, and SL
- default Pine-style main settings: ALMA length 2, alternate multiplier 8, TP levels `1.0 / 1.5 / 2.0%`, SL `0.5%`

The Pine supply/demand drawing section does not feed the strategy entry/exit conditions, so it is not part of the Python signal scanner.

## Requirements

- Python `3.11+`
- An installed `MetaTrader 5` terminal
- MT5 must be open and logged into a broker account before running the scanner

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run It

Single scan:

```bash
python app.py --once
```

Continuous monitoring:

```bash
python app.py --interval 30
```

Alert-only monitoring:

```bash
python app.py --interval 30 --telegram-only --disable-execution
```

Live MT5 execution is enabled by default. To place real orders, MT5 must be open,
logged in, and allowed to trade; run the watcher without `--disable-execution`.
Entry signals wait for the configured pip buffer before sending a market order.

Useful options:

```bash
python app.py --once --timeframes M15 H1 --bars 2000 --chart-bars 160 --mute
```

SWIFT options:

```bash
python app.py --once --single-signal
python app.py --once --trend-filter
python app.py --once --disable-alternate-signals
python app.py --once --main-ma TEMA
python app.py --once --main-tp 1 1.5 2 --main-sl 0.5
```

## TP/SL Outcome Report

The Excel report checks each historical alert and marks whether TP3, SL, both in the same candle, or neither was touched first:

```bash
python app.py --export-alert-report --report-lookahead-bars 40
```

The output workbook includes:

- `Alerts`: each alert with TP/SL outcome, hit time, bars to hit, result in R, max favorable R, and max adverse R
- `Summary`: direction-candle summary
- `TP_SL_Summary`: grouped TP hit rate, SL hit count, same-candle ambiguity, average result, and average favorable/adverse movement

## Telegram Alerts

1. Open Telegram and message `@BotFather`.
2. Create a bot with `/newbot` and copy the bot token.
3. Message your new bot once from your phone so Telegram knows your chat exists.
4. In a browser, open:

```text
https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
```

5. Find your numeric `chat.id` in the JSON response.
6. Copy `.env.example` to `.env` and fill in:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_IDS=your_chat_id_1,your_chat_id_2
```

7. Test the connection:

```bash
python app.py --test-telegram
```

8. Run the watcher in Telegram-only mode:

```bash
python app.py --interval 30 --telegram-only
```

## Output

Signal images are written under:

```text
signals/YYYY-MM-DD/
```

The scanner also stores de-duplication state in:

```text
signals/alert_state.json
```

That keeps it from repeating the same SWIFT event every scan cycle.

## Notes

- The scanner can place live MT5 market orders when execution is enabled. Use
  `--disable-execution` for alert-only scanning.
- The alternate timeframe path resamples MT5 candles with Pine-style lookahead behavior for historical bars.
- If your broker uses unusual symbol names, update `SYMBOL_ALIASES` in [config.py](/c:/Users/Alpha/Documents/tradeview/order_blocks/config.py).
