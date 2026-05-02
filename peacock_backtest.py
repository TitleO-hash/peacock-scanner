"""
🦚 Peacock Backtest — R-Multiple Edge Validator
==================================================
Backtest กลยุทธ์ Peacock เพื่อพิสูจน์ว่ามี Edge จริงหรือไม่

ENTRY:
  - Fresh Today (Peacock เต็มวันแรก) → Buy at next Open (T+1)
  - แยก 2 กลุ่ม:
    * Group A (Fresh from below) — ไม่เคย Peacock ครบใน 90 วันก่อน → Edge สูง
    * Group B (Fresh from pullback) — เคย → Edge น้อย

EXIT:
  - Initial SL (วัน entry):
    * ถ้า (Close − EMA200)/EMA200 > 3% → SL = max(EMA10, entry × 0.97)
    * ถ้า ≤ 3% → SL = max(EMA200, entry × 0.97)
    * Master Law: SL ห่างจาก entry ไม่เกิน 3%
  - Trailing: ทุกวัน Stop = max(SL_initial, EMA20)
    EXIT เมื่อ Close < Stop → ขายที่ next Open

R-Multiple = (exit − entry) / (entry − SL_initial)

วิธีรัน:
  pip install streamlit yfinance pandas plotly
  streamlit run peacock_backtest.py
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
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
        "เลือกตลาด",
        ["S&P 500", "SET100", "อัพโหลด CSV", "พิมพ์เอง"],
        index=0,
    )

    uploaded_file = None
    typed_symbols = ""
    if universe_choice == "อัพโหลด CSV":
        uploaded_file = st.file_uploader("CSV (มีคอลัมน์ Symbol)", type=["csv"])
    elif universe_choice == "พิมพ์เอง":
        typed_symbols = st.text_area(
            "พิมพ์ symbol คั่นด้วย comma",
            placeholder="AAPL, MSFT, NVDA",
            height=80,
        )

    limit_n = st.number_input(
        "จำกัดจำนวน symbol (0 = ทั้งหมด)",
        min_value=0, max_value=2000, value=50, step=10,
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
        help="(Close−EMA200)/EMA200 เกินค่านี้ → ใช้ EMA10 SL"
    )

    lookback_days = st.number_input(
        "Lookback Group A vs B (วัน)",
        min_value=30, max_value=365, value=90, step=10,
        help="ไม่เคย Peacock ครบในช่วงนี้ → Group A (สด)"
    )

    master_law_pct = st.number_input(
        "Master Law: Max Risk per Trade (%)",
        min_value=1.0, max_value=10.0, value=3.0, step=0.5,
        help="SL ห่างจาก entry ห้ามเกินค่านี้"
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


def add_emas(df):
    """เพิ่ม EMA 5 เส้นใน dataframe"""
    df = df.copy()
    df["EMA10"] = calc_ema(df["Close"], 10)
    df["EMA20"] = calc_ema(df["Close"], 20)
    df["EMA35"] = calc_ema(df["Close"], 35)
    df["EMA75"] = calc_ema(df["Close"], 75)
    df["EMA200"] = calc_ema(df["Close"], 200)
    return df


def is_peacock_series(df):
    """
    Vectorized check: returns Series of bool
    Peacock = Close > EMA10 > EMA20 > EMA35 > EMA75 และ Close > EMA200
    """
    c = df["Close"]
    return (
        (c > df["EMA10"])
        & (df["EMA10"] > df["EMA20"])
        & (df["EMA20"] > df["EMA35"])
        & (df["EMA35"] > df["EMA75"])
        & (c > df["EMA200"])
    )


def find_fresh_signals(df):
    """หา index ของ Fresh Today: วันนี้ผ่าน Peacock + เมื่อวานไม่ผ่าน"""
    peacock = is_peacock_series(df)
    fresh = peacock & ~peacock.shift(1, fill_value=False)
    return np.where(fresh.values)[0]


def categorize_group(df, signal_idx, lookback):
    """
    A = ไม่เคย Peacock ครบใน lookback วันก่อน → Fresh from below (Edge สูง)
    B = เคย → Fresh from pullback (Edge น้อย)
    """
    start = max(0, signal_idx - lookback)
    if start == signal_idx:
        return "A"  # ข้อมูลไม่พอ ถือเป็น A
    window = is_peacock_series(df.iloc[start:signal_idx])
    return "B" if window.any() else "A"


def simulate_trade(df, signal_idx, threshold_pct, master_law_pct):
    """
    จำลอง 1 trade ตาม Peacock rules
    Return: dict ของ trade หรือ None ถ้า invalid
    """
    if signal_idx + 1 >= len(df):
        return None  # ไม่มีแท่งถัดไปให้เข้า

    # ── Entry ที่ Open T+1 ──
    entry_idx = signal_idx + 1
    entry_bar = df.iloc[entry_idx]
    entry_price = float(entry_bar["Open"])
    entry_date = entry_bar.name

    # ── Get EMAs ที่วัน signal (ใช้คำนวณ SL) ──
    signal_bar = df.iloc[signal_idx]
    ema10 = float(signal_bar["EMA10"])
    ema200 = float(signal_bar["EMA200"])

    # ── Distance from EMA200 ──
    distance = (entry_price - ema200) / ema200

    # ── Initial SL ──
    if distance > threshold_pct / 100:
        sl_candidate = ema10  # ลอยสูง → ใช้ EMA10
        sl_type = "EMA10"
    else:
        sl_candidate = ema200  # ใกล้ → ใช้ EMA200
        sl_type = "EMA200"

    # Master Law cap: SL ห่าง entry ไม่เกิน X%
    sl_floor = entry_price * (1 - master_law_pct / 100)
    sl_initial = max(sl_candidate, sl_floor)
    if sl_initial == sl_floor and sl_floor > sl_candidate:
        sl_type = f"{sl_type}+ML"  # capped by Master Law

    # Sanity: SL ต้องอยู่ใต้ entry
    if sl_initial >= entry_price:
        return None

    risk_unit = entry_price - sl_initial

    # ── Walk forward ──
    for i in range(entry_idx, len(df)):
        bar = df.iloc[i]
        ema20_i = float(bar["EMA20"])
        # Trailing stop = max(SL_initial, EMA20 ของวันนี้)
        stop = max(sl_initial, ema20_i)

        if float(bar["Close"]) < stop:
            # Exit ที่ next Open (หรือ Close ของวันสุดท้ายถ้าหมด data)
            if i + 1 < len(df):
                exit_idx = i + 1
                exit_bar = df.iloc[exit_idx]
                exit_price = float(exit_bar["Open"])
            else:
                exit_idx = i
                exit_bar = bar
                exit_price = float(exit_bar["Close"])

            R = (exit_price - entry_price) / risk_unit
            return {
                "entry_date": entry_date,
                "exit_date": exit_bar.name,
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "sl_initial": round(sl_initial, 4),
                "sl_type": sl_type,
                "risk_pct": round(risk_unit / entry_price * 100, 2),
                "R": round(R, 3),
                "days_held": exit_idx - entry_idx,
                "exit_reason": "trailing/SL",
                "_exit_idx_in_df": exit_idx,
            }

    # Trade ยังเปิดอยู่ที่สิ้นข้อมูล — mark to market
    last_bar = df.iloc[-1]
    R = (float(last_bar["Close"]) - entry_price) / risk_unit
    return {
        "entry_date": entry_date,
        "exit_date": last_bar.name,
        "entry_price": round(entry_price, 4),
        "exit_price": round(float(last_bar["Close"]), 4),
        "sl_initial": round(sl_initial, 4),
        "sl_type": sl_type,
        "risk_pct": round(risk_unit / entry_price * 100, 2),
        "R": round(R, 3),
        "days_held": len(df) - 1 - entry_idx,
        "exit_reason": "still_open",
        "_exit_idx_in_df": len(df) - 1,
    }


def backtest_symbol(symbol, period, lookback_days, threshold_pct, master_law_pct):
    """Backtest หุ้น 1 ตัว → คืน list ของ trades"""
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
            # ข้าม signal ที่ยังอยู่ใน open trade
            if idx <= last_exit_idx:
                continue
            # ต้องมี 200 แท่ง EMA history และ lookback_days ก่อน
            if idx < max(200, lookback_days):
                continue

            group = categorize_group(df, idx, lookback_days)
            trade = simulate_trade(df, idx, threshold_pct, master_law_pct)
            if trade is None:
                continue
            trade["symbol"] = symbol
            trade["group"] = group
            trades.append(trade)
            last_exit_idx = trade["_exit_idx_in_df"]

        return symbol, trades, "ok"
    except Exception as e:
        return symbol, [], f"error: {str(e)[:50]}"


# ══════════════════════════════════════════════════════
#  SYMBOL LIST LOADERS
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=86400)
def load_sp500():
    tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    syms = tables[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    return syms


@st.cache_data(ttl=86400)
def load_set100():
    tables = pd.read_html("https://en.wikipedia.org/wiki/SET100_Index")
    for table in tables:
        for col in table.columns:
            if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                syms = table[col].astype(str).str.strip().tolist()
                return [f"{s}.BK" for s in syms if s and s != "nan"]
    return []


def get_symbols():
    if universe_choice == "S&P 500":
        try:
            return load_sp500()
        except Exception as e:
            st.error(f"โหลด S&P 500 ไม่ได้: {e}")
            return []
    elif universe_choice == "SET100":
        try:
            return load_set100()
        except Exception as e:
            st.error(f"โหลด SET100 ไม่ได้: {e}")
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

    st.info(f"กำลัง backtest {len(symbols)} ตัว... (period: {period})")

    progress = st.progress(0.0)
    status = st.empty()

    all_trades = []
    errors = []

    if use_concurrent:
        with ThreadPoolExecutor(max_workers=int(max_workers)) as ex:
            futures = {
                ex.submit(backtest_symbol, sym, period, int(lookback_days),
                          float(threshold_pct), float(master_law_pct)): sym
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
                sym, period, int(lookback_days),
                float(threshold_pct), float(master_law_pct)
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
    }

    st.success(f"✅ Backtest เสร็จ! เจอ {len(df_trades)} trades จาก {len(symbols)} ตัว")


# ══════════════════════════════════════════════════════
#  ANALYTICS & VISUALIZATION
# ══════════════════════════════════════════════════════
def stats_block(df, label=""):
    """คำนวณสถิติของชุด trades"""
    if df.empty:
        return {"n": 0, "win_rate": 0, "avg_R": 0, "expectancy": 0,
                "pf": 0, "max_R": 0, "min_R": 0,
                "avg_win": 0, "avg_loss": 0}
    wins = df[df["R"] > 0]["R"]
    losses = df[df["R"] <= 0]["R"]
    profit_sum = wins.sum() if len(wins) > 0 else 0
    loss_sum = abs(losses.sum()) if len(losses) > 0 else 0
    pf = (profit_sum / loss_sum) if loss_sum > 0 else float("inf")
    return {
        "n": len(df),
        "win_rate": (df["R"] > 0).mean(),
        "avg_R": df["R"].mean(),
        "expectancy": df["R"].mean(),
        "pf": pf,
        "max_R": df["R"].max(),
        "min_R": df["R"].min(),
        "avg_win": wins.mean() if len(wins) > 0 else 0,
        "avg_loss": losses.mean() if len(losses) > 0 else 0,
    }


def show_stats_cards(stats, label=""):
    """แสดงการ์ดสถิติ"""
    if stats["n"] == 0:
        st.info(f"ไม่มี trade ในกลุ่มนี้")
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades", stats["n"])
    c2.metric("Win Rate", f"{stats['win_rate']:.1%}")
    c3.metric("Expectancy (avg R)", f"{stats['avg_R']:.3f}",
              delta=f"{'+' if stats['avg_R']>0 else ''}{stats['avg_R']:.2f}")
    c4.metric("Profit Factor", f"{stats['pf']:.2f}" if stats["pf"] != float("inf") else "∞")
    c5.metric("Max R / Min R", f"{stats['max_R']:.1f} / {stats['min_R']:.1f}")


def plot_r_distribution(df, title="R-Distribution"):
    """Histogram ของ R-Multiple แยก Group A vs B"""
    fig = go.Figure()

    # Bin: -2 ถึง +10 step 0.5
    bins = dict(start=-2, end=10, size=0.5)

    color_map = {"A": "#1D9E75", "B": "#BA7517"}
    label_map = {"A": "Group A (Fresh from below)", "B": "Group B (Fresh from pullback)"}

    for grp in ["A", "B"]:
        sub = df[df["group"] == grp]
        if sub.empty:
            continue
        fig.add_trace(go.Histogram(
            x=sub["R"],
            name=f"{label_map[grp]} (n={len(sub)})",
            marker_color=color_map[grp],
            opacity=0.7,
            xbins=bins,
        ))

    # เส้น 0
    fig.add_vline(x=0, line_dash="dash", line_color="gray", annotation_text="Break-even")
    # เส้น expectancy
    avg_R = df["R"].mean()
    fig.add_vline(x=avg_R, line_dash="dot", line_color="red",
                  annotation_text=f"Expectancy = {avg_R:.2f}R")

    fig.update_layout(
        title=title,
        xaxis_title="R-Multiple",
        yaxis_title="จำนวน Trades",
        barmode="overlay",
        height=450,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def plot_equity_curve(df):
    """Cumulative R curve"""
    df_sorted = df.sort_values("exit_date").copy()
    df_sorted["cum_R"] = df_sorted["R"].cumsum()
    df_sorted["trade_n"] = range(1, len(df_sorted) + 1)

    fig = go.Figure()

    # Per group cumulative
    for grp, color in [("A", "#1D9E75"), ("B", "#BA7517")]:
        sub = df_sorted[df_sorted["group"] == grp].copy()
        if sub.empty:
            continue
        sub["cum_R_grp"] = sub["R"].cumsum()
        fig.add_trace(go.Scatter(
            x=sub["exit_date"], y=sub["cum_R_grp"],
            mode="lines", name=f"Group {grp}",
            line=dict(color=color, width=2),
        ))

    # Combined
    fig.add_trace(go.Scatter(
        x=df_sorted["exit_date"], y=df_sorted["cum_R"],
        mode="lines", name="Combined",
        line=dict(color="#3498DB", width=3),
    ))

    fig.add_hline(y=0, line_dash="dash", line_color="gray")

    fig.update_layout(
        title="Equity Curve (Cumulative R)",
        xaxis_title="Exit Date",
        yaxis_title="Cumulative R",
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def plot_group_comparison(df):
    """Bar chart เปรียบเทียบ Group A vs B"""
    rows = []
    for grp in ["A", "B"]:
        sub = df[df["group"] == grp]
        if sub.empty:
            continue
        s = stats_block(sub)
        rows.append({
            "Group": f"{grp}",
            "Trades": s["n"],
            "Win Rate": s["win_rate"],
            "Expectancy (R)": s["avg_R"],
            "Profit Factor": s["pf"] if s["pf"] != float("inf") else 99,
            "Avg Win (R)": s["avg_win"],
            "Avg Loss (R)": s["avg_loss"],
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
    
    **Entry**
    - Fresh Today (Peacock เต็มวันแรก) → Buy at next Open
    - แยก 2 กลุ่ม:
      - 🟢 **Group A** — ไม่เคย Peacock ครบใน 90 วันก่อน (Fresh from below, Edge สูง)
      - 🟠 **Group B** — เคย Peacock ในช่วงนั้น (Fresh from pullback, Edge น้อย)
    
    **Initial SL (วัน entry)**
    - ถ้า (Close − EMA200)/EMA200 > 3% → ใช้ EMA10 cut
    - ถ้า ≤ 3% → ใช้ EMA200 cut
    - **Master Law**: SL ห่างจาก entry ห้ามเกิน 3%
    
    **Trailing**
    - ทุกวัน: Stop = max(SL_initial, EMA20 ของวันนั้น)
    - EXIT เมื่อ Close < Stop → ขายที่ next Open
    
    **R-Multiple** = (exit − entry) / (entry − SL_initial)
    """)
else:
    st.divider()
    st.subheader("📊 Overall Performance")
    show_stats_cards(stats_block(df))

    st.subheader("📈 R-Distribution")
    st.plotly_chart(plot_r_distribution(df), use_container_width=True)

    st.subheader("💰 Equity Curve")
    st.plotly_chart(plot_equity_curve(df), use_container_width=True)

    st.subheader("⚖️ Group A vs Group B")
    cmp_df = plot_group_comparison(df)
    if not cmp_df.empty:
        # Format
        cmp_show = cmp_df.copy()
        cmp_show["Win Rate"] = cmp_show["Win Rate"].apply(lambda x: f"{x:.1%}")
        cmp_show["Expectancy (R)"] = cmp_show["Expectancy (R)"].apply(lambda x: f"{x:.3f}")
        cmp_show["Profit Factor"] = cmp_show["Profit Factor"].apply(lambda x: f"{x:.2f}")
        cmp_show["Avg Win (R)"] = cmp_show["Avg Win (R)"].apply(lambda x: f"{x:.2f}")
        cmp_show["Avg Loss (R)"] = cmp_show["Avg Loss (R)"].apply(lambda x: f"{x:.2f}")
        st.dataframe(cmp_show, use_container_width=True, hide_index=True)

        # Insight
        df_a = df[df["group"] == "A"]
        df_b = df[df["group"] == "B"]
        if len(df_a) >= 5 and len(df_b) >= 5:
            edge_a = df_a["R"].mean()
            edge_b = df_b["R"].mean()
            if edge_a > edge_b + 0.1:
                st.success(f"✅ สมมติฐานยืนยัน: Group A (Edge {edge_a:.2f}R) > Group B (Edge {edge_b:.2f}R)")
            elif edge_b > edge_a + 0.1:
                st.warning(f"⚠️ ผิดสมมติฐาน: Group B กลับให้ Edge สูงกว่า ({edge_b:.2f}R vs {edge_a:.2f}R)")
            else:
                st.info(f"➖ Group A vs B ใกล้เคียงกัน ({edge_a:.2f}R vs {edge_b:.2f}R) — ลองเพิ่ม sample")

    st.subheader("📋 Trade Log")
    # Filter
    fc1, fc2 = st.columns([2, 1])
    with fc1:
        sym_filter = st.text_input("🔍 ค้นหา symbol", "")
    with fc2:
        grp_filter = st.selectbox("Group", ["All", "A", "B"], index=0)

    df_show = df.copy()
    if sym_filter:
        df_show = df_show[df_show["symbol"].str.contains(sym_filter.upper(), na=False)]
    if grp_filter != "All":
        df_show = df_show[df_show["group"] == grp_filter]

    df_display = df_show[[
        "symbol", "group", "entry_date", "exit_date", "days_held",
        "entry_price", "exit_price", "sl_initial", "sl_type",
        "risk_pct", "R", "exit_reason"
    ]].copy()
    df_display["entry_date"] = df_display["entry_date"].dt.strftime("%Y-%m-%d")
    df_display["exit_date"] = df_display["exit_date"].dt.strftime("%Y-%m-%d")
    df_display = df_display.sort_values("R", ascending=False)

    st.dataframe(df_display, use_container_width=True, height=400, hide_index=True)

    csv = df_display.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        f"💾 Download Trade Log ({len(df_display)} trades)",
        data=csv,
        file_name=f"peacock_backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
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
    
    - R = -1 → Stop Loss เต็ม (เสีย 1 หน่วยความเสี่ยง)
    - R = 0 → Break-even
    - R = +3 → กำไร 3 เท่าของความเสี่ยง
    
    **Expectancy** = ค่าเฉลี่ย R ต่อ trade — ถ้า > 0 = มี Edge
    
    **Profit Factor** = (กำไรรวม) / (ขาดทุนรวม) — ถ้า > 1.5 = ดี, > 2 = ดีมาก
    
    **ข้อจำกัด**
    - ไม่คิด commission, slippage, spread
    - ไม่คิด dividend
    - ไม่คำนึง position sizing แบบ compounding (ทุก trade = 1R เท่ากัน)
    - Survivorship bias: S&P500 / SET100 ปัจจุบัน อาจไม่สะท้อนความจริงในอดีต
    """)
