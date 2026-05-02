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
if "scan_results" not in st.session_state:
    st.session_state.scan_results = pd.DataFrame()
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
        ["6mo", "1y", "2y", "5y"],
        index=1,
        help="ต้องยาวพอสำหรับ EMA period ที่ใช้"
    )

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
    return series.ewm(span=period, adjust=False).mean()


def check_peacock(df, ema_settings):
    """
    เช็คว่าหุ้นเข้าเงื่อนไข Peacock หรือไม่
    Return: (passed: bool, latest_data: dict)
    """
    if df.empty or len(df) < 2:
        return False, None

    # คำนวณ EMA ทุกเส้นที่เปิดใช้
    enabled_emas = [e for e in ema_settings if e["enabled"]]
    if not enabled_emas:
        return False, None

    # ต้องมีข้อมูลพอ
    max_period = max(e["period"] for e in enabled_emas)
    if len(df) < max_period:
        return False, None

    df = df.copy()
    for ema in enabled_emas:
        df[ema["name"]] = calc_ema(df["Close"], ema["period"])

    last = df.iloc[-1]
    close = last["Close"]

    # แยกเส้น EMA200 (เงื่อนไข 2) ออกจาก EMA สั้น (เงื่อนไข 1)
    # ใน config มาตรฐาน: EMA200 คือเส้นที่ period ใหญ่สุด
    # แต่ user ปรับได้ ดังนั้นใช้ "ทุกเส้นที่เปิด" มาเรียงลำดับ period จากเล็ก → ใหญ่
    # เงื่อนไข Peacock = Price > เส้นสั้นสุด > ... > เส้นยาวที่สุด (ที่ไม่ใช่ 200)
    #                  AND Price > เส้น EMA 200

    # เพื่อ flexibility: เรียง period จากน้อย → มาก แล้วเช็คว่า Close > EMA1 > EMA2 > ... > EMA_N
    # (รวม EMA200 เข้าใน chain เลย เพราะถ้า Price > EMA สั้นกว่าทุกตัว และทุกตัว > 200 → Price > 200 อัตโนมัติ)

    sorted_emas = sorted(enabled_emas, key=lambda x: x["period"])

    # เช็ค Price > EMA สั้นสุด
    if close <= last[sorted_emas[0]["name"]]:
        return False, None

    # เช็คเรียงลำดับ EMA
    for i in range(len(sorted_emas) - 1):
        if last[sorted_emas[i]["name"]] <= last[sorted_emas[i + 1]["name"]]:
            return False, None

    # ผ่านทุกเงื่อนไข → เก็บข้อมูล
    result = {
        "Close": round(close, 4),
        "Date": last.name.strftime("%Y-%m-%d") if hasattr(last.name, "strftime") else str(last.name),
    }
    for ema in sorted_emas:
        result[ema["name"]] = round(last[ema["name"]], 4)

    # คำนวณ % change 5 วัน 20 วัน
    if len(df) >= 6:
        result["%Chg 5D"] = round((close / df["Close"].iloc[-6] - 1) * 100, 2)
    if len(df) >= 21:
        result["%Chg 20D"] = round((close / df["Close"].iloc[-21] - 1) * 100, 2)

    return True, result


def fetch_one(symbol, period):
    """ดึงข้อมูลหุ้น 1 ตัว"""
    try:
        df = yf.download(symbol, period=period, interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if df.empty:
            return None
        # yfinance บางที return MultiIndex column
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

    results = []
    errors = []

    for i, sym in enumerate(symbols):
        status.text(f"[{i+1}/{len(symbols)}] กำลังดึง {sym}...")
        df = fetch_one(sym, history_period)
        if df is None or df.empty:
            errors.append(sym)
        else:
            passed, data = check_peacock(df, ema_settings)
            if passed:
                data = {"Symbol": sym, **data}
                results.append(data)
        progress.progress((i + 1) / len(symbols))

    status.empty()
    progress.empty()

    st.session_state.scan_results = pd.DataFrame(results)
    st.session_state.scan_errors = errors
    st.session_state.scan_done = True

    st.success(f"✅ Scan เสร็จ! เจอหุ้นที่เข้าเงื่อนไข Peacock {len(results)} ตัว จากทั้งหมด {len(symbols)} ตัว")


# ══════════════════════════════════════════════════════
#  DISPLAY RESULTS
# ══════════════════════════════════════════════════════
if st.session_state.scan_done:
    st.divider()
    st.subheader("🎯 หุ้นที่เข้าเงื่อนไข Peacock")

    df_result = st.session_state.scan_results

    if df_result.empty:
        st.warning("ไม่มีหุ้นที่เข้าเงื่อนไขในรอบนี้ครับ — ลองปรับ parameter หรือเปลี่ยน watchlist")
    else:
        # เรียงลำดับ column ให้สวย
        col_order = ["Symbol", "Date", "Close"]
        ema_cols = [c for c in df_result.columns if c.startswith("EMA")]
        # sort EMA cols ตาม period
        ema_cols_sorted = sorted(
            ema_cols,
            key=lambda c: int(c.replace("EMA", "").strip())
        )
        change_cols = [c for c in df_result.columns if c.startswith("%Chg")]

        col_order = col_order + ema_cols_sorted + change_cols
        col_order = [c for c in col_order if c in df_result.columns]
        df_show = df_result[col_order].copy()

        # ── Filter / Sort ──
        col1, col2 = st.columns([2, 1])
        with col1:
            search = st.text_input("🔍 ค้นหา symbol", "")
        with col2:
            sort_by = st.selectbox("เรียงตาม", df_show.columns.tolist(), index=0)

        if search:
            df_show = df_show[df_show["Symbol"].str.contains(search.upper(), na=False)]

        df_show = df_show.sort_values(sort_by, ascending=False if sort_by != "Symbol" else True)

        st.dataframe(df_show, use_container_width=True, height=500)

        # ── Stats ──
        st.divider()
        c1, c2, c3 = st.columns(3)
        c1.metric("📊 หุ้นที่เข้าเงื่อนไข", len(df_result))
        if "%Chg 5D" in df_result.columns:
            c2.metric("⚡ %Chg 5D เฉลี่ย", f"{df_result['%Chg 5D'].mean():.2f}%")
        if "%Chg 20D" in df_result.columns:
            c3.metric("🚀 %Chg 20D เฉลี่ย", f"{df_result['%Chg 20D'].mean():.2f}%")

        # ── Download CSV ──
        csv = df_show.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="💾 Download ผลเป็น CSV",
            data=csv,
            file_name=f"peacock_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

        # ── TradingView Link ──
        st.divider()
        st.subheader("📈 เปิดดูใน TradingView")
        selected = st.selectbox(
            "เลือก Symbol",
            df_show["Symbol"].tolist()
        )
        if selected:
            # ตัด .BK ออกแล้วใส่ exchange ให้ TradingView
            tv_symbol = selected
            if selected.endswith(".BK"):
                tv_symbol = f"SET:{selected.replace('.BK', '')}"
            tv_url = f"https://www.tradingview.com/chart/?symbol={tv_symbol}"
            st.markdown(f"🔗 [เปิด {selected} ใน TradingView]({tv_url})")

    # ── Errors ──
    if st.session_state.scan_errors:
        with st.expander(f"⚠️ Symbol ที่ดึงข้อมูลไม่สำเร็จ ({len(st.session_state.scan_errors)} ตัว)"):
            st.write(", ".join(st.session_state.scan_errors))


# ══════════════════════════════════════════════════════
#  FOOTER — แสดง legend สี EMA
# ══════════════════════════════════════════════════════
st.divider()
with st.expander("ℹ️ เกี่ยวกับสูตร Peacock"):
    st.markdown("""
**สูตร Peacock** คือสูตรหาหุ้น **ขาขึ้นโมเมนตัมแกร่ง** บน Timeframe Day

**เงื่อนไข:**
1. `Price > EMA10 > EMA20 > EMA35 > EMA75` (เรียงลงมา)
2. `Price > EMA200`

**สีเส้น EMA มาตรฐาน:**
- 🟦 EMA 10 — ฟ้า
- 🟧 EMA 20 — ส้ม
- 🟩 EMA 35 — เขียว
- 🟥 EMA 75 — แดง
- 🟪 EMA 200 — ม่วง

**Note:** ผู้ใช้สามารถปรับค่า period ได้เอง และเลือกเปิด/ปิดเส้นไหนก็ได้
""")
