"""
🦚 Peacock Backtest V4 — R-Multiple Edge Validator
==================================================
Backtest กลยุทธ์ Peacock เพื่อพิสูจน์ว่ามี Edge จริงหรือไม่

ENTRY:
  - Fresh Today (Peacock เต็มวันแรก) → Buy at next Open (T+1)

EXIT:
  - Initial SL (วัน entry):
    * ถ้า (Open_T+1 − EMA200)/EMA200 > 3% → SL = EMA10
    * ถ้า ≤ 3% → SL = EMA200
    * Master Law (ขอบบน): SL ห่างจาก entry ไม่เกิน 3%
    * Min Risk (ขอบล่าง): SL ห่างจาก entry ไม่ต่ำกว่า 1%
  - Trailing: ทุกวัน Stop = max(SL_initial, EMA20)
    EXIT เมื่อ Close < Stop → ขายที่ next Open

R-Multiple = (exit − entry) / (entry − SL_initial) − fee_R
fee_R = (entry × fee_pct × 2) / risk_unit  ← round-trip

V4 Changes (QC):
  - Issue #2: เพิ่ม Survivorship Bias warning ตอน Run
  - Issue #3: still_open แยกออกจาก stats หลัก
  - Issue #5: เพิ่ม Fee Input (default 0.1%) หักจาก R
  - Issue #7: t-test + เตือนถ้า trade < 30
  - Executive Summary: 3 ข้อด้านบนสุด

วิธีรัน:
  pip install streamlit yfinance pandas plotly scipy
  streamlit run peacock_backtest.py
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from scipy import stats as scipy_stats
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ══════════════════════════════════════════════════════
#  PAGE CONFIG
# ══════════════════════════════════════════════════════
st.set_page_config(
    page_title="Peacock Backtest",
    page_icon="🦚",
    layout="wide"
)

st.title("🦚 Peacock Backtest — R-Distribution")
st.caption("พิสูจน์ Edge ของกลยุทธ์ Peacock ด้วย R-Multiple Distribution")

# ── Session State ─────────────────────────────────────
if "trades_df" not in st.session_state:
    st.session_state.trades_df = pd.DataFrame()
if "scan_errors" not in st.session_state:
    st.session_state.scan_errors = []
if "scan_summary" not in st.session_state:
    st.session_state.scan_summary = {}


# ══════════════════════════════════════════════════════
#  SIDEBAR — SETTINGS
# ══════════════════════════════════════════════════════
with st.sidebar:
    st.header("📁 Universe")
    universe_choice = st.radio(
        "เลือก Market",
        ["อัพโหลด CSV", "S&P 500 (preset)", "SET100 (preset)", "พิมพ์เอง"],
        index=0,
    )

    uploaded_file = None
    typed_symbols = ""
    if universe_choice == "อัพโหลด CSV":
        uploaded_file = st.file_uploader("CSV (มีคอลัมน์ Symbol)", type=["csv"])
    elif universe_choice == "พิมพ์เอง":
        typed_symbols = st.text_area(
            "พิมพ์ symbol คั่นด้วย comma หรือ newline",
            placeholder="AAPL, MSFT, NVDA",
            height=80,
        )

    limit_n = st.number_input(
        "จำกัดจำนวน symbol (0 = ทั้งหมด)",
        min_value=0, max_value=2000, value=0, step=10,
        help="แนะนำลองที่ 30-50 ก่อน เพื่อดูผลเร็ว"
    )

    st.divider()
    st.header("📅 ช่วงข้อมูล")
    period = st.selectbox("Period", ["2y", "5y", "10y", "max"], index=2)

    st.divider()
    st.header("⚙️ Backtest Rules")

    threshold_pct = st.number_input(
        "Threshold ลอยตัวสูง (%)",
        min_value=0.5, max_value=20.0, value=3.0, step=0.5,
        help="(Open_T+1−EMA200)/EMA200 เกินค่านี้ → ใช้ EMA10 SL"
    )

    master_law_pct = st.number_input(
        "Master Law: Max Risk per Trade (%)",
        min_value=1.0, max_value=10.0, value=3.0, step=0.5,
        help="SL ห่างจาก entry ห้ามเกินค่านี้ (ขอบบน)"
    )

    min_risk_pct = st.number_input(
        "🛡️ Min Risk per Trade (%)",
        min_value=0.0, max_value=3.0, value=1.0, step=0.1,
        help="SL ห่างจาก entry ขั้นต่ำ (ขอบล่าง)"
    )

    # ── V4: Fee Input ──────────────────────────────────
    st.divider()
    st.header("💸 Transaction Cost")
    fee_pct = st.number_input(
        "ค่า Fee ต่อข้าง (%)",
        min_value=0.0, max_value=1.0, value=0.1, step=0.01,
        format="%.2f",
        help="คิดเป็น Round-trip (ซื้อ + ขาย) อัตโนมัติ\nS&P 500 ≈ 0.05–0.10% | SET100 ≈ 0.15–0.20%"
    )
    st.caption(f"Round-trip รวม = **{fee_pct * 2:.2f}%** ต่อ trade")

    st.divider()
    st.header("🎯 Market Regime Filter")
    use_regime = st.checkbox(
        "เปิดใช้งาน Filter",
        value=True,
        help="เทรดเฉพาะตอน Benchmark > EMA200 (ตลาดขาขึ้น)"
    )
    benchmark_default = "SPY" if universe_choice == "S&P 500 (preset)" else (
        "^SET.BK" if universe_choice == "SET100 (preset)" else "SPY"
    )
    benchmark_symbol = st.text_input(
        "Benchmark Symbol",
        value=benchmark_default,
        disabled=not use_regime,
    )
    regime_ema_period = st.number_input(
        "Regime EMA Period",
        min_value=50, max_value=300, value=200, step=10,
        disabled=not use_regime,
    )

    st.divider()
    use_concurrent = st.checkbox(
        "⚡ Concurrent fetch (เร็วขึ้น 5-10×)",
        value=True
    )
    max_workers = st.number_input(
        "Threads", min_value=2, max_value=20, value=8, step=1,
        disabled=not use_concurrent
    )

    st.divider()
    run_btn = st.button("🚀 Run Backtest", type="primary", use_container_width=True)


# ══════════════════════════════════════════════════════
#  CORE LOGIC
# ══════════════════════════════════════════════════════
def calc_ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


@st.cache_data(ttl=3600, show_spinner=False)
def load_benchmark(symbol, period, ema_period):
    try:
        df = yf.download(
            symbol, period=period, interval="1d",
            progress=False, auto_adjust=False, threads=False
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df["EMA"] = calc_ema(df["Close"], ema_period)
        df["is_bull"] = df["Close"] > df["EMA"]
        return df[["Close", "EMA", "is_bull"]]
    except Exception:
        return None


def get_regime_at(benchmark_df, date):
    if benchmark_df is None:
        return "unknown"
    try:
        idx = benchmark_df.index.searchsorted(date, side="right") - 1
        if idx < 0:
            return "unknown"
        is_bull = bool(benchmark_df["is_bull"].iloc[idx])
        return "bull" if is_bull else "bear"
    except Exception:
        return "unknown"


def add_emas(df):
    df = df.copy()
    df["EMA10"] = calc_ema(df["Close"], 10)
    df["EMA20"] = calc_ema(df["Close"], 20)
    df["EMA35"] = calc_ema(df["Close"], 35)
    df["EMA75"] = calc_ema(df["Close"], 75)
    df["EMA200"] = calc_ema(df["Close"], 200)
    return df


def is_peacock_series(df):
    c = df["Close"]
    return (
        (c > df["EMA10"])
        & (df["EMA10"] > df["EMA20"])
        & (df["EMA20"] > df["EMA35"])
        & (df["EMA35"] > df["EMA75"])
        & (c > df["EMA200"])
    )


def find_fresh_signals(df):
    peacock = is_peacock_series(df)
    fresh = peacock & ~peacock.shift(1, fill_value=False)
    return np.where(fresh.values)[0]


def simulate_trade(df, signal_idx, threshold_pct, master_law_pct,
                   min_risk_pct=1.0, fee_pct=0.1):
    """
    จำลอง 1 trade ตาม Peacock rules

    SL Logic:
      ใช้ Open ของ T+1 (entry_price) ตัดสินใจ SL
      เพราะตอนเปิดตลาด trader เห็นราคาจริงรวม gap แล้ว
    
    Fee:
      Round-trip = fee_pct × 2
      แปลงเป็น R แล้วหักออก = (entry × fee_pct × 2) / risk_unit
    """
    if signal_idx + 1 >= len(df):
        return None

    entry_idx = signal_idx + 1
    entry_bar = df.iloc[entry_idx]
    entry_price = float(entry_bar["Open"])
    entry_date = entry_bar.name

    signal_bar = df.iloc[signal_idx]
    ema10 = float(signal_bar["EMA10"])
    ema200 = float(signal_bar["EMA200"])

    # ใช้ entry_price (Open T+1) เพราะ trader รู้ราคาจริงตอนเปิดตลาด
    distance = (entry_price - ema200) / ema200

    if distance > threshold_pct / 100:
        sl_candidate = ema10
        sl_type = "EMA10"
    else:
        sl_candidate = ema200
        sl_type = "EMA200"

    # Master Law: SL ต้องไม่ห่างเกิน max_risk_pct (ขอบบน)
    sl_max_floor = entry_price * (1 - master_law_pct / 100)
    sl_initial = max(sl_candidate, sl_max_floor)
    if sl_initial == sl_max_floor and sl_max_floor > sl_candidate:
        sl_type = f"{sl_type}+ML"

    # Min Risk: SL ต้องไม่ใกล้กว่า min_risk_pct (ขอบล่าง)
    sl_min_ceiling = entry_price * (1 - min_risk_pct / 100)
    if sl_initial > sl_min_ceiling:
        sl_initial = sl_min_ceiling
        sl_type = f"{sl_type}+MinR"

    if sl_initial >= entry_price:
        return None

    risk_unit = entry_price - sl_initial

    # คำนวณ fee เป็น R (round-trip)
    fee_R = (entry_price * fee_pct / 100 * 2) / risk_unit

    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        ema20_i = float(bar["EMA20"])
        stop = max(sl_initial, ema20_i)

        if float(bar["Close"]) < stop:
            if i + 1 < len(df):
                exit_idx = i + 1
                exit_bar = df.iloc[exit_idx]
                exit_price = float(exit_bar["Open"])
            else:
                exit_idx = i
                exit_bar = bar
                exit_price = float(exit_bar["Close"])

            R_gross = (exit_price - entry_price) / risk_unit
            R_net = R_gross - fee_R
            return {
                "entry_date": entry_date,
                "exit_date": exit_bar.name,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "sl_initial": round(sl_initial, 4),
                "sl_type": sl_type,
                "risk_pct": round(risk_unit / entry_price * 100, 2),
                "R_gross": round(R_gross, 3),
                "R": round(R_net, 3),
                "fee_R": round(fee_R, 3),
                "days_held": exit_idx - entry_idx,
                "exit_reason": "trailing/SL",
                "_exit_idx_in_df": exit_idx,
            }

    last_bar = df.iloc[-1]
    R_gross = (float(last_bar["Close"]) - entry_price) / risk_unit
    R_net = R_gross - fee_R
    return {
        "entry_date": entry_date,
        "exit_date": last_bar.name,
        "entry_price": round(entry_price, 4),
        "exit_price": round(float(last_bar["Close"]), 4),
        "sl_initial": round(sl_initial, 4),
        "sl_type": sl_type,
        "risk_pct": round(risk_unit / entry_price * 100, 2),
        "R_gross": round(R_gross, 3),
        "R": round(R_net, 3),
        "fee_R": round(fee_R, 3),
        "days_held": len(df) - 1 - entry_idx,
        "exit_reason": "still_open",
        "_exit_idx_in_df": len(df) - 1,
    }


def backtest_symbol(symbol, period, threshold_pct, master_law_pct,
                    min_risk_pct=1.0, fee_pct=0.1, benchmark_df=None):
    try:
        df = yf.download(
            symbol, period=period, interval="1d",
            progress=False, auto_adjust=False, threads=False
        )
        if df is None or df.empty or len(df) < 250:
            return symbol, [], "no_data"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = add_emas(df)

        signals = find_fresh_signals(df)
        trades = []
        last_exit_idx = -1
        for idx in signals:
            if idx <= last_exit_idx:
                continue
            if idx < 200:
                continue

            trade = simulate_trade(df, idx, threshold_pct, master_law_pct,
                                   min_risk_pct, fee_pct)
            if trade is None:
                continue
            trade["symbol"] = symbol
            signal_date = df.index[idx]
            trade["regime"] = get_regime_at(benchmark_df, signal_date)
            trades.append(trade)
            last_exit_idx = trade["_exit_idx_in_df"]

        return symbol, trades, "ok"
    except Exception as e:
        return symbol, [], f"error: {str(e)[:50]}"


# ══════════════════════════════════════════════════════
#  SYMBOL LIST LOADERS
# ══════════════════════════════════════════════════════
SP500_URL = "https://raw.githubusercontent.com/TitleO-hash/peacock-scanner/main/sp500_symbols.csv"
SET100_URL = "https://raw.githubusercontent.com/TitleO-hash/peacock-scanner/main/set100_symbols.csv"


@st.cache_data(ttl=86400)
def load_preset(url):
    df = pd.read_csv(url)
    # รองรับทั้ง column ชื่อ Symbol, symbol, Ticker, ticker
    for col in df.columns:
        if col.strip().lower() in ("symbol", "ticker", "symbols"):
            return df[col].dropna().astype(str).str.strip().tolist()
    return df.iloc[:, 0].dropna().astype(str).str.strip().tolist()


def get_symbols():
    if universe_choice == "S&P 500 (preset)":
        try:
            return load_preset(SP500_URL)
        except Exception as e:
            st.error(f"โหลด S&P 500 preset ไม่ได้: {e}")
            return []
    elif universe_choice == "SET100 (preset)":
        try:
            return load_preset(SET100_URL)
        except Exception as e:
            st.error(f"โหลด SET100 preset ไม่ได้: {e}")
            return []
    elif universe_choice == "อัพโหลด CSV" and uploaded_file:
        df = pd.read_csv(uploaded_file)
        for col in df.columns:
            if col.strip().lower() in ("symbol", "ticker", "symbols"):
                return df[col].dropna().astype(str).str.strip().str.upper().tolist()
        return df.iloc[:, 0].dropna().astype(str).str.strip().str.upper().tolist()
    elif universe_choice == "พิมพ์เอง" and typed_symbols.strip():
        raw = typed_symbols.replace(",", "\n").split("\n")
        return [s.strip().upper() for s in raw if s.strip()]
    return []


# ══════════════════════════════════════════════════════
#  RUN BACKTEST
# ══════════════════════════════════════════════════════
if run_btn:
    symbols = get_symbols()
    if not symbols:
        st.error("⚠️ ยังไม่มี symbol")
        st.stop()
    if limit_n > 0:
        symbols = symbols[:int(limit_n)]

    # ── V4: Survivorship Bias Warning ─────────────────
    st.warning(
        "⚠️ **Survivorship Bias:** Universe นี้ใช้หุ้นที่ยังอยู่ในดัชนี **ณ วันนี้** เท่านั้น "
        "หุ้นที่เคยอยู่แต่ถูกถอดออกหรือล้มละลายไปแล้วไม่ถูกนับ "
        "ผลลัพธ์จึงอาจดีกว่าความเป็นจริงในอดีต — **ใช้ตัวเลขนี้เป็น upper bound ครับ**"
    )

    # ── โหลด Benchmark ────────────────────────────────
    benchmark_df = None
    if use_regime:
        with st.spinner(f"กำลังโหลด benchmark {benchmark_symbol}..."):
            benchmark_df = load_benchmark(benchmark_symbol, period, int(regime_ema_period))
        if benchmark_df is None or benchmark_df.empty:
            st.warning(f"⚠️ โหลด {benchmark_symbol} ไม่สำเร็จ — รันโดยไม่ใช้ Regime Filter")
            benchmark_df = None
        else:
            bull_pct = benchmark_df["is_bull"].mean() * 100
            st.info(
                f"📊 Benchmark **{benchmark_symbol}** โหลดสำเร็จ — "
                f"ตลาดเป็น **bull {bull_pct:.1f}%** ของช่วงเวลา ({period})"
            )

    st.info(f"กำลัง backtest {len(symbols)} ตัว... (period: {period}, fee: {fee_pct}% ต่อข้าง)")

    progress = st.progress(0.0)
    status = st.empty()

    all_trades = []
    errors = []

    if use_concurrent:
        with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
            futures = {
                ex.submit(backtest_symbol, sym, period,
                          float(threshold_pct), float(master_law_pct),
                          float(min_risk_pct), float(fee_pct), benchmark_df): sym
                for sym in symbols
            }
            done = 0
            for fut in as_completed(futures):
                sym, trades, st_status = fut.result()
                if st_status == "ok":
                    all_trades.extend(trades)
                else:
                    errors.append(f"{sym} ({st_status})")
                done += 1
                progress.progress(done / len(symbols))
                status.text(f"[{done}/{len(symbols)}] {sym}")
    else:
        for i, sym in enumerate(symbols):
            status.text(f"[{i+1}/{len(symbols)}] {sym}...")
            sym, trades, st_status = backtest_symbol(
                sym, period,
                float(threshold_pct), float(master_law_pct),
                float(min_risk_pct), float(fee_pct), benchmark_df
            )
            if st_status == "ok":
                all_trades.extend(trades)
            else:
                errors.append(f"{sym} ({st_status})")
            progress.progress((i + 1) / len(symbols))

    progress.empty()
    status.empty()

    df_trades = pd.DataFrame(all_trades)
    if not df_trades.empty:
        df_trades = df_trades.drop(columns=["_exit_idx_in_df"], errors="ignore")
        df_trades["entry_date"] = pd.to_datetime(df_trades["entry_date"])
        df_trades["exit_date"] = pd.to_datetime(df_trades["exit_date"])
        df_trades = df_trades.sort_values("entry_date").reset_index(drop=True)

    st.session_state.trades_df = df_trades
    st.session_state.scan_errors = errors
    st.session_state.scan_summary = {
        "n_symbols": len(symbols),
        "n_ok": len(symbols) - len(errors),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "fee_pct": fee_pct,
    }

    n_open = (df_trades["exit_reason"] == "still_open").sum() if not df_trades.empty else 0
    n_closed = len(df_trades) - n_open
    st.success(
        f"✅ Backtest เสร็จ! เจอ **{len(df_trades):,} trades** จาก {len(symbols)} ตัว "
        f"(ปิดแล้ว {n_closed:,} | ยังเปิด {n_open:,})"
    )


# ══════════════════════════════════════════════════════
#  ANALYTICS
# ══════════════════════════════════════════════════════
def stats_block(df):
    """คำนวณสถิติจาก Closed trades เท่านั้น"""
    if df.empty:
        return {"n": 0, "win_rate": 0, "avg_R": 0, "median_R": 0,
                "pf": 0, "max_R": 0, "min_R": 0,
                "avg_win": 0, "avg_loss": 0,
                "p25": 0, "p75": 0, "p95": 0, "p05": 0,
                "t_stat": None, "p_value": None}

    wins = df[df["R"] > 0]["R"]
    losses = df[df["R"] <= 0]["R"]
    profit_sum = wins.sum() if len(wins) > 0 else 0
    loss_sum = abs(losses.sum()) if len(losses) > 0 else 0
    pf = (profit_sum / loss_sum) if loss_sum > 0 else float("inf")

    # t-test
    t_stat, p_value = (None, None)
    if len(df) >= 30:
        t_stat, p_value = scipy_stats.ttest_1samp(df["R"].values, 0)

    return {
        "n": len(df),
        "win_rate": (df["R"] > 0).mean(),
        "avg_R": df["R"].mean(),
        "median_R": df["R"].median(),
        "pf": pf,
        "max_R": df["R"].max(),
        "min_R": df["R"].min(),
        "avg_win": wins.mean() if len(wins) > 0 else 0,
        "avg_loss": losses.mean() if len(losses) > 0 else 0,
        "p05": df["R"].quantile(0.05),
        "p25": df["R"].quantile(0.25),
        "p75": df["R"].quantile(0.75),
        "p95": df["R"].quantile(0.95),
        "t_stat": t_stat,
        "p_value": p_value,
    }


# ══════════════════════════════════════════════════════
#  EXECUTIVE SUMMARY
# ══════════════════════════════════════════════════════
def show_executive_summary(stats, n_open, n_total, universe):
    st.subheader("📋 Executive Summary")

    col1, col2, col3 = st.columns(3)

    # ── ข้อ 1: มี Edge จริงไหม? ──────────────────────
    with col1:
        st.markdown("#### 1️⃣ มี Edge จริงไหม?")
        if stats["n"] == 0:
            st.error("ไม่มีข้อมูลเพียงพอ")
        elif stats["n"] < 30:
            st.warning(
                f"⚠️ **สรุปไม่ได้ครับ**\n\n"
                f"มีเพียง **{stats['n']} trades** (ต้องการอย่างน้อย 30)\n\n"
                f"Expectancy = {stats['avg_R']:+.3f}R แต่อาจเป็นโชคล้วนๆ"
            )
        else:
            avg = stats["avg_R"]
            p = stats["p_value"]
            p_str = f"{p:.4f}" if p is not None else "N/A"

            if avg > 0 and p is not None and p < 0.05:
                st.success(
                    f"✅ **มี Edge จริงครับ**\n\n"
                    f"Expectancy = **{avg:+.3f}R** ต่อ trade\n\n"
                    f"p-value = {p_str} → มั่นใจ 95%+"
                )
            elif avg > 0 and p is not None and p < 0.10:
                st.warning(
                    f"🟡 **มี Edge แต่ยังอ่อน**\n\n"
                    f"Expectancy = **{avg:+.3f}R** ต่อ trade\n\n"
                    f"p-value = {p_str} → มั่นใจ ~90%"
                )
            elif avg > 0:
                st.warning(
                    f"🟡 **Avg R บวก แต่ไม่มีนัยสำคัญ**\n\n"
                    f"Expectancy = **{avg:+.3f}R** ต่อ trade\n\n"
                    f"p-value = {p_str} → อาจเป็นโชค"
                )
            else:
                st.error(
                    f"🔴 **ไม่มี Edge**\n\n"
                    f"Expectancy = **{avg:+.3f}R** ต่อ trade\n\n"
                    f"p-value = {p_str}"
                )

    # ── ข้อ 2: Edge มาจากไหน? ────────────────────────
    with col2:
        st.markdown("#### 2️⃣ Edge มาจากไหน?")
        if stats["n"] == 0:
            st.info("ไม่มีข้อมูล")
        else:
            avg = stats["avg_R"]
            med = stats["median_R"]
            diff = abs(avg - med)

            if diff > 1.0 and avg > med:
                st.warning(
                    f"⚠️ **Edge มาจาก Outlier**\n\n"
                    f"Avg R = {avg:+.3f}R แต่ Median = {med:+.3f}R\n\n"
                    f"กำไรกระจุกอยู่ใน trade ดีๆ ไม่กี่ตัว — "
                    f"ถ้า trade พวกนั้นไม่เกิดซ้ำ Edge อาจหายไป"
                )
            elif avg > 0 and med > 0:
                st.success(
                    f"✅ **Edge กระจายสม่ำเสมอ**\n\n"
                    f"Avg R = {avg:+.3f}R | Median = {med:+.3f}R\n\n"
                    f"ทั้งสองค่าเป็นบวก — Edge ไม่ได้พึ่ง outlier"
                )
            else:
                st.error(
                    f"🔴 **Median ติดลบ**\n\n"
                    f"Avg R = {avg:+.3f}R | Median = {med:+.3f}R\n\n"
                    f"trade ส่วนใหญ่ขาดทุน — กำไรรวมมาจากไม่กี่ตัว"
                )

    # ── ข้อ 3: เชื่อได้แค่ไหน? ───────────────────────
    with col3:
        st.markdown("#### 3️⃣ เชื่อได้แค่ไหน?")
        issues = []
        score = 100

        if stats["n"] < 30:
            issues.append(f"❌ Trades น้อยเกินไป ({stats['n']} < 30)")
            score -= 40
        elif stats["n"] < 100:
            issues.append(f"⚠️ Trades ยังน้อย ({stats['n']} trades)")
            score -= 15

        open_pct = n_open / n_total * 100 if n_total > 0 else 0
        if open_pct > 10:
            issues.append(f"⚠️ Still Open {open_pct:.0f}% — ยังไม่จบจริง")
            score -= 20

        issues.append(f"⚠️ Survivorship Bias — Universe = หุ้นที่รอดแล้ว")
        score -= 15

        score = max(0, score)

        if score >= 70:
            st.success(f"✅ **เชื่อได้ระดับดี** (Score: {score}/100)\n\n" + "\n\n".join(issues))
        elif score >= 40:
            st.warning(f"🟡 **เชื่อได้บางส่วน** (Score: {score}/100)\n\n" + "\n\n".join(issues))
        else:
            st.error(f"🔴 **ควรระวัง** (Score: {score}/100)\n\n" + "\n\n".join(issues))


# ══════════════════════════════════════════════════════
#  DISPLAY HELPERS
# ══════════════════════════════════════════════════════
def show_stats_cards_main(stats):
    if stats["n"] == 0:
        st.info("ไม่มี trade")
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Closed Trades", f"{stats['n']:,}")
    c2.metric("Win Rate", f"{stats['win_rate']:.1%}")
    c3.metric("Expectancy (avg R)", f"{stats['avg_R']:.3f}",
              delta=f"{'+' if stats['avg_R']>0 else ''}{stats['avg_R']:.2f}")
    c4.metric("Profit Factor", f"{stats['pf']:.2f}" if stats["pf"] != float("inf") else "∞")
    c5.metric("Median R", f"{stats['median_R']:.3f}",
              help="ค่ากลาง — ตัด outlier แล้ว ใกล้ความจริงมากกว่า avg")


def show_stats_cards_extra(stats):
    if stats["n"] == 0:
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Avg Win", f"{stats['avg_win']:.2f}R")
    c2.metric("Avg Loss", f"{stats['avg_loss']:.2f}R")
    c3.metric("Max R", f"{stats['max_R']:.1f}")
    c4.metric("Min R", f"{stats['min_R']:.1f}")
    c5.metric("P95 / P05", f"{stats['p95']:.1f} / {stats['p05']:.1f}")


def show_ttest_result(stats):
    """แสดงผล t-test แบบอ่านง่าย"""
    if stats["n"] < 30:
        st.warning(
            f"⚠️ **t-test:** ต้องการ trade อย่างน้อย 30 ตัว "
            f"— ตอนนี้มีแค่ {stats['n']} ตัว ยังสรุปไม่ได้ครับ"
        )
        return

    t = stats["t_stat"]
    p = stats["p_value"]
    if t is None or p is None:
        return

    if p < 0.01:
        st.success(
            f"✅ **t-test: p = {p:.4f}** → มั่นใจ 99%+ ว่า Edge จริง ไม่ใช่โชค "
            f"(t = {t:.2f}, n = {stats['n']})"
        )
    elif p < 0.05:
        st.success(
            f"✅ **t-test: p = {p:.4f}** → มั่นใจ 95%+ ว่า Edge จริง "
            f"(t = {t:.2f}, n = {stats['n']})"
        )
    elif p < 0.10:
        st.warning(
            f"🟡 **t-test: p = {p:.4f}** → มั่นใจ ~90% — Edge อยู่แต่ยังอ่อน "
            f"(t = {t:.2f}, n = {stats['n']})"
        )
    else:
        st.error(
            f"🔴 **t-test: p = {p:.4f}** → ไม่มีนัยสำคัญทางสถิติ "
            f"ผลนี้อาจเกิดจากโชคล้วนๆ (t = {t:.2f}, n = {stats['n']})"
        )


def plot_r_distribution(df, x_min, x_max, bin_size):
    R = df["R"].values
    n_below = int((R < x_min).sum())
    n_above = int((R > x_max).sum())
    n_in_range = int(((R >= x_min) & (R <= x_max)).sum())
    R_in_range = R[(R >= x_min) & (R <= x_max)]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=R_in_range, name="Trades",
        marker_color="#5DCAA5", opacity=0.85,
        xbins=dict(start=x_min, end=x_max, size=bin_size),
        hovertemplate="R: %{x}<br>จำนวน: %{y}<extra></extra>",
    ))
    fig.add_vline(x=0, line_dash="dash", line_color="gray",
                  annotation_text="Break-even", annotation_position="top")

    avg_R = df["R"].mean()
    median_R = df["R"].median()
    if x_min <= avg_R <= x_max:
        fig.add_vline(x=avg_R, line_dash="dot", line_color="#E24B4A",
                      annotation_text=f"Avg = {avg_R:.2f}R", annotation_position="top")
    if x_min <= median_R <= x_max:
        fig.add_vline(x=median_R, line_dash="dashdot", line_color="#185FA5",
                      annotation_text=f"Median = {median_R:.2f}R", annotation_position="bottom")

    annotations = []
    if n_below > 0:
        annotations.append(dict(
            x=x_min, y=1.05, xref="x", yref="paper",
            text=f"⬅ มี {n_below:,} trades ที่ R < {x_min}<br>(ต่ำสุด: {df['R'].min():.1f})",
            showarrow=False, bgcolor="rgba(226,75,74,0.15)",
            bordercolor="#E24B4A", borderwidth=1,
            font=dict(size=11, color="#A32D2D"), align="left",
        ))
    if n_above > 0:
        annotations.append(dict(
            x=x_max, y=1.05, xref="x", yref="paper",
            text=f"มี {n_above:,} trades ที่ R > {x_max} ➡<br>(สูงสุด: {df['R'].max():.1f})",
            showarrow=False, bgcolor="rgba(99,153,34,0.15)",
            bordercolor="#639922", borderwidth=1,
            font=dict(size=11, color="#3B6D11"), align="right",
        ))

    fig.update_layout(
        title=dict(
            text=f"R-Distribution — Closed Trades (แสดง {n_in_range:,} จาก {len(df):,} trades)",
            font=dict(size=18),
        ),
        xaxis_title="R-Multiple (หลังหัก Fee แล้ว)",
        yaxis_title="จำนวน Trades",
        height=480, annotations=annotations,
        margin=dict(t=100), showlegend=False,
    )
    return fig


def plot_equity_curve(df):
    df_sorted = df.sort_values("exit_date").copy()
    df_sorted["cum_R"] = df_sorted["R"].cumsum()
    cum_max = df_sorted["cum_R"].cummax()
    drawdown = df_sorted["cum_R"] - cum_max
    max_dd = drawdown.min()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_sorted["exit_date"], y=df_sorted["cum_R"],
        mode="lines", name="Cumulative R",
        line=dict(color="#185FA5", width=2),
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(
        title=f"Equity Curve — Closed Trades | Final: {df_sorted['cum_R'].iloc[-1]:.1f}R | Max DD: {max_dd:.1f}R",
        xaxis_title="Exit Date", yaxis_title="Cumulative R", height=400,
    )
    return fig


def plot_sl_type_breakdown(df):
    grp = df.groupby("sl_type").agg(
        n=("R", "count"),
        avg_R=("R", "mean"),
        win_rate=("R", lambda x: (x > 0).mean()),
    ).reset_index().sort_values("n", ascending=False)
    return grp


def regime_comparison(df):
    rows = []
    for regime in ["bull", "bear", "unknown"]:
        sub = df[df["regime"] == regime]
        if sub.empty:
            continue
        wins = sub[sub["R"] > 0]["R"]
        losses = sub[sub["R"] <= 0]["R"]
        pf_num = wins.sum() if len(wins) else 0
        pf_den = abs(losses.sum()) if len(losses) else 0
        pf = pf_num / pf_den if pf_den > 0 else float("inf")
        rows.append({
            "Regime": regime, "Trades": len(sub),
            "Win Rate": (sub["R"] > 0).mean(),
            "Avg R": sub["R"].mean(), "Median R": sub["R"].median(),
            "Profit Factor": pf, "Total R": sub["R"].sum(),
        })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════
df = st.session_state.trades_df

if df.empty:
    st.info("👈 ตั้งค่าใน sidebar แล้วกด **Run Backtest** เพื่อเริ่มต้นครับ")
    st.markdown("""
    ### Logic สรุป
    **Entry** — Fresh Today (Peacock เต็มวันแรก) → Buy at next Open

    **Initial SL (วัน entry)**
    - ใช้ Open ของ T+1 ตัดสินใจ SL (รวม overnight gap แล้ว)
    - ถ้า (Open_T+1 − EMA200)/EMA200 > 3% → ใช้ EMA10
    - ถ้า ≤ 3% → ใช้ EMA200
    - **Master Law (ขอบบน):** SL ห่างจาก entry ห้ามเกิน 3%
    - **Min Risk (ขอบล่าง):** SL ห่างจาก entry ห้ามต่ำกว่า 1%

    **Trailing** — ทุกวัน: Stop = max(SL_initial, EMA20) → EXIT เมื่อ Close < Stop

    **R-Multiple** = (exit − entry) / risk_unit − fee_R
    """)
else:
    # ── แยก Closed vs Still Open ──────────────────────
    df_closed = df[df["exit_reason"] != "still_open"].copy()
    df_open = df[df["exit_reason"] == "still_open"].copy()
    n_open = len(df_open)
    n_total = len(df)

    # ── Still Open Warning ────────────────────────────
    if n_open > 0:
        open_pct = n_open / n_total * 100
        st.info(
            f"📌 มี **{n_open:,} trades ยังเปิดอยู่** ({open_pct:.1f}% ของทั้งหมด) "
            f"— R ยังไม่จบ ไม่นับในสถิติหลัก | "
            f"R ลอยๆ เฉลี่ย = {df_open['R'].mean():+.2f}R"
        )

    # ── Regime Filter View ────────────────────────────
    df_work = df_closed  # ใช้ closed เป็น base เสมอ

    has_regime = "regime" in df_work.columns and df_work["regime"].nunique() > 1
    if has_regime:
        st.divider()
        st.subheader("🎯 Market Regime View")
        regime_view = st.radio(
            "เลือกมุมมอง",
            ["🌐 ทั้งหมด (All)", "🟢 Bull only (Filter ON)", "🔴 Bear only", "⚖️ เทียบ Bull vs Bear"],
            horizontal=True,
        )
        if regime_view == "🟢 Bull only (Filter ON)":
            df_work = df_closed[df_closed["regime"] == "bull"]
        elif regime_view == "🔴 Bear only":
            df_work = df_closed[df_closed["regime"] == "bear"]
        elif regime_view == "⚖️ เทียบ Bull vs Bear":
            cmp_df = regime_comparison(df_closed)
            cmp_show = cmp_df.copy()
            cmp_show["Win Rate"] = cmp_show["Win Rate"].apply(lambda x: f"{x:.1%}")
            cmp_show["Avg R"] = cmp_show["Avg R"].apply(lambda x: f"{x:+.3f}")
            cmp_show["Median R"] = cmp_show["Median R"].apply(lambda x: f"{x:+.3f}")
            cmp_show["Profit Factor"] = cmp_show["Profit Factor"].apply(
                lambda x: f"{x:.2f}" if x != float("inf") else "∞"
            )
            cmp_show["Total R"] = cmp_show["Total R"].apply(lambda x: f"{x:+.1f}R")
            st.dataframe(cmp_show, use_container_width=True, hide_index=True)

            bull = cmp_df[cmp_df["Regime"] == "bull"]
            bear = cmp_df[cmp_df["Regime"] == "bear"]
            if len(bull) > 0 and len(bear) > 0:
                bull_R = float(bull.iloc[0]["Avg R"])
                bear_R = float(bear.iloc[0]["Avg R"])
                diff = bull_R - bear_R
                if diff > 0.1:
                    st.success(f"✅ Bull Edge ({bull_R:+.2f}R) > Bear Edge ({bear_R:+.2f}R) = Filter ได้ผล ({diff:+.2f}R)")
                elif diff < -0.1:
                    st.warning(f"⚠️ Bear Edge ({bear_R:+.2f}R) > Bull ({bull_R:+.2f}R) — Filter ทำให้แย่ลง")
                else:
                    st.info(f"➖ Bull vs Bear ใกล้กัน ({bull_R:+.2f}R vs {bear_R:+.2f}R) — Filter ไม่ได้ช่วยเพิ่ม Edge")

        if regime_view not in ["🌐 ทั้งหมด (All)", "⚖️ เทียบ Bull vs Bear"]:
            removed = len(df_closed) - len(df_work)
            st.caption(f"แสดง {len(df_work):,} จาก {len(df_closed):,} closed trades (กรองออก {removed:,})")

    # ── คำนวณ stats จาก Closed trades ────────────────
    stats = stats_block(df_work)

    # ══════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY (ด้านบนสุด)
    # ══════════════════════════════════════════════════
    st.divider()
    show_executive_summary(stats, n_open, n_total, universe_choice)

    # ══════════════════════════════════════════════════
    #  OVERALL PERFORMANCE
    # ══════════════════════════════════════════════════
    st.divider()
    st.subheader("📊 Overall Performance")
    st.caption("📌 คำนวณจาก **Closed Trades เท่านั้น** — Still Open แยกออกแล้ว")
    show_stats_cards_main(stats)
    st.write("")
    show_stats_cards_extra(stats)

    st.write("")
    show_ttest_result(stats)

    if stats["n"] > 0:
        if abs(stats["avg_R"] - stats["median_R"]) > 1.0:
            st.info(
                f"💡 Avg R ({stats['avg_R']:.2f}) ห่างจาก Median R ({stats['median_R']:.2f}) เยอะ "
                f"→ Edge กระจุกอยู่ใน outlier ไม่กี่ตัว"
            )

    # ══════════════════════════════════════════════════
    #  R-DISTRIBUTION
    # ══════════════════════════════════════════════════
    st.divider()
    st.subheader("📈 R-Distribution")

    preset_cols = st.columns(5)
    presets = [
        ("Zoom in (±3R)", -3.0, 3.0),
        ("Normal (-5 ถึง +10)", -5.0, 10.0),
        ("Wide (-10 ถึง +20)", -10.0, 20.0),
        ("Full (Min-Max)", float(np.floor(stats["min_R"])) if stats["n"] > 0 else -5.0,
                           float(np.ceil(stats["max_R"])) if stats["n"] > 0 else 10.0),
        ("Custom 👇", None, None),
    ]
    for i, (label, lo, hi) in enumerate(presets):
        with preset_cols[i]:
            if st.button(label, use_container_width=True, key=f"preset_{i}"):
                if lo is not None:
                    st.session_state["x_min_input"] = lo
                    st.session_state["x_max_input"] = hi

    col_a, col_b, col_c = st.columns([1, 1, 1])
    with col_a:
        x_min = st.number_input("R Min (ขอบซ้าย)", value=st.session_state.get("x_min_input", -5.0),
                                step=0.5, format="%.1f", key="x_min_input")
    with col_b:
        x_max = st.number_input("R Max (ขอบขวา)", value=st.session_state.get("x_max_input", 10.0),
                                step=0.5, format="%.1f", key="x_max_input")
    with col_c:
        bin_size = st.selectbox("ขนาด Bin", [0.25, 0.5, 1.0], index=1)

    if x_min >= x_max:
        st.warning("⚠️ R Min ต้องน้อยกว่า R Max — ใช้ค่า default")
        x_min, x_max = -5.0, 10.0

    if not df_work.empty:
        st.plotly_chart(plot_r_distribution(df_work, x_min, x_max, bin_size), use_container_width=True)

    # ══════════════════════════════════════════════════
    #  EQUITY CURVE
    # ══════════════════════════════════════════════════
    st.divider()
    st.subheader("💰 Equity Curve")
    st.caption("📌 Closed Trades เท่านั้น")
    if not df_work.empty:
        st.plotly_chart(plot_equity_curve(df_work), use_container_width=True)

    # ══════════════════════════════════════════════════
    #  SL TYPE BREAKDOWN
    # ══════════════════════════════════════════════════
    st.divider()
    st.subheader("🎯 SL Type Breakdown")
    if not df_work.empty:
        sl_df = plot_sl_type_breakdown(df_work)
        sl_show = sl_df.copy()
        sl_show["win_rate"] = sl_show["win_rate"].apply(lambda x: f"{x:.1%}")
        sl_show["avg_R"] = sl_show["avg_R"].apply(lambda x: f"{x:.3f}")
        sl_show.columns = ["SL Type", "Trades", "Avg R", "Win Rate"]
        st.dataframe(sl_show, use_container_width=True, hide_index=True)
        st.caption("🟢 EMA10 = ลอยสูง > threshold | 🔵 EMA200 = ใกล้ EMA200 | 🟡 +ML = ติด Master Law | 🟠 +MinR = ติด Min Risk floor")

    # ══════════════════════════════════════════════════
    #  STILL OPEN SECTION
    # ══════════════════════════════════════════════════
    if n_open > 0:
        st.divider()
        with st.expander(f"📌 Still Open Trades ({n_open:,} ตัว) — R ลอยๆ ยังไม่จบ"):
            st.caption("⚠️ trade เหล่านี้ยังไม่โดน SL — R ที่แสดงเป็นแค่ mark-to-market ณ วันสุดท้ายของข้อมูล")
            cols_open = ["symbol", "entry_date", "days_held", "entry_price",
                         "exit_price", "sl_initial", "risk_pct", "R_gross", "R"]
            df_open_show = df_open[[c for c in cols_open if c in df_open.columns]].copy()
            df_open_show["entry_date"] = pd.to_datetime(df_open_show["entry_date"]).dt.strftime("%Y-%m-%d")
            st.dataframe(df_open_show, use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════
    #  TRADE LOG
    # ══════════════════════════════════════════════════
    st.divider()
    st.subheader("📋 Trade Log (Closed Trades)")
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    with fc1:
        sym_filter = st.text_input("🔍 ค้นหา symbol", "")
    with fc2:
        sl_filter = st.selectbox(
            "SL Type",
            ["All"] + sorted(df_work["sl_type"].unique().tolist()) if not df_work.empty else ["All"],
        )
    with fc3:
        sort_by = st.selectbox(
            "เรียงโดย",
            ["entry_date (ล่าสุด)", "R (มากสุด)", "R (น้อยสุด)", "days_held (นานสุด)"],
        )

    df_show = df_work.copy()
    if sym_filter:
        df_show = df_show[df_show["symbol"].str.contains(sym_filter.upper(), na=False)]
    if sl_filter != "All":
        df_show = df_show[df_show["sl_type"] == sl_filter]

    sort_map = {
        "entry_date (ล่าสุด)": ("entry_date", False),
        "R (มากสุด)": ("R", False),
        "R (น้อยสุด)": ("R", True),
        "days_held (นานสุด)": ("days_held", False),
    }
    col_s, asc_s = sort_map[sort_by]
    df_show = df_show.sort_values(col_s, ascending=asc_s)

    cols_base = ["symbol", "entry_date", "exit_date", "days_held",
                 "entry_price", "exit_price", "sl_initial", "sl_type",
                 "risk_pct", "R_gross", "fee_R", "R", "exit_reason"]
    if "regime" in df_show.columns:
        cols_base.insert(1, "regime")

    df_display = df_show[[c for c in cols_base if c in df_show.columns]].copy()
    df_display["entry_date"] = pd.to_datetime(df_display["entry_date"]).dt.strftime("%Y-%m-%d")
    df_display["exit_date"] = pd.to_datetime(df_display["exit_date"]).dt.strftime("%Y-%m-%d")

    st.dataframe(df_display, use_container_width=True, height=400, hide_index=True)
    st.caption(f"แสดง {len(df_display):,} จาก {len(df_work):,} closed trades")

    csv = df_display.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        f"💾 Download Trade Log ({len(df_display):,} trades)",
        data=csv,
        file_name=f"peacock_backtest_v4_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

    if st.session_state.scan_errors:
        with st.expander(f"⚠️ Symbols ที่ดึงข้อมูลไม่สำเร็จ ({len(st.session_state.scan_errors)})"):
            st.write(", ".join(st.session_state.scan_errors[:50]))


# ══════════════════════════════════════════════════════
#  FOOTER
# ══════════════════════════════════════════════════════
st.divider()
with st.expander("ℹ️ เกี่ยวกับ Backtest"):
    st.markdown("""
    **R-Multiple** — วัดผลแต่ละ trade เป็นจำนวน "เท่าของความเสี่ยง"
    - R = -1 → Stop Loss เต็ม | R = 0 → Break-even | R = +3 → กำไร 3 เท่าของความเสี่ยง

    **Expectancy** = ค่าเฉลี่ย R ต่อ trade (หลังหัก Fee) — ถ้า > 0 = มี Edge

    **Profit Factor** = (กำไรรวม) / (ขาดทุนรวม) — > 1.5 = ดี, > 2 = ดีมาก

    **t-test** — ทดสอบว่า Expectancy ที่ได้เกิดจาก Edge จริง หรือโชคล้วนๆ
    - p < 0.05 = มั่นใจ 95%+ | p < 0.01 = มั่นใจ 99%+

    **ข้อจำกัด**
    - Fee คิดแบบ round-trip ต่อ trade (กรอกได้ใน sidebar)
    - Still Open trades ไม่นับในสถิติหลัก
    - Survivorship Bias: Universe ปัจจุบันไม่สะท้อนความจริงในอดีต
    - ทุก trade = 1R เท่ากัน (ไม่มี compounding)
    """)
