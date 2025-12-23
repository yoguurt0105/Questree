from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf
import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    ticker: str = "005930.KS"
    start: str = "2020-01-01"
    end: str = "2024-12-31"
    initial_cash: float = 1_000_000
    fee_rate: float = 0.00015
    days_before: int = 3  # 연휴 시작 3일 전 매수
    days_after: int = 2   # 연휴 종료 2일 후 매도
    output_dir: Path = Path("./holiday_results")

# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------
def fetch_price_data(cfg: StrategyConfig) -> pd.DataFrame:
    df = yf.download(cfg.ticker, start=cfg.start, end=cfg.end, auto_adjust=False, progress=False)
    if df.empty:
        raise RuntimeError("데이터를 가져오지 못했습니다.")

    # 다중 인덱스 제거 (yfinance 버전에 대비)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 인덱스(Date)를 컬럼으로 빼고 이름을 소문자 'date'로 통일
    df = df.reset_index()
    df.columns = [col.lower() for col in df.columns]
    
    # 'date' 컬럼이 없으면 'Date' 컬럼을 찾아 변환 (오류 방지 핵심)
    if 'date' not in df.columns:
        if 'Date' in df.columns:
            df = df.rename(columns={'Date': 'date'})
        else:
            # 인덱스 자체가 날짜인 경우
            df.index.name = 'date'
            df = df.reset_index()

    # 날짜 형식 정리 (시간 정보 제거)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df

# ---------------------------------------------------------------------------
# Signal creation (2020-2024 정확한 명절 반영)
# ---------------------------------------------------------------------------
def attach_signals(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    df = df.copy()
    
    # 2020~2024 주요 명절 및 공휴일 (설날, 추석, 크리스마스, 신정)
    holidays = [
        "2020-01-01", "2020-01-25", "2020-10-01", "2020-12-25",
        "2021-01-01", "2021-02-12", "2021-09-21", "2021-12-25",
        "2022-01-01", "2022-02-01", "2022-09-10", "2022-12-25",
        "2023-01-01", "2023-01-22", "2023-09-29", "2023-12-25",
        "2024-01-01", "2024-02-10", "2024-09-17", "2024-12-25"
    ]
    holiday_dates = [datetime.datetime.strptime(h, "%Y-%m-%d").date() for h in holidays]
    
    # 투자 구간 판단 로직
    is_invest_period = []
    all_dates = df['date'].values
    
    for current_date in all_dates:
        in_range = False
        for h_date in holiday_dates:
            diff = (h_date - current_date).days
            # 공휴일 전 3일부터 후 2일까지
            if -cfg.days_after <= diff <= cfg.days_before:
                in_range = True
                break
        is_invest_period.append(in_range)
    
    df['is_invest_period'] = is_invest_period

    # 신호 생성 (이전 상태와 비교하여 BUY/SELL 결정)
    df['signal'] = "HOLD"
    df['prev_invest'] = df['is_invest_period'].shift(1, fill_value=False)
    
    df.loc[(df['is_invest_period'] == True) & (df['prev_invest'] == False), 'signal'] = "BUY"
    df.loc[(df['is_invest_period'] == False) & (df['prev_invest'] == True), 'signal'] = "SELL"
    
    return df

# ---------------------------------------------------------------------------
# Backtester & Metrics
# ---------------------------------------------------------------------------
class Backtester:
    def __init__(self, data: pd.DataFrame, cfg: StrategyConfig):
        self.data = data
        self.cfg = cfg

    def run(self):
        cash = float(self.cfg.initial_cash)
        shares = 0
        equity_rows = []
        trades = []
        entry_price = 0

        for row in self.data.itertuples():
            # 매수: 투자 구간 진입 시 시가
            if row.signal == "BUY" and shares == 0:
                trade_price = row.open
                qty = int(cash / (trade_price * (1 + self.cfg.fee_rate)))
                if qty > 0:
                    fee = qty * trade_price * self.cfg.fee_rate
                    cash -= (qty * trade_price + fee)
                    shares = qty
                    entry_price = trade_price
                    trades.append({"type": "BUY", "date": row.date, "price": trade_price, "qty": qty})

            # 매도: 투자 구간 종료 시 시가
            elif row.signal == "SELL" and shares > 0:
                trade_price = row.open
                fee = shares * trade_price * self.cfg.fee_rate
                cash += (shares * trade_price - fee)
                ret = (trade_price - entry_price) / entry_price
                trades.append({"type": "SELL", "date": row.date, "price": trade_price, "qty": shares, "return": ret})
                shares = 0

            equity = cash + (shares * row.close)
            equity_rows.append({"date": row.date, "equity": equity})

        return pd.DataFrame(equity_rows), pd.DataFrame(trades)

def main():
    cfg = StrategyConfig()
    try:
        price_df = fetch_price_data(cfg)
        merged = attach_signals(price_df, cfg)
        
        tester = Backtester(merged, cfg)
        equity_df, trades_df = tester.run()
        
        # 결과 계산 및 출력
        final_equity = equity_df['equity'].iloc[-1]
        total_return = (final_equity / cfg.initial_cash - 1) * 100
        
        print(f"=== {cfg.ticker} 캘린더 전략 결과 ===")
        print(f"최종 자산: {final_equity:,.0f}원")
        print(f"누적 수익률: {total_return:.2f}%")
        print(f"매매 횟수: {len(trades_df[trades_df['type']=='SELL'])}회")
        
    except Exception as e:
        print(f"오류 발생: {e}")

if __name__ == "__main__":
    main()