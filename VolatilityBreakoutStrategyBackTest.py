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
    """모든 백테스트 설정값을 관리합니다."""

    ticker: str = "005930.KS"
    start: str = "2020-01-01"
    end: str = "2024-12-31"
    initial_cash: float = 1_000_000
    fee_rate: float = 0.00015  # 0.015%
    k: float = 0.5             # 변동성 돌파 계수 (KSAT_human_ver1.py 기준)
    output_dir: Path = Path("./vbt_results")


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
        raise RuntimeError("데이터를 가져오지 못했습니다. 티커나 기간을 확인하세요.")

    if isinstance(price.columns, pd.MultiIndex):
        price.columns = price.columns.get_level_values(0)

    price = price.reset_index()
    # 'date' 컬럼 생성 및 정리
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
    return price.copy()


# ---------------------------------------------------------------------------
# Signal creation (수정된 부분: 변동성 돌파 로직)
# ---------------------------------------------------------------------------
def attach_signals(price_df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    df = price_df.copy()
    
    # 1. 전일 고가/저가 데이터 준비
    df["prev_high"] = df["high"].shift(1)
    df["prev_low"] = df["low"].shift(1)
    
    # 2. 목표가 계산: 당일 시가 + (전일 변동폭 * K)
    # KSAT_human_ver1.py: target_price = stck_oprc + (stck_hgpr - stck_lwpr) * 0.5
    df["target_price"] = df["open"] + (df["prev_high"] - df["prev_low"]) * cfg.k
    
    # 3. 매수 신호: 당일 고가가 목표가를 돌파했는지 확인
    # (실제 백테스트 시뮬레이션에서 가격 도달 여부를 판단하기 위해 사용)
    df["breakout"] = df["high"] > df["target_price"]
    
    return df


# ---------------------------------------------------------------------------
# Backtester (수정된 부분: 당일 매수 / 당일 매도 시뮬레이션)
# ---------------------------------------------------------------------------
class Backtester:
    def __init__(self, data: pd.DataFrame, cfg: StrategyConfig):
        # 전일 데이터가 없는 첫 줄 제외
        self.data = data.dropna(subset=["target_price"]).copy()
        self.cfg = cfg

    def run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        cash = float(self.cfg.initial_cash)
        shares = 0
        equity_rows: list[dict] = []
        trades: list[dict] = []

        for row in self.data.itertuples(index=False):
            # --- 1. 매수 체크 (목표가 돌파 시) ---
            if row.breakout and shares == 0:
                trade_price = row.target_price
                qty = int(cash / (trade_price * (1.0 + self.cfg.fee_rate)))
                
                if qty > 0:
                    cost = qty * trade_price
                    fee = cost * self.cfg.fee_rate
                    cash -= (cost + fee)
                    shares = qty
                    
                    trades.append({
                        "type": "BUY",
                        "date": row.date,
                        "price": trade_price,
                        "qty": qty,
                        "fee": fee,
                        "cash_after": cash
                    })

            # --- 2. 매도 체크 (당일 종가 청산) ---
            # KSAT_human_ver1.py의 't_sell' 시간 일괄 매도 로직을 반영
            if shares > 0:
                sell_price = row.close
                proceeds = shares * sell_price
                fee = proceeds * self.cfg.fee_rate
                cash += (proceeds - fee)

                buy_info = trades[-1]
                pnl = (sell_price - buy_info["price"]) * shares - (buy_info["fee"] + fee)
                ret = (sell_price - buy_info["price"]) / buy_info["price"]

                trades.append({
                    "type": "SELL",
                    "date": row.date,
                    "price": sell_price,
                    "qty": shares,
                    "fee": fee,
                    "cash_after": cash,
                    "pnl": pnl,
                    "return": ret,
                    "entry_date": buy_info["date"],
                    "entry_price": buy_info["price"]
                })
                shares = 0

            # 자산 가치 기록 (당일 종료 후 시점)
            equity = cash + (shares * row.close)
            equity_rows.append({
                "date": row.date,
                "cash": cash,
                "shares": shares,
                "close_price": row.close,
                "equity": equity
            })

        return pd.DataFrame(equity_rows), pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Metrics / Output (원본 파일 유지)
# ---------------------------------------------------------------------------
def compute_equity_metrics(equity_df: pd.DataFrame) -> dict:
    eq = equity_df.copy()
    eq["equity"] = pd.to_numeric(eq["equity"], errors="coerce")
    eq = eq.dropna(subset=["equity"])
    
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
        "cumulative_return": f"{cumulative:.2%}",
        "max_drawdown": f"{max_dd:.2%}",
        "sharpe_like": round(float(sharpe), 2) if not np.isnan(sharpe) else np.nan,
        "total_days": int(eq.shape[0]),
    }


def compute_trade_stats(trades_df: pd.DataFrame) -> dict:
    sells = trades_df[trades_df["type"] == "SELL"].copy()
    if sells.empty:
        return {"trades": 0, "win_rate": "0%"}

    win_rate = (sells["pnl"] > 0).mean()
    return {
        "total_trades": int(sells.shape[0]),
        "win_rate": f"{win_rate:.2%}",
        "avg_trade_return": f"{sells['return'].mean():.2%}",
    }


def display_summary(equity_metrics: dict, trade_stats: dict) -> None:
    print("\n=== 포트폴리오 성과 리포트 (변동성 돌파) ===")
    for key, value in equity_metrics.items():
        print(f"{key:>20}: {value}")

    print("\n=== 매매 통계 ===")
    for key, value in trade_stats.items():
        print(f"{key:>20}: {value}")


def main() -> None:
    cfg = StrategyConfig()
    price_df = fetch_price_data(cfg)
    merged = attach_signals(price_df, cfg)

    tester = Backtester(merged, cfg)
    equity_df, trades_df = tester.run()

    equity_metrics = compute_equity_metrics(equity_df)
    trade_stats = compute_trade_stats(trades_df)

    # 화면 출력
    display_summary(equity_metrics, trade_stats)
    
    # 파일 저장
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    equity_df.to_csv(cfg.output_dir / "equity_curve_vbt.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(cfg.output_dir / "trades_vbt.csv", index=False, encoding="utf-8-sig")
    print(f"\n결과 파일이 {cfg.output_dir.resolve()} 에 저장되었습니다.")


if __name__ == "__main__":
    main()