import io
import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

TZ = ZoneInfo("Asia/Tehran")


def secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return os.getenv(key, default)


DATABASE_URL = secret("DATABASE_URL")
BOT_TOKEN = secret("TELEGRAM_BOT_TOKEN")
CHAT_ID = secret("TELEGRAM_CHAT_ID")

st.set_page_config(page_title="انبار عطر", page_icon="🧴", layout="wide")

if not DATABASE_URL:
    st.error("DATABASE_URL تنظیم نشده است. فایل secrets.toml را بررسی کنید.")
    st.stop()


# ---------------------------------------------------------------- دیتابیس
@st.cache_resource
def get_engine():
    url = DATABASE_URL
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
            break
    return create_engine(url, pool_pre_ping=True, pool_size=5, pool_recycle=300)


ENGINE = get_engine()

DDL = [
    """CREATE TABLE IF NOT EXISTS products(
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        brand TEXT NOT NULL DEFAULT '',
        size_ml INTEGER NOT NULL DEFAULT 0,
        price NUMERIC(14,0) NOT NULL DEFAULT 0,
        stock NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (stock >= 0),
        min_stock NUMERIC(12,3) NOT NULL DEFAULT 3,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS movements(
        id BIGSERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id),
        kind TEXT NOT NULL CHECK (kind IN ('IN','OUT')),
        qty NUMERIC(12,3) NOT NULL CHECK (qty > 0),
        stock_after NUMERIC(12,3) NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        telegram_sent BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # ارتقای خودکار اگر جدول‌ها قبلاً با عدد صحیح ساخته شده باشند
    """DO $$
    BEGIN
        IF (SELECT data_type FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'products'
              AND column_name = 'stock') = 'integer' THEN
            ALTER TABLE products ALTER COLUMN stock TYPE NUMERIC(12,3),
                                 ALTER COLUMN min_stock TYPE NUMERIC(12,3);
            ALTER TABLE movements ALTER COLUMN qty TYPE NUMERIC(12,3),
                                  ALTER COLUMN stock_after TYPE NUMERIC(12,3);
        END IF;
    END $$""",
    "CREATE INDEX IF NOT EXISTS idx_movements_created ON movements(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_movements_product ON movements(product_id)",
    # تاریخچه فقط‌افزودنی: حذف و تغییر حرکت‌ها ممکن نیست (فقط وضعیت ارسال تلگرام)
    """CREATE OR REPLACE FUNCTION movements_guard() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'movements is append-only';
        END IF;
        IF NEW.product_id <> OLD.product_id OR NEW.kind <> OLD.kind
           OR NEW.qty <> OLD.qty OR NEW.stock_after <> OLD.stock_after
           OR NEW.created_at <> OLD.created_at OR NEW.note <> OLD.note THEN
            RAISE EXCEPTION 'movements is append-only';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS movements_guard_trg ON movements",
    """CREATE TRIGGER movements_guard_trg
       BEFORE UPDATE OR DELETE ON movements
       FOR EACH ROW EXECUTE FUNCTION movements_guard()""",
]


@st.cache_resource
def init_db():
    with ENGINE.begin() as c:
        for stmt in DDL:
            c.execute(text(stmt))
    return True


init_db()


def to_local(df, col="created_at"):
    if col in df and not df.empty:
        df[col] = (
            pd.to_datetime(df[col], utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
        )
    return df


def fmt(x):
    """عدد با حداکثر ۳ رقم اعشار، بدون صفرهای اضافه."""
    return f"{float(x):.3f}".rstrip("0").rstrip(".")


def products_df(include_inactive=False):
    q = "SELECT * FROM products" + ("" if include_inactive else " WHERE active")
    with ENGINE.connect() as c:
        df = pd.read_sql(text(q + " ORDER BY name"), c)
    df["price"] = df["price"].astype(float)
    df["stock"] = df["stock"].astype(float)
    df["min_stock"] = df["min_stock"].astype(float)
    return to_local(df)


def movements_df():
    q = """SELECT m.id, p.name, p.brand, m.kind, m.qty, m.stock_after,
                  p.price, m.note, m.created_at
           FROM movements m JOIN products p ON p.id = m.product_id
           ORDER BY m.created_at, m.id"""
    with ENGINE.connect() as c:
        df = pd.read_sql(text(q), c)
    df["price"] = df["price"].astype(float)
    df["qty"] = df["qty"].astype(float)
    df["stock_after"] = df["stock_after"].astype(float)
    return to_local(df)


def add_product(name, brand, size_ml, price, stock, min_stock):
    with ENGINE.begin() as c:
        pid = c.execute(
            text(
                """INSERT INTO products(name,brand,size_ml,price,stock,min_stock)
                   VALUES(:n,:b,:s,:p,:st,:m) RETURNING id"""
            ),
            dict(n=name, b=brand, s=size_ml, p=price, st=stock, m=min_stock),
        ).scalar()
        if stock > 0:
            c.execute(
                text(
                    """INSERT INTO movements(product_id,kind,qty,stock_after,note)
                       VALUES(:pid,'IN',:q,:q,'موجودی اولیه')"""
                ),
                dict(pid=pid, q=stock),
            )


def set_active(product_id, active):
    with ENGINE.begin() as c:
        c.execute(
            text("UPDATE products SET active=:a WHERE id=:id"),
            dict(a=active, id=product_id),
        )


def _norm(s):
    s = str(s).replace("ي", "ی").replace("ك", "ک")
    return " ".join(s.split())


def parse_stock_excel(file):
    """ستون ۱: برند (بالای هر گروه)، ستون ۲: مدل، ستون ۳: موجودی."""
    raw = pd.read_excel(file, header=None)
    warns, rows, brand = [], [], None
    for _, r in raw.iterrows():
        a = _norm(r[0]) if pd.notna(r[0]) else ""
        b = _norm(r[1]) if raw.shape[1] > 1 and pd.notna(r[1]) else ""
        c = r[2] if raw.shape[1] > 2 else None
        if a.lower() == "brand" and b.lower() == "model":
            continue
        if a:
            brand = a
        if not b:
            continue
        if brand is None:
            warns.append(f"«{b}»: برند ندارد، رد شد.")
            continue
        qty = pd.to_numeric(c, errors="coerce")
        if pd.isna(qty):
            warns.append(f"«{brand} {b}»: موجودی ندارد، صفر ثبت می‌شود.")
            qty = 0.0
        elif qty < 0:
            warns.append(f"«{brand} {b}»: موجودی منفی است، رد شد.")
            continue
        rows.append(dict(brand=brand, model=b, name=f"{brand} {b}", stock=round(float(qty), 3)))
    out = pd.DataFrame(rows, columns=["brand", "model", "name", "stock"])
    dup = out.duplicated("name", keep="first")
    if dup.any():
        warns.append(f"{int(dup.sum())} ردیف تکراری (برند و مدل یکسان) حذف شد.")
        out = out[~dup]
    return out.reset_index(drop=True), warns


def import_products(df, min_stock):
    """همه‌چیز در یک تراکنش؛ محصول تکراری رد می‌شود. تاریخچه را «ارسال‌شده» علامت می‌زند
    تا دکمه ارسال مجدد صدها پیام نفرستد."""
    added = skipped = 0
    with ENGINE.begin() as c:
        for r in df.itertuples(index=False):
            pid = c.execute(
                text(
                    """INSERT INTO products(name,brand,stock,min_stock)
                       VALUES(:n,:b,:s,:m) ON CONFLICT (name) DO NOTHING RETURNING id"""
                ),
                dict(n=r.name, b=r.brand, s=r.stock, m=min_stock),
            ).scalar()
            if pid is None:
                skipped += 1
                continue
            added += 1
            if r.stock > 0:
                c.execute(
                    text(
                        """INSERT INTO movements(product_id,kind,qty,stock_after,note,telegram_sent)
                           VALUES(:pid,'IN',:q,:q,'موجودی اولیه (ورود از اکسل)',TRUE)"""
                    ),
                    dict(pid=pid, q=r.stock),
                )
    return added, skipped


def apply_movement(product_id, kind, qty, note):
    """تراکنش اتمیک با قفل ردیف؛ از خروج بیش از موجودی جلوگیری می‌کند."""
    with ENGINE.begin() as c:
        row = c.execute(
            text("SELECT name, stock, min_stock FROM products WHERE id=:id AND active FOR UPDATE"),
            dict(id=product_id),
        ).one()
        qty = Decimal(str(round(float(qty), 3)))
        old = row.stock
        new = old + qty if kind == "IN" else old - qty
        if new < 0:
            raise ValueError(f"موجودی کافی نیست (موجودی فعلی: {fmt(old)})")
        c.execute(text("UPDATE products SET stock=:s WHERE id=:id"), dict(s=new, id=product_id))
        mid = c.execute(
            text(
                """INSERT INTO movements(product_id,kind,qty,stock_after,note)
                   VALUES(:pid,:k,:q,:s,:n) RETURNING id"""
            ),
            dict(pid=product_id, k=kind, q=qty, s=new, n=note),
        ).scalar()
    return mid, row.name, old, new, row.min_stock


# ---------------------------------------------------------------- تلگرام
def make_excel():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        products_df(include_inactive=True).drop(columns=["created_at"]).to_excel(
            w, sheet_name="موجودی", index=False
        )
        movements_df().to_excel(w, sheet_name="تاریخچه", index=False)
    buf.seek(0)
    return buf


def tg_post(method, **kwargs):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", timeout=60, **kwargs
    )


def report_text(kind, name, qty, old, new, note, when=None):
    icon = "📥 ورود به انبار" if kind == "IN" else "📤 خروج از انبار"
    when = when or datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    s = f"{icon}\nعطر: {name}\nمقدار: {fmt(qty)} کیلوگرم (KG)\nموجودی: {fmt(old)} ← {fmt(new)} کیلوگرم (KG)\nزمان: {when}"
    return s + (f"\nتوضیح: {note}" if note else "")


def send_telegram(text_msg, movement_ids=()):
    """ارسال متن + اکسل. فقط در صورت موفقیت، حرکت‌ها «ارسال‌شده» علامت می‌خورند."""
    if not BOT_TOKEN or not CHAT_ID:
        return False, "توکن یا چت‌آیدی تلگرام تنظیم نشده است."
    try:
        r1 = tg_post("sendMessage", data={"chat_id": CHAT_ID, "text": text_msg})
        fname = f"inventory_{datetime.now(TZ):%Y%m%d_%H%M%S}.xlsx"
        r2 = tg_post(
            "sendDocument",
            data={"chat_id": CHAT_ID},
            files={"document": (fname, make_excel())},
        )
        if r1.ok and r2.ok:
            if movement_ids:
                with ENGINE.begin() as c:
                    c.execute(
                        text("UPDATE movements SET telegram_sent=TRUE WHERE id = ANY(:ids)"),
                        dict(ids=list(movement_ids)),
                    )
            return True, "گزارش به تلگرام ارسال شد."
        return False, f"خطای تلگرام: {r1.text[:100]} {r2.text[:100]}"
    except requests.RequestException as e:
        return False, f"خطا در اتصال به تلگرام: {e}"


def retry_unsent():
    with ENGINE.connect() as c:
        rows = c.execute(
            text(
                """SELECT m.id, p.name, m.kind, m.qty, m.stock_after, m.note, m.created_at
                   FROM movements m JOIN products p ON p.id=m.product_id
                   WHERE NOT m.telegram_sent ORDER BY m.id"""
            )
        ).all()
    if not rows:
        return True, "همه تغییرات قبلاً ارسال شده‌اند."
    lines = []
    for r in rows:
        old = r.stock_after - r.qty if r.kind == "IN" else r.stock_after + r.qty
        when = r.created_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(report_text(r.kind, r.name, r.qty, old, r.stock_after, r.note, when))
    return send_telegram(
        f"🔁 ارسال مجدد {len(rows)} تغییر ارسال‌نشده\n\n" + "\n\n".join(lines),
        [r.id for r in rows],
    )


# ---------------------------------------------------------------- ظاهر و استایل
GOLD, WINE, TEAL, SAND, RED = "#D4AF6A", "#C2587A", "#4FB3A9", "#E8A07F", "#E5534B"
PALETTE = [GOLD, WINE, TEAL, "#7FA7D9", "#B48EDB", "#E3C98F", SAND, "#8FA3AD"]
STATUS_COLORS = {"عادی": TEAL, "کم‌موجودی": SAND, "تمام‌شده": RED}
KIND_FA = {"IN": "ورود", "OUT": "خروج"}
KIND_COLORS = {"ورود": TEAL, "خروج": WINE}
WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
WEEKDAY_INDEX = {5: "شنبه", 6: "یکشنبه", 0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنجشنبه", 4: "جمعه"}

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700&display=swap');
:root { --bg:#100E14; --card:#1A1621; --card2:#221C2B; --line:#2E2836; --txt:#EDE3D3; --muted:#A99C92; --gold:#D4AF6A; --wine:#C2587A; }
.stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stMarkdown, .stCaption,
[data-testid="stMetric"], [data-baseweb="tab"] {
    font-family: 'Vazirmatn', Tahoma, sans-serif !important;
}
.stApp { direction: rtl; background: radial-gradient(1200px 600px at 85% -10%, #2A1422 0%, var(--bg) 55%); color: var(--txt); }
.stApp p, .stApp label, .stApp li, .stMarkdown, [data-testid="stWidgetLabel"] p { color: var(--txt); }
.stCaption, [data-testid="stCaptionContainer"] { color: var(--muted) !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.6rem; max-width: 1250px; }
#MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }
input, textarea { text-align: right; }

.hero {
    background: linear-gradient(135deg, #3B1020 0%, #5E1B35 55%, #7C2C4A 100%);
    border: 1px solid rgba(212,175,106,.25);
    border-radius: 20px; padding: 26px 32px; margin-bottom: 18px;
    color: #F6EBD9; display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 12px 34px rgba(0,0,0,.45); position: relative; overflow: hidden;
}
.hero::after { content: "🧴"; position: absolute; left: 24px; bottom: -22px; font-size: 120px; opacity: .10; }
.hero h1 { margin: 0; font-size: 1.9rem; font-weight: 700; color: #F6EBD9; padding: 0; }
.hero p { margin: 4px 0 0; color: var(--gold); font-size: .95rem; }
.hero .date { text-align: left; font-size: .9rem; color: var(--gold); line-height: 1.9; z-index: 1; }

.kpi {
    background: var(--card); border-radius: 16px; padding: 16px 18px; display: flex; gap: 14px;
    align-items: center; box-shadow: 0 4px 18px rgba(0,0,0,.35);
    border: 1px solid var(--line); margin-bottom: 14px; border-right: 5px solid var(--gold);
}
.kpi.warn { border-right-color: #E8A07F; } .kpi.bad { border-right-color: #E5534B; }
.kpi.good { border-right-color: #4FB3A9; } .kpi.wine { border-right-color: #C2587A; }
.kpi-ic { font-size: 1.7rem; background: var(--card2); border-radius: 12px; width: 52px; height: 52px;
          display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.kpi-l { color: var(--muted); font-size: .82rem; }
.kpi-v { color: #FFF6E6; font-size: 1.45rem; font-weight: 700; line-height: 1.3; }
.kpi-v small { font-size: .75rem; font-weight: 400; color: var(--muted); margin-right: 5px; }

.section-title { font-size: 1.05rem; font-weight: 700; color: var(--gold); margin: 10px 0 6px;
                 padding-right: 10px; border-right: 4px solid var(--wine); }
.chip { display: inline-block; background: rgba(232,160,127,.15); color: #F0B89B; border: 1px solid rgba(232,160,127,.35);
        border-radius: 20px; padding: 4px 12px; margin: 3px; font-size: .85rem; }
.chip.bad { background: rgba(229,83,75,.15); color: #FF8F88; border-color: rgba(229,83,75,.4); }

[data-baseweb="tab-list"] { gap: 6px; background: var(--card); padding: 6px; border-radius: 14px; border: 1px solid var(--line); }
[data-baseweb="tab"] { border-radius: 10px; padding: 8px 18px; height: auto; color: var(--muted); background: transparent; }
[data-baseweb="tab"][aria-selected="true"] { background: var(--card2); color: var(--gold); font-weight: 700;
                                             box-shadow: 0 2px 10px rgba(0,0,0,.4); }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none; }

[data-testid="stForm"] { background: var(--card); border: 1px solid var(--line); border-radius: 18px;
                         padding: 22px; box-shadow: 0 4px 18px rgba(0,0,0,.35); }
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div, textarea {
    background-color: var(--card2) !important; color: var(--txt) !important; border-color: var(--line) !important; border-radius: 10px !important;
}
input { color: var(--txt) !important; }
.stButton > button, [data-testid="stFormSubmitButton"] > button, .stDownloadButton > button {
    border-radius: 12px; border: 1px solid var(--gold); color: var(--gold); background: transparent; font-weight: 500; padding: .45rem 1.2rem;
}
[data-testid="stFormSubmitButton"] > button {
    background: linear-gradient(135deg, #8E3358, #C2587A); color: #fff; border: none; width: 100%; padding: .6rem;
}
.stButton > button:hover, .stDownloadButton > button:hover { background: var(--gold); color: #1A1621; border-color: var(--gold); }
[data-testid="stExpander"] { background: var(--card); border-radius: 14px; border: 1px solid var(--line); }
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; border: 1px solid var(--line); }
[data-testid="stAlert"] { border-radius: 12px; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- توابع نمودار (فقط داده ← شکل)
def now_local():
    return datetime.now(TZ).replace(tzinfo=None)


def num(x, d=2):
    s = f"{float(x):,.{d}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def today_fa():
    try:
        import jdatetime

        j = jdatetime.datetime.fromgregorian(datetime=now_local())
        return f"{j.year}/{j.month:02d}/{j.day:02d}"
    except Exception:
        return now_local().strftime("%Y-%m-%d")


def style(fig, h=380):
    fig.update_layout(
        font=dict(family="Vazirmatn, Tahoma, sans-serif", size=13, color="#EDE3D3"),
        height=h,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.18, title_text=""),
        colorway=PALETTE,
    )
    fig.update_xaxes(showgrid=False, title_text=fig.layout.xaxis.title.text or "")
    fig.update_yaxes(gridcolor="#2E2836", zerolinecolor="#2E2836")
    return fig


def prep_products(pdf):
    d = pdf.copy()
    d["brand"] = d["brand"].replace("", "بدون برند")
    d["status"] = "عادی"
    d.loc[d["stock"] <= d["min_stock"], "status"] = "کم‌موجودی"
    d.loc[d["stock"] <= 0, "status"] = "تمام‌شده"
    d["value"] = d["stock"] * d["price"]
    return d


def prep_moves(mdf, include_initial=False):
    m = mdf.copy()
    if m.empty:
        return m
    m["brand"] = m["brand"].replace("", "بدون برند")
    m["kind_fa"] = m["kind"].map(KIND_FA)
    m["signed"] = m["qty"].where(m["kind"] == "IN", -m["qty"])
    m["initial"] = m["note"].str.startswith("موجودی اولیه")
    return m if include_initial else m[~m["initial"]]


def in_period(m, days):
    if days is None or m.empty:
        return m
    return m[m["created_at"] >= now_local() - timedelta(days=days)]


def fig_stock_bar(p, n=15):
    d = p.sort_values("stock", ascending=False).head(n).sort_values("stock")
    d["txt"] = d["stock"].map(fmt)
    fig = px.bar(d, x="stock", y="name", orientation="h", color="status", text="txt",
                 color_discrete_map=STATUS_COLORS, labels={"stock": "موجودی (KG)", "name": "", "status": ""})
    fig.update_traces(textposition="outside", cliponaxis=False)
    return style(fig, max(320, 30 * len(d) + 90))


def fig_brand_donut(p, col="stock", title_unit="KG"):
    g = p.groupby("brand", as_index=False)[col].sum()
    g = g[g[col] > 0]
    if g.empty:
        return None
    fig = px.pie(g, names="brand", values=col, hole=0.55)
    fig.update_traces(textinfo="percent", hovertemplate="%{label}<br>%{value:,.2f} " + title_unit + "<extra></extra>")
    return style(fig, 340)


def fig_status_donut(p):
    g = p.groupby("status", as_index=False).size()
    fig = px.pie(g, names="status", values="size", hole=0.55, color="status", color_discrete_map=STATUS_COLORS)
    fig.update_traces(textinfo="label+value")
    return style(fig, 340)


def fig_treemap(p):
    d = p[p["stock"] > 0]
    if d.empty:
        return None
    fig = px.treemap(d, path=[px.Constant("انبار"), "brand", "name"], values="stock", color="brand",
                     color_discrete_sequence=PALETTE)
    fig.update_traces(textinfo="label+value", hovertemplate="%{label}<br>%{value:,.2f} KG<extra></extra>")
    return style(fig, 460)


def fig_value_brand(p):
    g = p.groupby("brand", as_index=False)["value"].sum().sort_values("value", ascending=False)
    if g["value"].sum() <= 0:
        return None
    fig = px.bar(g, x="brand", y="value", labels={"brand": "", "value": "ارزش (تومان)"}, color_discrete_sequence=[GOLD])
    return style(fig, 340)


def fig_daily(m):
    if m.empty:
        return None
    m = m.assign(day=m["created_at"].dt.normalize())
    d = m.groupby(["day", "kind_fa"], as_index=False)["qty"].sum()
    fig = px.bar(d, x="day", y="qty", color="kind_fa", barmode="group", color_discrete_map=KIND_COLORS,
                 labels={"day": "", "qty": "مقدار (KG)", "kind_fa": ""})
    net = m.groupby("day", as_index=False)["signed"].sum()
    fig.add_scatter(x=net["day"], y=net["signed"], mode="lines+markers", name="خالص تغییر",
                    line=dict(color=GOLD, width=3))
    return style(fig, 380)


def fig_cumulative(mall):
    if mall.empty:
        return None
    d = mall.sort_values("created_at").copy()
    d["total"] = d["signed"].cumsum()
    fig = px.area(d, x="created_at", y="total", labels={"created_at": "", "total": "کل موجودی انبار (KG)"},
                  color_discrete_sequence=[WINE])
    fig.update_traces(line_shape="hv", fillcolor="rgba(194,88,122,.22)")
    return style(fig, 340)


def fig_heatmap(m):
    o = m[m["kind"] == "OUT"]
    if o.empty:
        return None
    o = o.assign(wd=o["created_at"].dt.dayofweek.map(WEEKDAY_INDEX), hr=o["created_at"].dt.hour)
    pv = o.pivot_table(index="wd", columns="hr", values="qty", aggfunc="sum", fill_value=0)
    pv = pv.reindex(index=WEEKDAYS, columns=range(24), fill_value=0)
    fig = px.imshow(pv, aspect="auto", color_continuous_scale=["#1C1822", "#7A5A2E", GOLD, "#F3D9A0"],
                    labels=dict(x="ساعت", y="", color="KG"))
    return style(fig, 340)


def fig_top_out(m, by="name", n=10):
    o = m[m["kind"] == "OUT"]
    if o.empty:
        return None
    g = o.groupby(by, as_index=False)["qty"].sum().nlargest(n, "qty").sort_values("qty")
    fig = px.bar(g, x="qty", y=by, orientation="h", labels={"qty": "خروج (KG)", by: ""},
                 color_discrete_sequence=[WINE], text=g["qty"].map(fmt))
    fig.update_traces(textposition="outside", cliponaxis=False)
    return style(fig, max(300, 32 * len(g) + 80))


def fig_trend(mall, names):
    d = mall[mall["name"].isin(names)]
    if d.empty:
        return None
    fig = px.line(d, x="created_at", y="stock_after", color="name", markers=True, line_shape="hv",
                  labels={"created_at": "", "stock_after": "موجودی (KG)", "name": ""})
    return style(fig, 380)


def cover_df(p, mall, window=30, n=10):
    o = mall[(mall["kind"] == "OUT") & (mall["created_at"] >= now_local() - timedelta(days=window))]
    if o.empty:
        return None
    rate = (o.groupby("name")["qty"].sum() / window).rename("rate")
    d = p.set_index("name").join(rate, how="inner")
    d = d[d["rate"] > 0].copy()
    d["days_left"] = d["stock"] / d["rate"]
    return d.reset_index().nsmallest(n, "days_left")


def fig_cover(p, mall):
    d = cover_df(p, mall)
    if d is None or d.empty:
        return None
    d = d.sort_values("days_left", ascending=False)
    fig = px.bar(d, x="days_left", y="name", orientation="h", labels={"days_left": "روز تا اتمام موجودی", "name": ""},
                 color_discrete_sequence=[SAND], text=d["days_left"].map(lambda x: num(x, 0)))
    fig.update_traces(textposition="outside", cliponaxis=False)
    return style(fig, max(300, 32 * len(d) + 80))


def slow_movers(p, mall, days=60):
    last_out = mall[mall["kind"] == "OUT"].groupby("name")["created_at"].max().rename("آخرین خروج")
    d = p[p["stock"] > 0].set_index("name").join(last_out)
    cutoff = now_local() - timedelta(days=days)
    d = d[d["آخرین خروج"].isna() | (d["آخرین خروج"] < cutoff)]
    d = d.reset_index()[["name", "brand", "stock", "آخرین خروج"]]
    d.columns = ["عطر", "برند", "موجودی (KG)", "آخرین خروج"]
    return d.sort_values("موجودی (KG)", ascending=False)


def show(fig, empty="داده کافی برای این نمودار وجود ندارد."):
    if fig is None:
        st.info(empty)
    else:
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def kpi(icon, label, value, unit="", tone=""):
    return (f'<div class="kpi {tone}"><div class="kpi-ic">{icon}</div><div><div class="kpi-l">{label}</div>'
            f'<div class="kpi-v">{value}<small>{unit}</small></div></div></div>')


def title(t):
    st.markdown(f'<div class="section-title">{t}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- رابط
st.markdown(
    f"""<div class="hero"><div><h1>مدیریت انبار عطرفروشی</h1>
    <p>ثبت لحظه‌ای ورود و خروج، گزارش خودکار تلگرام و تحلیل موجودی</p></div>
    <div class="date">📅 {today_fa()}<br>🕒 {now_local():%H:%M}</div></div>""",
    unsafe_allow_html=True,
)

tab_dash, tab_move, tab_prod, tab_charts, tab_data = st.tabs(
    ["📊 داشبورد", "📦 ورود / خروج", "🧴 محصولات", "📈 تحلیل و نمودارها", "🗂️ داده‌ها و پشتیبان"]
)

# ------------------------- داشبورد
with tab_dash:
    P = prep_products(products_df())
    M_all = prep_moves(movements_df(), include_initial=True)
    M = prep_moves(movements_df())
    if P.empty:
        st.info("هنوز محصولی ثبت نشده است. از تب «محصولات» شروع کنید.")
    else:
        low = P[P["status"] == "کم‌موجودی"]
        out_ = P[P["status"] == "تمام‌شده"]
        today = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
        td = M[M["created_at"] >= today] if not M.empty else M
        tin = td.loc[td["kind"] == "IN", "qty"].sum() if not td.empty else 0
        tout = td.loc[td["kind"] == "OUT", "qty"].sum() if not td.empty else 0
        m30 = in_period(M, 30)
        out30 = m30.loc[m30["kind"] == "OUT", "qty"].sum() if not m30.empty else 0

        r1 = st.columns(4)
        r1[0].markdown(kpi("⚖️", "کل موجودی انبار", num(P["stock"].sum()), "KG"), unsafe_allow_html=True)
        r1[1].markdown(kpi("🧴", "تعداد محصول فعال", len(P), "عطر", "wine"), unsafe_allow_html=True)
        r1[2].markdown(kpi("⚠️", "کم‌موجودی", len(low), "عطر", "warn"), unsafe_allow_html=True)
        r1[3].markdown(kpi("⛔", "تمام‌شده", len(out_), "عطر", "bad"), unsafe_allow_html=True)
        r2 = st.columns(4)
        r2[0].markdown(kpi("📥", "ورود امروز", num(tin), "KG", "good"), unsafe_allow_html=True)
        r2[1].markdown(kpi("📤", "خروج امروز", num(tout), "KG", "wine"), unsafe_allow_html=True)
        r2[2].markdown(kpi("📆", "خروج ۳۰ روز اخیر", num(out30), "KG"), unsafe_allow_html=True)
        val = P["value"].sum()
        r2[3].markdown(kpi("💰", "ارزش موجودی", num(val, 0) if val > 0 else "—", "تومان" if val > 0 else ""),
                       unsafe_allow_html=True)

        a, b = st.columns([3, 2])
        with a:
            title("ورود و خروج ۱۴ روز اخیر")
            show(fig_daily(in_period(M, 14)), "در ۱۴ روز اخیر حرکتی ثبت نشده است.")
        with b:
            title("سهم برندها از موجودی")
            show(fig_brand_donut(P))

        a, b = st.columns([3, 2])
        with a:
            title("آخرین حرکت‌ها")
            if M_all.empty:
                st.info("هنوز حرکتی ثبت نشده است.")
            else:
                rec = M_all.iloc[::-1].head(8)[["created_at", "name", "kind_fa", "qty", "stock_after"]]
                rec.columns = ["زمان", "عطر", "نوع", "مقدار (KG)", "موجودی پس از عملیات"]
                st.dataframe(rec, use_container_width=True, hide_index=True)
        with b:
            title("نیازمند سفارش / شارژ")
            if low.empty and out_.empty:
                st.success("همه عطرها بالاتر از حد هشدار هستند. ✅")
            else:
                chips = "".join(f'<span class="chip bad">{r.name} · {fmt(r.stock)}</span>' for r in out_.itertuples())
                chips += "".join(f'<span class="chip">{r.name} · {fmt(r.stock)}</span>' for r in low.itertuples())
                st.markdown(chips, unsafe_allow_html=True)

# ------------------------- ورود / خروج
with tab_move:
    df = products_df()
    if df.empty:
        st.info("ابتدا از تب «محصولات» یک عطر اضافه کنید.")
    else:
        with st.form("move_form", clear_on_submit=True):
            choice = st.selectbox(
                "عطر",
                df["id"],
                format_func=lambda i: f"{df.loc[df.id == i, 'name'].iloc[0]} "
                f"(موجودی: {fmt(df.loc[df.id == i, 'stock'].iloc[0])} KG)",
            )
            kind_label = st.radio("نوع عملیات", ["ورود به انبار", "خروج از انبار"], horizontal=True)
            qty = st.number_input("مقدار (کیلوگرم - KG)", min_value=0.001, value=1.0, step=0.001, format="%.3f")
            note = st.text_input("توضیح (اختیاری)")
            ok = st.form_submit_button("ثبت عملیات")
        if ok:
            kind = "IN" if kind_label == "ورود به انبار" else "OUT"
            try:
                mid, name, old, new, min_stock = apply_movement(int(choice), kind, float(qty), note)
                st.success(f"✅ در دیتابیس ذخیره شد — {name}: {fmt(old)} ← {fmt(new)} KG")
                sent, msg = send_telegram(
                    report_text(kind, name, qty, old, new, note), [mid]
                )
                (st.success if sent else st.warning)(
                    msg if sent else msg + " (از تب «داده‌ها» می‌توانی دوباره بفرستی.)"
                )
                if new <= min_stock:
                    st.warning("⚠️ موجودی به حد هشدار رسیده است.")
            except ValueError as e:
                st.error(str(e))

        title("آخرین عملیات ثبت‌شده")
        _m = prep_moves(movements_df(), include_initial=True)
        if _m.empty:
            st.caption("هنوز عملیاتی ثبت نشده است.")
        else:
            _r = _m.iloc[::-1].head(6)[["created_at", "name", "kind_fa", "qty", "stock_after"]]
            _r.columns = ["زمان", "عطر", "نوع", "مقدار (KG)", "موجودی پس از عملیات"]
            st.dataframe(_r, use_container_width=True, hide_index=True)

# ------------------------- محصولات
with tab_prod:
    with st.form("add_prod", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("نام عطر (یکتا)")
        brand = c2.text_input("برند")
        c3, c4, c5, c6 = st.columns(4)
        size = c3.number_input("حجم (ml)", min_value=0, value=100, step=10)
        price = c4.number_input("قیمت فروش (تومان)", min_value=0, value=0, step=10000)
        stock = c5.number_input("موجودی اولیه (KG)", min_value=0.0, value=0.0, step=0.001, format="%.3f")
        min_stock = c6.number_input("حد هشدار (KG)", min_value=0.0, value=3.0, step=0.5)
        if st.form_submit_button("افزودن محصول"):
            if not name.strip():
                st.error("نام عطر را وارد کنید.")
            else:
                try:
                    add_product(name.strip(), brand.strip(), int(size), int(price), float(stock), float(min_stock))
                    st.success("محصول اضافه شد.")
                    st.rerun()
                except IntegrityError:
                    st.error("این نام قبلاً ثبت شده است.")

    with st.expander("📥 ورود دسته‌ای موجودی از فایل اکسل"):
        st.caption(
            "ستون اول: نام برند (بالای هر گروه) — ستون دوم: مدل — ستون سوم: موجودی. "
            "نام محصول به‌صورت «برند مدل» ثبت می‌شود. محصولاتی که قبلاً ثبت شده‌اند رد می‌شوند."
        )
        up = st.file_uploader("فایل اکسل (xlsx)", type=["xlsx"], key="stock_xlsx")
        imp_min = st.number_input(
            "حد هشدار پیش‌فرض برای محصولات واردشده", min_value=0.0, value=1.0, step=0.5, key="imp_min"
        )
        if up is not None:
            try:
                imp_df, warns = parse_stock_excel(up)
            except Exception as e:
                st.error(f"خواندن فایل ناموفق بود: {e}")
            else:
                st.write(
                    f"{len(imp_df)} محصول از {imp_df['brand'].nunique()} برند — "
                    f"مجموع موجودی: {fmt(imp_df['stock'].sum())}"
                )
                for w in warns:
                    st.warning(w)
                st.dataframe(imp_df, use_container_width=True, hide_index=True)
                if st.button("ورود به دیتابیس", key="do_import"):
                    added, skipped = import_products(imp_df, float(imp_min))
                    st.success(f"✅ {added} محصول جدید ثبت شد؛ {skipped} مورد تکراری رد شد.")
                    sent, msg = send_telegram(
                        f"📦 ورود اولیه از اکسل\n{added} محصول جدید ثبت شد ({skipped} تکراری)\n"
                        f"مجموع موجودی فایل: {fmt(imp_df['stock'].sum())} کیلوگرم (KG)\n"
                        f"زمان: {datetime.now(TZ):%Y-%m-%d %H:%M}"
                    )
                    (st.success if sent else st.warning)(msg)

    title("لیست محصولات")
    _p = prep_products(products_df())
    if not _p.empty:
        q = st.text_input("🔎 جستجو در نام یا برند", key="prod_search")
        if q.strip():
            _p = _p[_p["name"].str.contains(q.strip(), case=False, na=False)
                    | _p["brand"].str.contains(q.strip(), case=False, na=False)]
        _show = _p[["name", "brand", "size_ml", "price", "stock", "min_stock", "status"]]
        _show.columns = ["نام عطر", "برند", "حجم (ml)", "قیمت (تومان)", "موجودی (KG)", "حد هشدار (KG)", "وضعیت"]
        st.dataframe(_show, use_container_width=True, hide_index=True)
    else:
        st.caption("محصولی ثبت نشده است.")

    # حذف نرم: محصول هرگز پاک نمی‌شود تا تاریخچه سالم بماند
    with st.expander("غیرفعال / فعال‌سازی محصول (حذف نمی‌شود، تاریخچه حفظ می‌شود)"):
        allp = products_df(include_inactive=True)
        if not allp.empty:
            pid = st.selectbox(
                "محصول",
                allp["id"],
                format_func=lambda i: allp.loc[allp.id == i, "name"].iloc[0]
                + ("" if allp.loc[allp.id == i, "active"].iloc[0] else " (غیرفعال)"),
                key="toggle_sel",
            )
            is_active = bool(allp.loc[allp.id == pid, "active"].iloc[0])
            if st.button("غیرفعال کن" if is_active else "فعال کن"):
                set_active(int(pid), not is_active)
                st.rerun()

# ------------------------- تحلیل و نمودارها
with tab_charts:
    P = prep_products(products_df())
    raw_m = movements_df()
    if P.empty:
        st.info("هنوز داده‌ای برای تحلیل وجود ندارد.")
    else:
        f1, f2 = st.columns([3, 2])
        period = f1.radio("بازه زمانی", ["۷ روز اخیر", "۳۰ روز اخیر", "۹۰ روز اخیر", "همه"], index=1, horizontal=True)
        with_init = f2.checkbox("شامل موجودی اولیه (ورود از اکسل)", value=False)
        days = {"۷ روز اخیر": 7, "۳۰ روز اخیر": 30, "۹۰ روز اخیر": 90, "همه": None}[period]
        M_all = prep_moves(raw_m, include_initial=True)
        M = in_period(prep_moves(raw_m, include_initial=with_init), days)

        t1, t2, t3 = st.tabs(["🧴 وضعیت موجودی", "🔄 گردش کالا", "🔬 تحلیل پیشرفته"])

        with t1:
            title("موجودی هر عطر")
            n = st.slider("تعداد عطر در نمودار", 5, min(50, max(6, len(P))), min(15, max(5, len(P))))
            show(fig_stock_bar(P, n))
            a, b = st.columns(2)
            with a:
                title("وضعیت موجودی عطرها")
                show(fig_status_donut(P))
            with b:
                title("سهم برندها از موجودی (KG)")
                show(fig_brand_donut(P))
            title("نقشه درختی: برند ← عطر")
            show(fig_treemap(P))
            if P["value"].sum() > 0:
                title("ارزش موجودی به تفکیک برند (تومان)")
                show(fig_value_brand(P))

        with t2:
            title("ورود و خروج روزانه و خالص تغییر")
            show(fig_daily(M), "در این بازه حرکتی ثبت نشده است.")
            title("روند کل موجودی انبار")
            show(fig_cumulative(M_all))
            a, b = st.columns(2)
            with a:
                title("پرفروش‌ترین عطرها (خروج)")
                show(fig_top_out(M, "name"), "در این بازه خروجی ثبت نشده است.")
            with b:
                title("پرفروش‌ترین برندها (خروج)")
                show(fig_top_out(M, "brand"), "در این بازه خروجی ثبت نشده است.")

        with t3:
            title("چه روز و ساعتی بیشترین خروج را داریم؟")
            show(fig_heatmap(M), "در این بازه خروجی ثبت نشده است.")
            title("تخمین زمان اتمام موجودی (بر اساس میانگین خروج ۳۰ روز اخیر)")
            show(fig_cover(P, M_all), "برای تخمین، حداقل یک خروج در ۳۰ روز اخیر لازم است.")
            title("روند موجودی عطرهای انتخابی")
            default = list(M_all[M_all["kind"] == "OUT"]["name"].value_counts().head(3).index) if not M_all.empty else []
            picked = st.multiselect("عطرها", list(P["name"]), default=default)
            show(fig_trend(M_all, picked) if picked else None, "یک یا چند عطر انتخاب کنید.")
            title("کم‌گردش‌ها: موجودی دارند ولی مدتی خروج نداشته‌اند")
            sm_days = st.select_slider("حداقل مدت بدون خروج (روز)", [30, 60, 90, 180], value=60)
            slow = slow_movers(P, M_all, sm_days)
            if slow.empty:
                st.success("همه عطرهای دارای موجودی در این مدت فروش داشته‌اند. ✅")
            else:
                st.dataframe(slow, use_container_width=True, hide_index=True)

# ------------------------- داده‌ها و پشتیبان
with tab_data:
    title("تاریخچه تغییرات")
    st.dataframe(movements_df().iloc[::-1], use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns(3)
    c1.download_button(
        "دانلود اکسل (پشتیبان)",
        make_excel(),
        file_name=f"inventory_{datetime.now(TZ):%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    if c2.button("ارسال گزارش کامل به تلگرام"):
        ok, msg = send_telegram(f"📊 گزارش کامل انبار - {datetime.now(TZ):%Y-%m-%d %H:%M}")
        (st.success if ok else st.error)(msg)
    if c3.button("ارسال مجدد تغییرات ارسال‌نشده"):
        ok, msg = retry_unsent()
        (st.success if ok else st.error)(msg)
