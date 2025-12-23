from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    """Holds every knob that controls the backtest."""

    ticker: str = "005930.KS"
    start: str = "2020-01-01"
    end: str = "2024-12-31"
    initial_cash: float = 1_000_000
    fee_rate: float = 0.00015
    price_for_entry: Literal["open", "close"] = "open"
    use_adj_close: bool = False
    output_dir: Path = Path(".")
    fast_window: int = 20
    slow_window: int = 60


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------
def fetch_price_data(cfg: StrategyConfig) -> pd.DataFrame:
    price = yf.download(
        cfg.ticker,
        start=cfg.start,
        end=cfg.end,
        auto_adjust=False,
        progress=False,
    )
    if price.empty:
        raise RuntimeError(
            "yfinance returned no rows. Check the ticker symbol or the date range."
        )

    if isinstance(price.columns, pd.MultiIndex):
        price.columns = price.columns.get_level_values(0)

    price = price.reset_index()
    price["date"] = pd.to_datetime(price["Date"]).dt.tz_localize(None).dt.date
    rename_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    }
    price = price.rename(columns=rename_map)

    close_col = "adj_close" if cfg.use_adj_close else "close"
    price["close_price"] = price[close_col]
    columns = ["date", "open", "high", "low", "close", "adj_close", "volume", "close_price"]
    return price[columns].copy()


# ---------------------------------------------------------------------------
# Signal creation
# ---------------------------------------------------------------------------
def attach_signals(price_df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    if cfg.fast_window <= 0 or cfg.slow_window <= 0:
        raise ValueError("Moving-average windows must be positive integers.")
    if cfg.fast_window >= cfg.slow_window:
        raise ValueError("fast_window must be smaller than slow_window for MA crossover.")

    df = price_df.copy()
    df["fast_ma"] = df["close_price"].rolling(cfg.fast_window).mean()
    df["slow_ma"] = df["close_price"].rolling(cfg.slow_window).mean()
    df["ma_diff"] = df["fast_ma"] - df["slow_ma"]

    df["signal_source"] = df["ma_diff"].shift(1)
    df["signal"] = df["signal_source"].apply(_ma_signal)
    return df


def _ma_signal(diff: float | int | None) -> str:
    if diff is None or np.isnan(diff):
        return "HOLD"
    if diff > 0:
        return "BUY"
    if diff < 0:
        return "SELL"
    return "HOLD"


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------
class Backtester:
    def __init__(self, data: pd.DataFrame, cfg: StrategyConfig):
        self.data = data.copy()
        self.cfg = cfg

    def run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        cash = float(self.cfg.initial_cash)
        shares = 0
        position = 0  # 0 = flat, 1 = long

        equity_rows: list[dict] = []
        trades: list[dict] = []

        entry_price = None
        entry_date = None
        entry_fee = 0.0

        for row in self.data.itertuples(index=False):
            trade_price = row.open if self.cfg.price_for_entry == "open" else row.close_price

            if row.signal == "BUY" and position == 0:
                affordable = cash / (trade_price * (1.0 + self.cfg.fee_rate))
                qty = int(affordable)
                if qty > 0:
                    cost = qty * trade_price
                    fee = cost * self.cfg.fee_rate
                    cash -= cost + fee
                    shares += qty
                    position = 1

                    entry_price = trade_price
                    entry_date = row.date
                    entry_fee = fee

                    trades.append(
                        {
                            "type": "BUY",
                            "date": row.date,
                            "price": trade_price,
                            "qty": qty,
                            "fee": fee,
                            "cash_after": cash,
                            "signal_source": row.signal_source,
                        }
                    )

            elif row.signal == "SELL" and position == 1:
                proceeds = shares * trade_price
                fee = proceeds * self.cfg.fee_rate
                cash += proceeds - fee

                pnl = (trade_price - entry_price) * shares - (entry_fee + fee)
                ret = (trade_price - entry_price) / entry_price if entry_price else 0.0

                trades.append(
                    {
                        "type": "SELL",
                        "date": row.date,
                        "price": trade_price,
                        "qty": shares,
                        "fee": fee,
                        "cash_after": cash,
                        "pnl": pnl,
                        "return": ret,
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "signal_source": row.signal_source,
                    }
                )

                shares = 0
                position = 0
                entry_price = None
                entry_date = None
                entry_fee = 0.0

            equity = cash + shares * row.close_price
            equity_rows.append(
                {
                    "date": row.date,
                    "cash": cash,
                    "shares": shares,
                    "close_price": row.close_price,
                    "equity": equity,
                    "signal": row.signal,
                    "signal_source": row.signal_source,
                }
            )

        return pd.DataFrame(equity_rows), pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_equity_metrics(equity_df: pd.DataFrame) -> dict:
    eq = equity_df.copy()
    eq["equity"] = pd.to_numeric(eq["equity"], errors="coerce")
    eq = eq.dropna(subset=["equity"])
    if eq.empty:
        raise RuntimeError("Equity curve is empty.")

    eq["returns"] = eq["equity"].pct_change().fillna(0.0)
    initial = float(eq["equity"].iloc[0])
    final = float(eq["equity"].iloc[-1])
    cumulative = (final / initial) - 1.0

    eq["peak"] = eq["equity"].cummax()
    eq["drawdown"] = eq["equity"] / eq["peak"] - 1.0
    max_dd = float(eq["drawdown"].min())

    sharpe = np.nan
    if eq["returns"].std() > 0:
        sharpe = (eq["returns"].mean() / eq["returns"].std()) * np.sqrt(252)

    return {
        "initial_capital": initial,
        "final_equity": final,
        "cumulative_return": cumulative,
        "max_drawdown": max_dd,
        "sharpe_like": float(sharpe) if not np.isnan(sharpe) else np.nan,
        "days": int(eq.shape[0]),
    }


def compute_trade_stats(trades_df: pd.DataFrame) -> dict:
    if trades_df.empty:
        return {"trades": 0, "win_rate": 0.0, "avg_trade_return": 0.0}

    sells = trades_df[trades_df["type"] == "SELL"].copy()
    if sells.empty:
        return {"trades": 0, "win_rate": 0.0, "avg_trade_return": 0.0}

    sells["return"] = pd.to_numeric(sells["return"], errors="coerce")
    win_rate = (sells["return"] > 0).mean() if not sells.empty else 0.0

    return {
        "trades": int(sells.shape[0]),
        "win_rate": float(win_rate),
        "avg_trade_return": float(sells["return"].mean()),
    }


def buy_and_hold(price_df: pd.DataFrame) -> dict:
    first = float(price_df["close_price"].iloc[0])
    last = float(price_df["close_price"].iloc[-1])
    return {"buy_hold_return": (last / first) - 1.0}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def save_outputs(cfg: StrategyConfig, equity_df: pd.DataFrame, trades_df: pd.DataFrame, merged_df: pd.DataFrame) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    equity_df.to_csv(cfg.output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(cfg.output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    merged_df.to_csv(cfg.output_dir / "backtest_table.csv", index=False, encoding="utf-8-sig")


def display_summary(equity_metrics: dict, trade_stats: dict, benchmark: dict) -> None:
    print("\n=== Portfolio Metrics ===")
    for key, value in equity_metrics.items():
        print(f"{key:>20}: {value}")

    print("\n=== Trade Stats ===")
    for key, value in trade_stats.items():
        print(f"{key:>20}: {value}")

    print("\n=== Buy & Hold Benchmark ===")
    for key, value in benchmark.items():
        print(f"{key:>20}: {value}")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def main() -> None:
    cfg = StrategyConfig()
    price_df = fetch_price_data(cfg)
    merged = attach_signals(price_df, cfg)

    tester = Backtester(merged, cfg)
    equity_df, trades_df = tester.run()

    equity_metrics = compute_equity_metrics(equity_df)
    trade_stats = compute_trade_stats(trades_df)
    benchmark = buy_and_hold(price_df)

    save_outputs(cfg, equity_df, trades_df, merged)
    display_summary(equity_metrics, trade_stats, benchmark)

    print("\nCSV files saved to:", cfg.output_dir.resolve())


if __name__ == "__main__":
    main()
