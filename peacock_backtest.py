"""
🦚 Peacock Backtest V2 — R-Multiple Edge Validator
==================================================
Backtest กลยุทธ์ Peacock เพื่อพิสูจน์ว่ามี Edge จริงหรือไม่

ENTRY:
  - Fresh Today (Peacock เต็มวันแรก) → Buy at next Open (T+1)

EXIT:
  - Initial SL (วัน entry):
    * ถ้า (Close − EMA200)/EMA200 > 3% → SL = max(EMA10, entry × 0.97)
    * ถ้า ≤ 3% → SL = max(EMA200, entry × 0.97)
    * Master Law: SL ห่างจาก entry ไม่เกิน 3%
  - Trailing: ทุกวัน Stop = max(SL_initial, EMA20)
    EXIT เมื่อ Close < Stop → ขายที่ next Open

R-Multiple = (exit − entry) / (entry − SL_initial)

V2 Changes:
  - ตัด Group A/B classification ออก (เร็วขึ้น)
  - R-Distribution มี slider ปรับ X-axis range
  - แสดงจำนวน trade ที่อยู่นอก range เป็น annotation
  - เพิ่ม Median R, Percentiles, SL Type breakdown

วิธีรัน:
  pip install streamlit yfinance pandas plotly
  streamlit run peacock_backtest.py
"""

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
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
        help="(Close−EMA200)/EMA200 เกินค่านี้ → ใช้ EMA10 SL"
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
    """Vectorized: Close > EMA10 > EMA20 > EMA35 > EMA75 และ Close > EMA200"""
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


def simulate_trade(df, signal_idx, threshold_pct, master_law_pct):
    """จำลอง 1 trade ตาม Peacock rules"""
    if signal_idx + 1 >= len(df):
        return None

    entry_idx = signal_idx + 1
    entry_bar = df.iloc[entry_idx]
    entry_price = float(entry_bar["Open"])
    entry_date = entry_bar.name

    signal_bar = df.iloc[signal_idx]
    ema10 = float(signal_bar["EMA10"])
    ema200 = float(signal_bar["EMA200"])

    distance = (entry_price - ema200) / ema200

    if distance > threshold_pct / 100:
        sl_candidate = ema10
        sl_type = "EMA10"
    else:
        sl_candidate = ema200
        sl_type = "EMA200"

    sl_floor = entry_price * (1 - master_law_pct / 100)
    sl_initial = max(sl_candidate, sl_floor)
    if sl_initial == sl_floor and sl_floor > sl_candidate:
        sl_type = f"{sl_type}+ML"

    if sl_initial >= entry_price:
        return None

    risk_unit = entry_price - sl_initial

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


def backtest_symbol(symbol, period, threshold_pct, master_law_pct):
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
            if idx <= last_exit_idx:
                continue
            if idx < 200:
                continue

            trade = simulate_trade(df, idx, threshold_pct, master_law_pct)
            if trade is None:
                continue
            trade["symbol"] = symbol
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
                ex.submit(backtest_symbol, sym, period,
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
                sym, period,
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
def stats_block(df):
    """คำนวณสถิติของชุด trades"""
    if df.empty:
        return {"n": 0, "win_rate": 0, "avg_R": 0, "median_R": 0,
                "pf": 0, "max_R": 0, "min_R": 0,
                "avg_win": 0, "avg_loss": 0,
                "p25": 0, "p75": 0, "p95": 0, "p05": 0}
    wins = df[df["R"] > 0]["R"]
    losses = df[df["R"] <= 0]["R"]
    profit_sum = wins.sum() if len(wins) > 0 else 0
    loss_sum = abs(losses.sum()) if len(losses) > 0 else 0
    pf = (profit_sum / loss_sum) if loss_sum > 0 else float("inf")
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
    }


def show_stats_cards_main(stats):
    """5 cards หลัก"""
    if stats["n"] == 0:
        st.info("ไม่มี trade")
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Trades", f"{stats['n']:,}")
    c2.metric("Win Rate", f"{stats['win_rate']:.1%}")
    c3.metric("Expectancy (avg R)", f"{stats['avg_R']:.3f}",
              delta=f"{'+' if stats['avg_R']>0 else ''}{stats['avg_R']:.2f}")
    c4.metric("Profit Factor", f"{stats['pf']:.2f}" if stats["pf"] != float("inf") else "∞")
    c5.metric("Median R", f"{stats['median_R']:.3f}",
              help="ค่ากลาง — ตัด outlier แล้ว ใกล้ความจริงมากกว่า avg")


def show_stats_cards_extra(stats):
    """5 cards รอง"""
    if stats["n"] == 0:
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Avg Win", f"{stats['avg_win']:.2f}R")
    c2.metric("Avg Loss", f"{stats['avg_loss']:.2f}R")
    c3.metric("Max R", f"{stats['max_R']:.1f}",
              help="trade ที่ดีที่สุด — ระวัง outlier")
    c4.metric("Min R", f"{stats['min_R']:.1f}",
              help="trade ที่แย่ที่สุด — ระวัง outlier")
    c5.metric("P95 / P05", f"{stats['p95']:.1f} / {stats['p05']:.1f}",
              help="95th และ 5th percentile (90% ของ trade อยู่ระหว่างนี้)")


def plot_r_distribution(df, x_min, x_max, bin_size):
    """Histogram ของ R-Multiple พร้อม annotation outliers"""
    R = df["R"].values

    n_below = int((R < x_min).sum())
    n_above = int((R > x_max).sum())
    n_in_range = int(((R >= x_min) & (R <= x_max)).sum())

    R_in_range = R[(R >= x_min) & (R <= x_max)]

    fig = go.Figure()

    fig.add_trace(go.Histogram(
        x=R_in_range,
        name="Trades",
        marker_color="#5DCAA5",
        opacity=0.85,
        xbins=dict(start=x_min, end=x_max, size=bin_size),
        hovertemplate="R: %{x}<br>จำนวน: %{y}<extra></extra>",
    ))

    fig.add_vline(x=0, line_dash="dash", line_color="gray",
                  annotation_text="Break-even", annotation_position="top")

    avg_R = df["R"].mean()
    median_R = df["R"].median()

    if x_min <= avg_R <= x_max:
        fig.add_vline(x=avg_R, line_dash="dot", line_color="#E24B4A",
                      annotation_text=f"Avg = {avg_R:.2f}R",
                      annotation_position="top")
    if x_min <= median_R <= x_max:
        fig.add_vline(x=median_R, line_dash="dashdot", line_color="#185FA5",
                      annotation_text=f"Median = {median_R:.2f}R",
                      annotation_position="bottom")

    annotations = []
    if n_below > 0:
        annotations.append(dict(
            x=x_min, y=1.05, xref="x", yref="paper",
            text=f"⬅ มี {n_below:,} trades ที่ R < {x_min}<br>(ต่ำสุด: {df['R'].min():.1f})",
            showarrow=False,
            bgcolor="rgba(226, 75, 74, 0.15)",
            bordercolor="#E24B4A",
            borderwidth=1,
            font=dict(size=11, color="#A32D2D"),
            align="left",
        ))
    if n_above > 0:
        annotations.append(dict(
            x=x_max, y=1.05, xref="x", yref="paper",
            text=f"มี {n_above:,} trades ที่ R > {x_max} ➡<br>(สูงสุด: {df['R'].max():.1f})",
            showarrow=False,
            bgcolor="rgba(99, 153, 34, 0.15)",
            bordercolor="#639922",
            borderwidth=1,
            font=dict(size=11, color="#3B6D11"),
            align="right",
        ))

    fig.update_layout(
        title=dict(
            text=f"R-Distribution (แสดง {n_in_range:,} จาก {len(df):,} trades)",
            font=dict(size=18),
        ),
        xaxis_title="R-Multiple",
        yaxis_title="จำนวน Trades",
        height=480,
        annotations=annotations,
        margin=dict(t=100),
        showlegend=False,
    )
    return fig


def plot_equity_curve(df):
    """Cumulative R curve + drawdown"""
    df_sorted = df.sort_values("exit_date").copy()
    df_sorted["cum_R"] = df_sorted["R"].cumsum()

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_sorted["exit_date"], y=df_sorted["cum_R"],
        mode="lines", name="Cumulative R",
        line=dict(color="#185FA5", width=2),
    ))

    cum_max = df_sorted["cum_R"].cummax()
    drawdown = df_sorted["cum_R"] - cum_max
    max_dd = drawdown.min()

    fig.add_hline(y=0, line_dash="dash", line_color="gray")

    fig.update_layout(
        title=f"Equity Curve (Cumulative R) — Final: {df_sorted['cum_R'].iloc[-1]:.1f}R, Max DD: {max_dd:.1f}R",
        xaxis_title="Exit Date",
        yaxis_title="Cumulative R",
        height=400,
    )
    return fig


def plot_sl_type_breakdown(df):
    """Bar เปรียบเทียบ SL types"""
    grp = df.groupby("sl_type").agg(
        n=("R", "count"),
        avg_R=("R", "mean"),
        win_rate=("R", lambda x: (x > 0).mean()),
    ).reset_index().sort_values("n", ascending=False)
    return grp


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
    stats = stats_block(df)

    st.divider()
    st.subheader("📊 Overall Performance")
    show_stats_cards_main(stats)
    st.write("")
    show_stats_cards_extra(stats)

    if stats["avg_R"] > 0:
        if stats["avg_R"] >= 0.5:
            st.success(f"🟢 **มี Edge ที่ดี** — Expectancy = {stats['avg_R']:.2f}R per trade")
        elif stats["avg_R"] >= 0.2:
            st.success(f"🟢 **มี Edge** — Expectancy = {stats['avg_R']:.2f}R per trade")
        else:
            st.warning(f"🟡 **Edge อ่อน** — Expectancy = {stats['avg_R']:.2f}R per trade")
    else:
        st.error(f"🔴 **ไม่มี Edge** — Expectancy = {stats['avg_R']:.2f}R per trade")

    if abs(stats["avg_R"] - stats["median_R"]) > 1.0:
        st.info(
            f"💡 Avg R ({stats['avg_R']:.2f}) ห่างจาก Median R ({stats['median_R']:.2f}) เยอะ "
            f"→ มี outliers ที่ดึง average ผิด — ดู Median เป็นค่าจริงมากกว่า"
        )

    st.divider()
    st.subheader("📈 R-Distribution")

    r_floor = float(min(stats["min_R"] - 1, -3))
    r_ceil = float(max(stats["max_R"] + 1, 10))

    col_l, col_r = st.columns([3, 1])
    with col_l:
        x_range = st.slider(
            "ปรับช่วงแกน X (R-Multiple ที่จะแสดง)",
            min_value=float(np.floor(r_floor)),
            max_value=float(np.ceil(r_ceil)),
            value=(-3.0, 10.0),
            step=0.5,
            help="ลาก slider เพื่อขยาย/ย่อช่วง — outlier นอกช่วงจะแสดงเป็น annotation",
        )
    with col_r:
        bin_size = st.selectbox(
            "ขนาด Bin",
            [0.25, 0.5, 1.0],
            index=1,
            help="bin เล็ก = ละเอียด, bin ใหญ่ = อ่านง่าย",
        )

    st.plotly_chart(
        plot_r_distribution(df, x_range[0], x_range[1], bin_size),
        use_container_width=True,
    )

    with st.expander("📖 อ่านกราฟ R-Distribution ยังไง?"):
        st.markdown("""
        - **เส้นสีเทา (Break-even)** = R = 0 → trade ที่อยู่ขวาเส้น = กำไร
        - **เส้นแดง (Avg)** = Expectancy เฉลี่ย — ถ้าอยู่ขวา 0 = มี Edge
        - **เส้นน้ำเงิน (Median)** = ค่ากลาง — ตัด outlier แล้ว
        - **กล่องที่ขอบกราฟ** = บอกว่ามี trade เกินช่วงที่แสดงกี่ตัว
        - **รูปร่างที่ดี** = right-skewed (กระจุกซ้ายแล้วลากหางยาวไปขวา) = ระบบ trend-following
        """)

    st.divider()
    st.subheader("💰 Equity Curve")
    st.plotly_chart(plot_equity_curve(df), use_container_width=True)

    st.divider()
    st.subheader("🎯 SL Type Breakdown")
    sl_df = plot_sl_type_breakdown(df)
    sl_show = sl_df.copy()
    sl_show["win_rate"] = sl_show["win_rate"].apply(lambda x: f"{x:.1%}")
    sl_show["avg_R"] = sl_show["avg_R"].apply(lambda x: f"{x:.3f}")
    sl_show.columns = ["SL Type", "Trades", "Avg R", "Win Rate"]
    st.dataframe(sl_show, use_container_width=True, hide_index=True)
    st.caption(
        "🟢 EMA10 = ลอยสูง > 3% | 🔵 EMA200 = ใกล้ EMA200 | "
        "🟡 +ML = ติด Master Law cap"
    )

    st.divider()
    st.subheader("📋 Trade Log")
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    with fc1:
        sym_filter = st.text_input("🔍 ค้นหา symbol", "")
    with fc2:
        sl_filter = st.selectbox(
            "SL Type",
            ["All"] + sorted(df["sl_type"].unique().tolist()),
            index=0,
        )
    with fc3:
        sort_by = st.selectbox(
            "เรียงโดย",
            ["entry_date (ล่าสุด)", "R (มากสุด)", "R (น้อยสุด)",
             "days_held (นานสุด)", "risk_pct (สูงสุด)"],
            index=0,
        )

    df_show = df.copy()
    if sym_filter:
        df_show = df_show[df_show["symbol"].str.contains(sym_filter.upper(), na=False)]
    if sl_filter != "All":
        df_show = df_show[df_show["sl_type"] == sl_filter]

    if sort_by == "entry_date (ล่าสุด)":
        df_show = df_show.sort_values("entry_date", ascending=False)
    elif sort_by == "R (มากสุด)":
        df_show = df_show.sort_values("R", ascending=False)
    elif sort_by == "R (น้อยสุด)":
        df_show = df_show.sort_values("R", ascending=True)
    elif sort_by == "days_held (นานสุด)":
        df_show = df_show.sort_values("days_held", ascending=False)
    elif sort_by == "risk_pct (สูงสุด)":
        df_show = df_show.sort_values("risk_pct", ascending=False)

    df_display = df_show[[
        "symbol", "entry_date", "exit_date", "days_held",
        "entry_price", "exit_price", "sl_initial", "sl_type",
        "risk_pct", "R", "exit_reason"
    ]].copy()
    df_display["entry_date"] = df_display["entry_date"].dt.strftime("%Y-%m-%d")
    df_display["exit_date"] = df_display["exit_date"].dt.strftime("%Y-%m-%d")

    st.dataframe(df_display, use_container_width=True, height=400, hide_index=True)
    st.caption(f"แสดง {len(df_display):,} จาก {len(df):,} trades")

    csv = df_display.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        f"💾 Download Trade Log ({len(df_display):,} trades)",
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
    
    **Profit Factor** = (กำไรรวม) / (ขาดทุนรวม) — > 1.5 = ดี, > 2 = ดีมาก
    
    **Median R** vs **Avg R** — ถ้า Median ใกล้ 0 แต่ Avg สูงมาก = Edge มาจาก outlier ไม่กี่ตัว 
    (ระวัง! อาจไม่ replicate ได้ในอนาคต)
    
    **ข้อจำกัด**
    - ไม่คิด commission, slippage, spread
    - ไม่คิด dividend
    - ทุก trade = 1R เท่ากัน (ไม่มี compounding)
    - Survivorship bias: S&P500 / SET100 ปัจจุบัน ไม่สะท้อนความจริงในอดีต
    - Trade ที่ยังเปิดอยู่ตอนจบข้อมูล (exit_reason = `still_open`) อาจมี R สูงผิดปกติ
    """)
