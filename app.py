import io
import os
from datetime import datetime
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
    icon = "📥 ورود" if kind == "IN" else "📤 خروج"
    when = when or datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    s = f"{icon} به انبار\nعطر: {name}\nتعداد: {fmt(qty)}\nموجودی: {fmt(old)} ← {fmt(new)}\nزمان: {when}"
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


# ---------------------------------------------------------------- رابط
st.title("🧴 مدیریت انبار عطرفروشی")
tab_move, tab_prod, tab_charts, tab_data = st.tabs(
    ["ورود / خروج", "محصولات", "نمودارها", "داده‌ها و پشتیبان"]
)

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
                f"(موجودی: {fmt(df.loc[df.id == i, 'stock'].iloc[0])})",
            )
            kind_label = st.radio("نوع عملیات", ["ورود به انبار", "خروج از انبار"], horizontal=True)
            qty = st.number_input("تعداد", min_value=0.001, value=1.0, step=0.001, format="%.3f")
            note = st.text_input("توضیح (اختیاری)")
            ok = st.form_submit_button("ثبت")
        if ok:
            kind = "IN" if kind_label == "ورود به انبار" else "OUT"
            try:
                mid, name, old, new, min_stock = apply_movement(int(choice), kind, float(qty), note)
                st.success(f"✅ در دیتابیس ذخیره شد — {name}: {fmt(old)} ← {fmt(new)}")
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

# ------------------------- محصولات
with tab_prod:
    with st.form("add_prod", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("نام عطر (یکتا)")
        brand = c2.text_input("برند")
        c3, c4, c5, c6 = st.columns(4)
        size = c3.number_input("حجم (ml)", min_value=0, value=100, step=10)
        price = c4.number_input("قیمت فروش (تومان)", min_value=0, value=0, step=10000)
        stock = c5.number_input("موجودی اولیه", min_value=0.0, value=0.0, step=0.001, format="%.3f")
        min_stock = c6.number_input("حد هشدار", min_value=0.0, value=3.0, step=0.5)
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
                        f"مجموع موجودی فایل: {fmt(imp_df['stock'].sum())}\n"
                        f"زمان: {datetime.now(TZ):%Y-%m-%d %H:%M}"
                    )
                    (st.success if sent else st.warning)(msg)

    st.dataframe(
        products_df().drop(columns=["active", "created_at"]),
        use_container_width=True,
        hide_index=True,
    )

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

# ------------------------- نمودارها
with tab_charts:
    pdf, mdf = products_df(), movements_df()
    options = [
        "موجودی هر عطر",
        "عطرهای کم‌موجودی",
        "ورود و خروج روزانه",
        "پرفروش‌ترین عطرها",
        "ارزش موجودی به تفکیک برند",
        "روند موجودی هر عطر",
    ]
    chosen = st.multiselect("نمودارهای مورد نظر را انتخاب کنید", options)
    if st.button("نمایش نمودارها") and chosen:
        for ch in chosen:
            st.subheader(ch)
            if ch == options[0] and not pdf.empty:
                st.plotly_chart(px.bar(pdf, x="name", y="stock"), use_container_width=True)
            elif ch == options[1] and not pdf.empty:
                low = pdf[pdf["stock"] <= pdf["min_stock"]]
                if low.empty:
                    st.success("همه عطرها بالاتر از حد هشدار هستند.")
                else:
                    st.plotly_chart(px.bar(low, x="name", y="stock", color_discrete_sequence=["crimson"]), use_container_width=True)
            elif ch == options[2] and not mdf.empty:
                d = mdf.assign(day=mdf["created_at"].dt.date).groupby(["day", "kind"], as_index=False)["qty"].sum()
                st.plotly_chart(px.bar(d, x="day", y="qty", color="kind", barmode="group"), use_container_width=True)
            elif ch == options[3] and not mdf.empty:
                top = mdf[mdf.kind == "OUT"].groupby("name", as_index=False)["qty"].sum().nlargest(10, "qty")
                st.plotly_chart(px.bar(top, x="name", y="qty"), use_container_width=True)
            elif ch == options[4] and not pdf.empty:
                v = pdf.assign(value=pdf.stock * pdf.price).groupby("brand", as_index=False)["value"].sum()
                st.plotly_chart(px.pie(v, names="brand", values="value"), use_container_width=True)
            elif ch == options[5] and not mdf.empty:
                st.plotly_chart(px.line(mdf, x="created_at", y="stock_after", color="name", markers=True), use_container_width=True)
            else:
                st.info("داده کافی برای این نمودار وجود ندارد.")

# ------------------------- داده‌ها و پشتیبان
with tab_data:
    st.subheader("تاریخچه تغییرات")
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
