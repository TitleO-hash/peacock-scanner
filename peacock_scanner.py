"""
🦚 Peacock Scanner — Streamlit App
======================================
สูตรแสกนหุ้นขาขึ้นโมเมนตัมแกร่ง (Peacock)

เงื่อนไข:
  1. Price > EMA10 > EMA20 > EMA35 > EMA75   (เรียงลงมา)
  2. Price > EMA200
  3. Timeframe: Day

User ปรับได้:
  - ค่า period ของ EMA แต่ละเส้น
  - เปิด/ปิด การใช้งานเส้น EMA แต่ละเส้น

วิธีรัน:
  pip install streamlit yfinance pandas
  streamlit run peacock_scanner.py
"""

import streamlit as st
import yfinance as yf
import pandas as pd
from datetime import datetime
from io import StringIO

# ══════════════════════════════════════════════════════
#  PAGE CONFIG
# ══════════════════════════════════════════════════════
st.set_page_config(
    page_title="Peacock Scanner",
    page_icon="🦚",
    layout="wide"
)

st.title("🦚 Peacock Scanner")
st.caption("หุ้นขาขึ้นโมเมนตัมแกร่ง — Price > EMA10 > EMA20 > EMA35 > EMA75 และ Price > EMA200")
st.caption(f"อัพเดทล่าสุด: {datetime.now().strftime('%d %b %Y %H:%M')}")

# ── Session State ─────────────────────────────────────
if "pre_results" not in st.session_state:
    st.session_state.pre_results = pd.DataFrame()
if "fresh_results" not in st.session_state:
    st.session_state.fresh_results = pd.DataFrame()
if "recent_results" not in st.session_state:
    st.session_state.recent_results = pd.DataFrame()
if "scan_done" not in st.session_state:
    st.session_state.scan_done = False
if "scan_errors" not in st.session_state:
    st.session_state.scan_errors = []

# ══════════════════════════════════════════════════════
#  EMA CONFIG (default ตามไฟล์ Peacock)
# ══════════════════════════════════════════════════════
EMA_DEFAULTS = [
    {"name": "EMA 10",  "period": 10,  "color": "#3498DB", "label": "ฟ้า"},
    {"name": "EMA 20",  "period": 20,  "color": "#E67E22", "label": "ส้ม"},
    {"name": "EMA 35",  "period": 35,  "color": "#27AE60", "label": "เขียว"},
    {"name": "EMA 75",  "period": 75,  "color": "#E74C3C", "label": "แดง"},
    {"name": "EMA 200", "period": 200, "color": "#9B59B6", "label": "ม่วง"},
]

# ══════════════════════════════════════════════════════
#  SIDEBAR — SETTINGS
# ══════════════════════════════════════════════════════
with st.sidebar:
    st.header("⚙️ ตั้งค่า EMA")
    st.caption("ปิด/เปิด หรือปรับค่า period ของแต่ละเส้นได้")

    ema_settings = []
    for i, ema in enumerate(EMA_DEFAULTS):
        col1, col2 = st.columns([1, 2])
        with col1:
            enabled = st.checkbox(
                f"เปิด",
                value=True,
                key=f"enable_{i}",
                label_visibility="collapsed"
            )
        with col2:
            period = st.number_input(
                f"🎨 {ema['name']} ({ema['label']})",
                min_value=2,
                max_value=500,
                value=ema["period"],
                step=1,
                key=f"period_{i}",
                disabled=not enabled,
            )
        ema_settings.append({
            "name": ema["name"],
            "default_period": ema["period"],
            "period": period,
            "color": ema["color"],
            "label": ema["label"],
            "enabled": enabled,
        })

    st.divider()

    st.header("📁 รายชื่อหุ้น")
    market_choice = st.radio(
        "เลือก Market",
        ["อัพโหลด CSV", "S&P 500 (preset)", "SET100 (preset)", "พิมพ์เอง"],
        index=0,
    )

    uploaded_file = None
    typed_symbols = ""
    preset_used = None

    if market_choice == "อัพโหลด CSV":
        uploaded_file = st.file_uploader(
            "อัพโหลดไฟล์ CSV (มีคอลัมน์ Symbol)",
            type=["csv"]
        )
        st.caption("💡 รองรับคอลัมน์ชื่อ: Symbol / Ticker / symbol")
    elif market_choice == "พิมพ์เอง":
        typed_symbols = st.text_area(
            "พิมพ์ symbol คั่นด้วย comma หรือ newline",
            placeholder="AAPL, MSFT, NVDA\nORCL\nGOOGL",
            height=120,
        )
    elif market_choice == "S&P 500 (preset)":
        preset_used = "sp500"
        st.info("ใช้ symbol จาก S&P 500 (ดึง list จาก Wikipedia)")
    elif market_choice == "SET100 (preset)":
        preset_used = "set100"
        st.info("ใช้ symbol จาก SET100 (ต่อท้าย .BK)")

    st.divider()

    st.header("📅 ช่วงข้อมูล")
    history_period = st.selectbox(
        "ดึงข้อมูลย้อนหลัง",
        ["1y", "2y", "5y"],
        index=0,
        help="แนะนำอย่างน้อย 1 ปี เพื่อให้ EMA200 คำนวณได้แม่นยำ"
    )

    st.divider()

    st.header("🌟 Recent Cross")
    st.caption("เลือกช่วงวันสำหรับ Group 3 (ตัดครบมาแล้วกี่วัน)")
    recent_range = st.slider(
        "ตัดครบมาแล้ว N วันก่อน",
        min_value=1,
        max_value=30,
        value=(1, 1),
        step=1,
        help=(
            "1 = เมื่อวาน, 2 = วันก่อนเมื่อวาน, ...\n"
            "ลากเป็น range ได้ เช่น (1, 5) = ตัดมาแล้ว 1-5 วันก่อน"
        ),
    )
    recent_min, recent_max = recent_range

    st.divider()
    scan_button = st.button("🚀 SCAN เลย!", type="primary", use_container_width=True)


# ══════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ══════════════════════════════════════════════════════
def get_symbol_list():
    """ดึง list ของ symbols จาก source ที่ user เลือก"""
    symbols = []

    if market_choice == "อัพโหลด CSV" and uploaded_file is not None:
        df = pd.read_csv(uploaded_file)
        # หา column ที่ชื่อใกล้เคียง Symbol
        for col in df.columns:
            if col.strip().lower() in ("symbol", "ticker", "symbols", "tickers"):
                symbols = df[col].dropna().astype(str).str.strip().str.upper().tolist()
                break
        if not symbols:
            # ถ้าไม่เจอ column ชื่อ Symbol ใช้ column แรก
            symbols = df.iloc[:, 0].dropna().astype(str).str.strip().str.upper().tolist()

    elif market_choice == "พิมพ์เอง" and typed_symbols.strip():
        # split ด้วย comma หรือ newline
        raw = typed_symbols.replace(",", "\n").split("\n")
        symbols = [s.strip().upper() for s in raw if s.strip()]

    elif preset_used == "sp500":
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
            symbols = tables[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        except Exception as e:
            st.error(f"ดึง S&P 500 list ไม่สำเร็จ: {e}")

    elif preset_used == "set100":
        # SET100 ต้องต่อ .BK สำหรับ yfinance
        try:
            tables = pd.read_html("https://en.wikipedia.org/wiki/SET100_Index")
            # หา column ที่ดูเหมือน symbol
            for table in tables:
                for col in table.columns:
                    if "symbol" in str(col).lower() or "ticker" in str(col).lower():
                        symbols = table[col].astype(str).str.strip().tolist()
                        break
                if symbols:
                    break
            symbols = [f"{s}.BK" for s in symbols if s and s != "nan"]
        except Exception as e:
            st.error(f"ดึง SET100 list ไม่สำเร็จ: {e}")

    return symbols


def calc_ema(series, period):
    # ใช้วิธีเดียวกับ TradingView คือ
    # ใช้ค่าเฉลี่ยธรรมดาของ N วันแรกเป็นจุดตั้งต้น
    # แล้วค่อยคำนวณ EMA ต่อจากนั้น
    ema = series.copy().astype(float)
    if len(series) < period:
        return ema * float("nan")
    # จุดตั้งต้น = ค่าเฉลี่ยธรรมดาของ period วันแรก
    ema.iloc[:period - 1] = float("nan")
    ema.iloc[period - 1] = series.iloc[:period].mean()
    # คำนวณ EMA ต่อจากนั้น
    multiplier = 2 / (period + 1)
    for i in range(period, len(series)):
        ema.iloc[i] = (series.iloc[i] - ema.iloc[i - 1]) * multiplier + ema.iloc[i - 1]
    return ema


def is_peacock_bar(row, chain_emas, long_filter):
    """
    เช็คว่าแท่งนี้เข้าเงื่อนไข Peacock เต็มหรือไม่
    เงื่อนไข 1: Close > chain[0] > chain[1] > ... > chain[n-1]
    เงื่อนไข 2: Close > long_filter (EMA200)
    """
    close = row["Close"]
    if chain_emas:
        if close <= row[chain_emas[0]["name"]]:
            return False
        for i in range(len(chain_emas) - 1):
            if row[chain_emas[i]["name"]] <= row[chain_emas[i + 1]["name"]]:
                return False
    if long_filter:
        if close <= row[long_filter["name"]]:
            return False
    return True


def is_pre_peacock_bar(row, chain_emas, long_filter):
    """
    เช็คว่าแท่งนี้เป็น "จ่อตัดครบ" หรือไม่
      - EMA chain เรียงครบ: chain[0] > chain[1] > ... > chain[n-1]
      - Close > long_filter (EMA200)
      - แต่ Close <= chain[0]  ← ยังตัดไม่ครบ
    """
    if not chain_emas:
        return False
    close = row["Close"]
    # Close ต้องยังไม่ทะลุ EMA เส้นสั้นที่สุด
    if close > row[chain_emas[0]["name"]]:
        return False
    # EMA chain ต้องเรียงครบ
    for i in range(len(chain_emas) - 1):
        if row[chain_emas[i]["name"]] <= row[chain_emas[i + 1]["name"]]:
            return False
    # Close ต้อง > EMA200
    if long_filter:
        if close <= row[long_filter["name"]]:
            return False
    return True


def classify_peacock(df, ema_settings):
    """
    จำแนกหุ้นเป็น 1 ใน 4 ประเภท:
      'pre'          = จ่อตัดครบ
      'fresh_today'  = ตัดครบวันนี้วันแรก
      'recent'       = ตัดครบมาแล้ว N วัน
      None           = ไม่เข้าเงื่อนไขใดๆ
    """
    if df.empty or len(df) < 2:
        return None, None, None

    enabled_emas = [e for e in ema_settings if e["enabled"]]
    if not enabled_emas:
        return None, None, None

    max_period = max(e["period"] for e in enabled_emas)
    if len(df) < max_period:
        return None, None, None

    df = df.copy()
    for ema in enabled_emas:
        df[ema["name"]] = calc_ema(df["Close"], ema["period"])

    chain_emas = [ema_settings[i] for i in range(min(4, len(ema_settings)))
                  if ema_settings[i]["enabled"]]
    long_filter = (ema_settings[4]
                   if len(ema_settings) > 4 and ema_settings[4]["enabled"]
                   else None)

    last = df.iloc[-1]

    # ── เช็ค Peacock เต็ม ──
    is_peacock_today = is_peacock_bar(last, chain_emas, long_filter)

    if is_peacock_today:
        days_since_cross = None
        for i in range(1, min(len(df), 60)):
            past_row = df.iloc[-i - 1]
            if not is_peacock_bar(past_row, chain_emas, long_filter):
                days_since_cross = i
                break

        if days_since_cross is None:
            days_since_cross = 60

        category = "fresh_today" if days_since_cross == 1 else "recent"
        days_ago = 0 if category == "fresh_today" else days_since_cross - 1

        result = _build_result_dict(last, enabled_emas)
        if category == "recent":
            result["Days Ago"] = days_ago
        return category, days_ago, result

    # ── เช็ค Pre-Peacock ──
    if is_pre_peacock_bar(last, chain_emas, long_filter):
        result = _build_result_dict(last, enabled_emas)
        gap_pct = (last["Close"] / last[chain_emas[0]["name"]] - 1) * 100
        result["Gap to EMA เส้นสั้นสุด %"] = round(gap_pct, 2)
        return "pre", None, result

    return None, None, None


def _build_result_dict(last_row, enabled_emas):
    """สร้าง dict ของผลลัพธ์ row หนึ่ง"""
    close = last_row["Close"]
    result = {
        "Close": round(close, 4),
        "Date": last_row.name.strftime("%Y-%m-%d") if hasattr(last_row.name, "strftime") else str(last_row.name),
    }
    for ema in enabled_emas:
        result[ema["name"]] = round(last_row[ema["name"]], 4)
    return result


def fetch_one(symbol, period):
    """ดึงข้อมูลหุ้น 1 ตัว"""
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None


# ══════════════════════════════════════════════════════
#  RUN SCAN
# ══════════════════════════════════════════════════════
if scan_button:
    symbols = get_symbol_list()

    if not symbols:
        st.error("⚠️ ยังไม่มี symbol ให้ scan ครับ — กรุณาอัพโหลด CSV / พิมพ์ / หรือเลือก preset")
        st.stop()

    st.info(f"กำลัง scan {len(symbols)} ตัว...")

    progress = st.progress(0.0)
    status = st.empty()

    pre_results = []
    fresh_results = []
    recent_results = []
    errors = []

    for i, sym in enumerate(symbols):
        status.text(f"[{i+1}/{len(symbols)}] กำลังดึง {sym}...")
        df = fetch_one(sym, history_period)
        if df is None or df.empty:
            errors.append(sym)
        else:
            category, days_ago, data = classify_peacock(df, ema_settings)
            if category == "pre":
                pre_results.append({"Symbol": sym, **data})
            elif category == "fresh_today":
                fresh_results.append({"Symbol": sym, **data})
            elif category == "recent":
                if recent_min <= days_ago <= recent_max:
                    recent_results.append({"Symbol": sym, **data})
        progress.progress((i + 1) / len(symbols))

    status.empty()
    progress.empty()

    st.session_state.pre_results = pd.DataFrame(pre_results)
    st.session_state.fresh_results = pd.DataFrame(fresh_results)
    st.session_state.recent_results = pd.DataFrame(recent_results)
    st.session_state.recent_range = (recent_min, recent_max)
    st.session_state.scan_errors = errors
    st.session_state.scan_done = True

    total = len(pre_results) + len(fresh_results) + len(recent_results)
    st.success(
        f"✅ Scan เสร็จ! เจอหุ้นเข้าเงื่อนไขรวม **{total}** ตัว จากทั้งหมด {len(symbols)} ตัว — "
        f"จ่อตัด: {len(pre_results)}, ตัดวันนี้: {len(fresh_results)}, ตัด {recent_min}-{recent_max} วันก่อน: {len(recent_results)}"
    )


# ══════════════════════════════════════════════════════
#  DISPLAY RESULTS
# ══════════════════════════════════════════════════════
def show_group(df_result, group_name, group_key):
    """แสดงตารางของแต่ละ group"""
    if df_result.empty:
        st.info(f"ไม่มีหุ้นในกลุ่ม {group_name} ครับ")
        return

    base_order = ["Symbol", "Date", "Close"]
    if "Days Ago" in df_result.columns:
        base_order.append("Days Ago")
    if "Gap to EMA เส้นสั้นสุด %" in df_result.columns:
        base_order.append("Gap to EMA เส้นสั้นสุด %")

    ema_cols = [c for c in df_result.columns if c.startswith("EMA")]
    ema_cols_sorted = sorted(ema_cols, key=lambda c: int(c.replace("EMA", "").strip()))

    col_order = base_order + ema_cols_sorted
    col_order = [c for c in col_order if c in df_result.columns]
    df_show = df_result[col_order].copy()

    col1, col2 = st.columns([2, 1])
    with col1:
        search = st.text_input("🔍 ค้นหา symbol", "", key=f"search_{group_key}")
    with col2:
        sort_by = st.selectbox("เรียงตาม", df_show.columns.tolist(), index=0, key=f"sort_{group_key}")

    if search:
        df_show = df_show[df_show["Symbol"].str.contains(search.upper(), na=False)]

    df_show = df_show.sort_values(sort_by, ascending=False if sort_by != "Symbol" else True)

    st.dataframe(df_show, use_container_width=True, height=400)

    csv = df_show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label=f"💾 Download {group_name} เป็น CSV",
        data=csv,
        file_name=f"peacock_{group_key}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        key=f"dl_{group_key}",
    )

    if not df_show.empty:
        selected = st.selectbox(
            "📈 เปิดใน TradingView",
            df_show["Symbol"].tolist(),
            key=f"tv_{group_key}",
        )
        if selected:
            tv_symbol = selected
            if selected.endswith(".BK"):
                tv_symbol = f"SET:{selected.replace('.BK', '')}"
            tv_url = f"https://www.tradingview.com/chart/?symbol={tv_symbol}"
            st.markdown(f"🔗 [เปิด {selected} ใน TradingView]({tv_url})")


if st.session_state.scan_done:
    st.divider()
    st.subheader("🎯 ผลการ Scan")

    pre_df = st.session_state.get("pre_results", pd.DataFrame())
    fresh_df = st.session_state.get("fresh_results", pd.DataFrame())
    recent_df = st.session_state.get("recent_results", pd.DataFrame())
    rng = st.session_state.get("recent_range", (1, 1))

    c1, c2, c3 = st.columns(3)
    c1.metric("🟡 จ่อตัดครบ", len(pre_df))
    c2.metric("🔥 ตัดครบวันนี้", len(fresh_df))
    c3.metric(f"🌟 ตัดครบ {rng[0]}-{rng[1]} วันก่อน", len(recent_df))

    tab1, tab2, tab3 = st.tabs([
        f"🟡 จ่อตัดครบ ({len(pre_df)})",
        f"🔥 ตัดครบวันนี้ ({len(fresh_df)})",
        f"🌟 ตัด {rng[0]}-{rng[1]} วันก่อน ({len(recent_df)})",
    ])

    # ดึงชื่อ EMA เส้นสั้นสุดที่ enabled สำหรับ caption
    enabled_chain = [e for e in ema_settings[:4] if e["enabled"]]
    shortest_ema_name = enabled_chain[0]["name"] if enabled_chain else "EMA เส้นสั้นสุด"

    with tab1:
        st.caption(f"EMA chain เรียงครบ + Close > EMA200 — แต่ Close ยังไม่ทะลุ {shortest_ema_name}")
        show_group(pre_df, "จ่อตัดครบ", "pre")

    with tab2:
        st.caption(f"Close > {shortest_ema_name} > ... > EMA75 และ Close > EMA200 — เพิ่งครบวันนี้วันแรก")
        show_group(fresh_df, "ตัดครบวันนี้", "fresh")

    with tab3:
        st.caption(f"ครบเงื่อนไข Peacock มาแล้ว {rng[0]}-{rng[1]} วันก่อน (1 = เมื่อวาน)")
        show_group(recent_df, f"ตัด {rng[0]}-{rng[1]} วันก่อน", "recent")

    if st.session_state.scan_errors:
        with st.expander(f"⚠️ Symbol ที่ดึงข้อมูลไม่สำเร็จ ({len(st.session_state.scan_errors)} ตัว)"):
            st.write(", ".join(st.session_state.scan_errors))


# ══════════════════════════════════════════════════════
#  FOOTER
# ══════════════════════════════════════════════════════
st.divider()
with st.expander("ℹ️ เกี่ยวกับสูตร Peacock"):
    st.markdown("""
**สูตร Peacock** คือสูตรหาหุ้น **ขาขึ้นโมเมนตัมแกร่ง** บน Timeframe Day

**เงื่อนไข Peacock เต็ม:**
1. `Close > EMA10 > EMA20 > EMA35 > EMA75` (เรียงลงมา)
2. `Close > EMA200`

**3 กลุ่มที่ scanner แยก:**
- 🟡 **จ่อตัดครบ** — EMA เรียงครบ + Close > EMA200 แต่ Close ยังไม่ทะลุ EMA เส้นสั้นสุด
- 🔥 **ตัดครบวันนี้** — เข้าเงื่อนไข Peacock เต็มวันนี้ (เมื่อวานยังไม่ผ่าน)
- 🌟 **ตัดครบ N วันก่อน** — ครบมาแล้ว N วัน (1 = เมื่อวาน)

**สีเส้น EMA มาตรฐาน:**
- 🟦 EMA 10 — ฟ้า
- 🟧 EMA 20 — ส้ม
- 🟩 EMA 35 — เขียว
- 🟥 EMA 75 — แดง
- 🟪 EMA 200 — ม่วง

**Note:** ผู้ใช้สามารถปรับค่า period ได้เอง และเลือกเปิด/ปิดเส้นไหนก็ได้
""")
