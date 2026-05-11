#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import sqlite3
import sys
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APP_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "faka.db"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "18080"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "change-me-now")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
RESERVATION_TTL = max(5, int(os.environ.get("RESERVATION_TTL_MINUTES", "120"))) * 60
MAX_BODY = 2 * 1024 * 1024


def utc_now() -> int:
    return int(time.time())


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def money(cents: int | str | float) -> str:
    return f"{int(cents) / 100:.2f}"


def cents_from_text(value: str) -> int:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("price is empty")
    if "." in raw:
        left, right = raw.split(".", 1)
        right = (right + "00")[:2]
        return int(left or "0") * 100 + int(right)
    return int(raw) * 100


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def new_order_no() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.localtime()) + secrets.token_hex(3)


def sign_value(value: str) -> str:
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def signed_session(user: str, expires: int) -> str:
    encoded_user = urllib.parse.quote(user, safe="")
    payload = f"{encoded_user}.{expires}"
    return f"{payload}.{sign_value(payload)}"


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        encoded_user, expires_text, sig = token.split(".", 2)
        payload = f"{encoded_user}.{expires_text}"
        if not hmac.compare_digest(sign_value(payload), sig):
            return False
        user = urllib.parse.unquote(encoded_user)
        return user == ADMIN_USER and int(expires_text) > utc_now()
    except Exception:
        return False


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price_cents INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    pay_type TEXT NOT NULL,
                    contact TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    card_id INTEGER,
                    provider_trade_no TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER,
                    FOREIGN KEY(product_id) REFERENCES products(id)
                );

                CREATE TABLE IF NOT EXISTS cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    order_id INTEGER,
                    reserved_until INTEGER,
                    sold_at INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES products(id),
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                );

                CREATE INDEX IF NOT EXISTS idx_cards_product_status
                    ON cards(product_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_orders_status_created
                    ON orders(status, created_at);
                """
            )
            defaults = {
                "site_name": os.environ.get("SITE_NAME", "轻量发卡网"),
                "base_url": os.environ.get("BASE_URL", ""),
                "epay_url": os.environ.get("EPAY_URL", ""),
                "epay_pid": os.environ.get("EPAY_PID", ""),
                "epay_key": os.environ.get("EPAY_KEY", ""),
            }
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )

    def settings(self) -> dict[str, str]:
        with self.connect() as conn:
            return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}

    def update_settings(self, values: dict[str, str]) -> None:
        with self.connect() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def cleanup_expired(self, conn: sqlite3.Connection | None = None) -> None:
        close_conn = conn is None
        if conn is None:
            conn = self.connect()
        now = utc_now()
        try:
            conn.execute(
                """
                UPDATE cards
                   SET status='available', order_id=NULL, reserved_until=NULL
                 WHERE status='reserved' AND reserved_until IS NOT NULL AND reserved_until < ?
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE orders
                   SET status='expired'
                 WHERE status='pending' AND created_at < ?
                """,
                (now - RESERVATION_TTL,),
            )
            if close_conn:
                conn.commit()
        finally:
            if close_conn:
                conn.close()

    def public_products(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            return list(
                conn.execute(
                    """
                    SELECT p.*,
                           SUM(CASE WHEN c.status='available' THEN 1 ELSE 0 END) AS stock
                      FROM products p
                 LEFT JOIN cards c ON c.product_id = p.id
                     WHERE p.active = 1
                  GROUP BY p.id
                  ORDER BY p.sort_order ASC, p.id DESC
                    """
                )
            )

    def product_rows(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            return list(
                conn.execute(
                    """
                    SELECT p.*,
                           SUM(CASE WHEN c.status='available' THEN 1 ELSE 0 END) AS stock,
                           SUM(CASE WHEN c.status='reserved' THEN 1 ELSE 0 END) AS reserved,
                           SUM(CASE WHEN c.status='sold' THEN 1 ELSE 0 END) AS sold
                      FROM products p
                 LEFT JOIN cards c ON c.product_id = p.id
                  GROUP BY p.id
                  ORDER BY p.sort_order ASC, p.id DESC
                    """
                )
            )

    def create_product(self, name: str, description: str, price_cents: int, active: bool, sort_order: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO products(name, description, price_cents, active, sort_order, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (name.strip(), description.strip(), price_cents, int(active), sort_order, utc_now()),
            )

    def update_product(
        self,
        product_id: int,
        name: str,
        description: str,
        price_cents: int,
        active: bool,
        sort_order: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE products
                   SET name=?, description=?, price_cents=?, active=?, sort_order=?
                 WHERE id=?
                """,
                (name.strip(), description.strip(), price_cents, int(active), sort_order, product_id),
            )

    def import_cards(self, product_id: int, text: str) -> int:
        lines = []
        seen = set()
        for line in text.replace("\r", "\n").split("\n"):
            code = line.strip()
            if code and code not in seen:
                seen.add(code)
                lines.append(code)
        if not lines:
            return 0
        with self.connect() as conn:
            product = conn.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise ValueError("product not found")
            for code in lines:
                conn.execute(
                    """
                    INSERT INTO cards(product_id, code, status, created_at)
                    VALUES(?, ?, 'available', ?)
                    """,
                    (product_id, code, utc_now()),
                )
        return len(lines)

    def delete_available_cards(self, product_id: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM cards WHERE product_id=? AND status='available'",
                (product_id,),
            )
            return int(cur.rowcount or 0)

    def cards_for_product(self, product_id: int, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            return list(
                conn.execute(
                    """
                    SELECT c.*, o.order_no
                      FROM cards c
                 LEFT JOIN orders o ON o.id = c.order_id
                     WHERE c.product_id=?
                  ORDER BY c.id DESC
                     LIMIT ?
                    """,
                    (product_id, limit),
                )
            )

    def create_order(self, product_id: int, pay_type: str, contact: str) -> str:
        if pay_type not in {"alipay", "wxpay"}:
            raise ValueError("payment type is invalid")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.cleanup_expired(conn)
            product = conn.execute(
                "SELECT * FROM products WHERE id=? AND active=1",
                (product_id,),
            ).fetchone()
            if not product:
                raise ValueError("product is not available")
            card = conn.execute(
                """
                SELECT * FROM cards
                 WHERE product_id=? AND status='available'
              ORDER BY id ASC
                 LIMIT 1
                """,
                (product_id,),
            ).fetchone()
            if not card:
                raise ValueError("out of stock")
            order_no = new_order_no()
            cur = conn.execute(
                """
                INSERT INTO orders(order_no, product_id, product_name, price_cents,
                                   pay_type, contact, status, card_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    order_no,
                    product["id"],
                    product["name"],
                    product["price_cents"],
                    pay_type,
                    contact.strip(),
                    card["id"],
                    utc_now(),
                ),
            )
            order_id = int(cur.lastrowid)
            conn.execute(
                """
                UPDATE cards
                   SET status='reserved', order_id=?, reserved_until=?
                 WHERE id=?
                """,
                (order_id, utc_now() + RESERVATION_TTL, card["id"]),
            )
            return order_no

    def order_by_no(self, order_no: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            return conn.execute(
                """
                SELECT o.*, c.code AS card_code, c.status AS card_status
                  FROM orders o
             LEFT JOIN cards c ON c.id = o.card_id
                 WHERE o.order_no=?
                """,
                (order_no.strip(),),
            ).fetchone()

    def recent_orders(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            return list(
                conn.execute(
                    """
                    SELECT o.*, c.code AS card_code
                      FROM orders o
                 LEFT JOIN cards c ON c.id = o.card_id
                  ORDER BY o.id DESC
                     LIMIT ?
                    """,
                    (limit,),
                )
            )

    def mark_paid(self, order_no: str, trade_no: str, paid_cents: int) -> tuple[bool, str]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute(
                "SELECT * FROM orders WHERE order_no=?",
                (order_no,),
            ).fetchone()
            if not order:
                return False, "order not found"
            if int(order["price_cents"]) != int(paid_cents):
                return False, "money mismatch"
            if order["status"] == "paid":
                return True, "already paid"

            card = None
            if order["card_id"]:
                card = conn.execute(
                    "SELECT * FROM cards WHERE id=? AND order_id=?",
                    (order["card_id"], order["id"]),
                ).fetchone()

            if card and card["status"] in {"reserved", "sold"}:
                card_id = card["id"]
                status = "paid"
                conn.execute(
                    """
                    UPDATE cards
                       SET status='sold', sold_at=?, reserved_until=NULL
                     WHERE id=?
                    """,
                    (utc_now(), card_id),
                )
            else:
                replacement = conn.execute(
                    """
                    SELECT * FROM cards
                     WHERE product_id=? AND status='available'
                  ORDER BY id ASC
                     LIMIT 1
                    """,
                    (order["product_id"],),
                ).fetchone()
                if replacement:
                    card_id = replacement["id"]
                    status = "paid"
                    conn.execute(
                        """
                        UPDATE cards
                           SET status='sold', order_id=?, sold_at=?, reserved_until=NULL
                         WHERE id=?
                        """,
                        (order["id"], utc_now(), card_id),
                    )
                else:
                    card_id = None
                    status = "paid_no_stock"

            conn.execute(
                """
                UPDATE orders
                   SET status=?, card_id=?, provider_trade_no=?, paid_at=?
                 WHERE id=?
                """,
                (status, card_id, trade_no, utc_now(), order["id"]),
            )
            return True, status

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            self.cleanup_expired(conn)
            data = {
                "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                "stock": conn.execute("SELECT COUNT(*) FROM cards WHERE status='available'").fetchone()[0],
                "pending": conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],
                "paid": conn.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0],
            }
            return {k: int(v) for k, v in data.items()}


store = Store(DB_PATH)


STATUS_LABEL = {
    "pending": "待支付",
    "paid": "已支付",
    "expired": "已过期",
    "paid_no_stock": "已支付/缺货",
}

PAY_LABEL = {"alipay": "支付宝", "wxpay": "微信"}


def epay_submit_url(epay_url: str) -> str:
    url = epay_url.strip().rstrip("/")
    if url.endswith(".php"):
        return url
    return url + "/submit.php"


def epay_sign(params: dict[str, str], key: str) -> str:
    pairs = []
    for name in sorted(params):
        if name in {"sign", "sign_type"}:
            continue
        value = params.get(name, "")
        if value == "":
            continue
        pairs.append(f"{name}={value}")
    raw = "&".join(pairs) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def parse_paid_cents(value: str) -> int:
    return cents_from_text(value)


class App(BaseHTTPRequestHandler):
    server_version = "LiteFaka/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def current_settings(self) -> dict[str, str]:
        return store.settings()

    def is_admin(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("lite_faka_session")
        return verify_session(token.value if token else None)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, title: str, body: str, status: int = 200, message: str = "") -> None:
        settings = self.current_settings()
        site_name = settings.get("site_name") or "轻量发卡网"
        admin_link = "后台" if self.is_admin() else "登录"
        banner = f"<div class='notice'>{e(message)}</div>" if message else ""
        html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} | {e(site_name)}</title>
  <style>
    :root {{
      --bg: #f6f7f8;
      --panel: #ffffff;
      --text: #1d2329;
      --muted: #667085;
      --line: #d7dde5;
      --accent: #1f7a5a;
      --accent-dark: #155c44;
      --danger: #b42318;
      --warn: #a15c07;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      line-height: 1.5;
    }}
    a {{ color: var(--accent-dark); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .topbar {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    .nav {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 14px 18px;
      display: flex;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
    }}
    .brand {{ font-weight: 700; color: var(--text); }}
    .nav-links {{ display: flex; gap: 12px; flex-wrap: wrap; font-size: 14px; }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 24px 18px 56px;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
    .card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .panel {{ margin-bottom: 16px; }}
    .row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .between {{ justify-content: space-between; }}
    .muted {{ color: var(--muted); }}
    .price {{ font-size: 24px; font-weight: 700; margin: 8px 0; }}
    .stock {{ color: var(--accent-dark); font-weight: 600; }}
    .empty {{ color: var(--danger); font-weight: 600; }}
    .notice {{
      border: 1px solid #b8dfcf;
      color: #0b5138;
      background: #ecfdf3;
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 16px;
    }}
    .warn {{
      border: 1px solid #f6d29b;
      color: var(--warn);
      background: #fff7ed;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 10px 0;
    }}
    input, textarea, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
      background: #fff;
    }}
    textarea {{ min-height: 110px; resize: vertical; }}
    label {{ display: block; margin: 10px 0 5px; font-weight: 600; font-size: 14px; }}
    button, .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 9px 14px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    button:hover, .button:hover {{ background: var(--accent-dark); text-decoration: none; }}
    .secondary {{ background: #eef2f6; color: var(--text); }}
    .secondary:hover {{ background: #dfe5ec; }}
    .danger {{ background: var(--danger); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 13px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
    .stat strong {{ display: block; font-size: 24px; }}
    .codebox {{
      white-space: pre-wrap;
      word-break: break-all;
      background: #101828;
      color: #f2f4f7;
      border-radius: 8px;
      padding: 14px;
      margin-top: 10px;
    }}
    .inline-form {{ display: inline; }}
    @media (max-width: 640px) {{
      .nav {{ align-items: flex-start; flex-direction: column; gap: 8px; }}
      .price {{ font-size: 20px; }}
      th, td {{ padding: 8px; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="nav">
      <a class="brand" href="/">{e(site_name)}</a>
      <nav class="nav-links">
        <a href="/">商品</a>
        <a href="/order">查订单</a>
        <a href="/admin">{admin_link}</a>
      </nav>
    </div>
  </header>
  <main>
    {banner}
    {body}
  </main>
</body>
</html>"""
        data = html_doc.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def parse_query(self) -> dict[str, str]:
        query = urllib.parse.urlparse(self.path).query
        parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def parse_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def request_base_url(self) -> str:
        settings = self.current_settings()
        base = (settings.get("base_url") or "").strip().rstrip("/")
        if base:
            return base
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"{proto}://{host}".rstrip("/")

    def require_admin(self) -> bool:
        if self.is_admin():
            return True
        self.redirect("/admin")
        return False

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/":
                self.home()
            elif path == "/health":
                self.send_text("ok\n")
            elif path == "/order":
                self.order_lookup()
            elif path == "/pay":
                self.pay_redirect()
            elif path == "/return":
                order_no = self.parse_query().get("out_trade_no", "")
                self.redirect(f"/order?order_no={urllib.parse.quote(order_no)}")
            elif path == "/notify":
                self.notify(self.parse_query())
            elif path == "/admin":
                self.admin_home()
            elif path == "/admin/products":
                if self.require_admin():
                    self.admin_products()
            elif path == "/admin/cards":
                if self.require_admin():
                    self.admin_cards()
            elif path == "/admin/orders":
                if self.require_admin():
                    self.admin_orders()
            elif path == "/admin/settings":
                if self.require_admin():
                    self.admin_settings()
            else:
                self.send_html("未找到", "<div class='panel'><h1>404</h1><p>页面不存在。</p></div>", 404)
        except Exception as exc:
            self.send_html("出错了", f"<div class='panel'><h1>出错了</h1><p>{e(exc)}</p></div>", 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            form = self.parse_form()
            if path == "/order":
                self.create_order(form)
            elif path == "/notify":
                self.notify(form)
            elif path == "/admin/login":
                self.admin_login(form)
            elif path == "/admin/logout":
                self.clear_session()
            elif path.startswith("/admin/") and not self.is_admin():
                self.redirect("/admin")
            elif path == "/admin/products/create":
                store.create_product(
                    form.get("name", ""),
                    form.get("description", ""),
                    cents_from_text(form.get("price", "")),
                    form.get("active") == "1",
                    int(form.get("sort_order", "0") or "0"),
                )
                self.redirect("/admin/products?msg=" + urllib.parse.quote("商品已创建"))
            elif path == "/admin/products/update":
                store.update_product(
                    int(form.get("id", "0")),
                    form.get("name", ""),
                    form.get("description", ""),
                    cents_from_text(form.get("price", "")),
                    form.get("active") == "1",
                    int(form.get("sort_order", "0") or "0"),
                )
                self.redirect("/admin/products?msg=" + urllib.parse.quote("商品已更新"))
            elif path == "/admin/cards/import":
                count = store.import_cards(int(form.get("product_id", "0")), form.get("codes", ""))
                self.redirect("/admin/cards?product_id=%s&msg=%s" % (form.get("product_id", "0"), urllib.parse.quote(f"已导入 {count} 张卡密")))
            elif path == "/admin/cards/delete-available":
                product_id = int(form.get("product_id", "0"))
                count = store.delete_available_cards(product_id)
                self.redirect(f"/admin/cards?product_id={product_id}&msg=" + urllib.parse.quote(f"已删除 {count} 张未售卡密"))
            elif path == "/admin/settings":
                settings = {
                    "site_name": form.get("site_name", "").strip() or "轻量发卡网",
                    "base_url": form.get("base_url", "").strip().rstrip("/"),
                    "epay_url": form.get("epay_url", "").strip().rstrip("/"),
                    "epay_pid": form.get("epay_pid", "").strip(),
                }
                if form.get("epay_key", "").strip():
                    settings["epay_key"] = form.get("epay_key", "").strip()
                store.update_settings(settings)
                self.redirect("/admin/settings?msg=" + urllib.parse.quote("设置已保存"))
            else:
                self.send_html("未找到", "<div class='panel'><h1>404</h1><p>页面不存在。</p></div>", 404)
        except Exception as exc:
            self.send_html("出错了", f"<div class='panel'><h1>提交失败</h1><p>{e(exc)}</p></div>", 400)

    def home(self) -> None:
        products = store.public_products()
        cards = []
        for p in products:
            stock = int(p["stock"] or 0)
            buy_form = ""
            if stock > 0:
                buy_form = f"""
                <form method="post" action="/order">
                  <input type="hidden" name="product_id" value="{p['id']}">
                  <label>联系方式或备注</label>
                  <input name="contact" placeholder="邮箱、微信或订单备注，可留空">
                  <label>支付方式</label>
                  <select name="pay_type">
                    <option value="alipay">支付宝</option>
                    <option value="wxpay">微信</option>
                  </select>
                  <div style="height:10px"></div>
                  <button type="submit">立即购买</button>
                </form>
                """
            else:
                buy_form = "<p class='empty'>暂时缺货</p>"
            cards.append(
                f"""
                <article class="card">
                  <div class="row between">
                    <h2 style="margin:0">{e(p['name'])}</h2>
                    <span class="{ 'stock' if stock else 'empty' }">库存 {stock}</span>
                  </div>
                  <div class="price">￥{money(p['price_cents'])}</div>
                  <p class="muted">{e(p['description'])}</p>
                  {buy_form}
                </article>
                """
            )
        if not cards:
            cards.append("<div class='panel'><p>暂无商品，请管理员登录后台添加商品和卡密。</p></div>")
        body = f"""
        <section class="panel">
          <h1 style="margin-top:0">商品列表</h1>
          <p class="muted">下单后请完成扫码支付，支付成功会自动显示卡密。订单号请自行保存。</p>
        </section>
        <section class="grid">{''.join(cards)}</section>
        """
        self.send_html("商品列表", body)

    def create_order(self, form: dict[str, str]) -> None:
        order_no = store.create_order(
            int(form.get("product_id", "0")),
            form.get("pay_type", "alipay"),
            form.get("contact", ""),
        )
        settings = self.current_settings()
        if settings.get("epay_url") and settings.get("epay_pid") and settings.get("epay_key"):
            self.redirect(f"/pay?order_no={urllib.parse.quote(order_no)}")
        else:
            self.redirect(f"/order?order_no={urllib.parse.quote(order_no)}")

    def order_lookup(self) -> None:
        order_no = self.parse_query().get("order_no", "").strip()
        if not order_no:
            self.send_html(
                "查订单",
                """
                <section class="panel">
                  <h1 style="margin-top:0">查询订单</h1>
                  <form method="get" action="/order">
                    <label>订单号</label>
                    <input name="order_no" placeholder="请输入订单号">
                    <div style="height:10px"></div>
                    <button type="submit">查询</button>
                  </form>
                </section>
                """,
            )
            return
        order = store.order_by_no(order_no)
        if not order:
            self.send_html("订单不存在", "<div class='panel'><h1>订单不存在</h1><p>请检查订单号。</p></div>", 404)
            return
        status = STATUS_LABEL.get(order["status"], order["status"])
        pay_again = ""
        if order["status"] == "pending":
            pay_again = f"<p><a class='button' href='/pay?order_no={e(order_no)}'>继续支付</a></p>"
        card = ""
        if order["status"] == "paid" and order["card_code"]:
            card = f"<h2>卡密</h2><div class='codebox'>{e(order['card_code'])}</div>"
        elif order["status"] == "paid_no_stock":
            card = "<div class='warn'>订单已支付，但当前库存不足。请联系管理员补发。</div>"
        body = f"""
        <section class="panel">
          <h1 style="margin-top:0">订单详情</h1>
          <p><strong>订单号：</strong>{e(order['order_no'])}</p>
          <p><strong>商品：</strong>{e(order['product_name'])}</p>
          <p><strong>金额：</strong>￥{money(order['price_cents'])}</p>
          <p><strong>支付方式：</strong>{PAY_LABEL.get(order['pay_type'], e(order['pay_type']))}</p>
          <p><strong>状态：</strong>{e(status)}</p>
          <p><strong>创建时间：</strong>{fmt_time(order['created_at'])}</p>
          <p><strong>支付时间：</strong>{fmt_time(order['paid_at'])}</p>
          {pay_again}
          {card}
        </section>
        """
        self.send_html("订单详情", body)

    def pay_redirect(self) -> None:
        order_no = self.parse_query().get("order_no", "").strip()
        order = store.order_by_no(order_no)
        if not order:
            self.send_html("订单不存在", "<div class='panel'><p>订单不存在。</p></div>", 404)
            return
        if order["status"] == "paid":
            self.redirect(f"/order?order_no={urllib.parse.quote(order_no)}")
            return
        settings = self.current_settings()
        if not (settings.get("epay_url") and settings.get("epay_pid") and settings.get("epay_key")):
            self.send_html(
                "支付未配置",
                "<div class='panel'><h1>支付未配置</h1><p>请先在后台配置易支付网关、商户 ID 和商户密钥。</p></div>",
                400,
            )
            return
        base = self.request_base_url()
        params = {
            "pid": settings["epay_pid"],
            "type": order["pay_type"],
            "out_trade_no": order["order_no"],
            "notify_url": base + "/notify",
            "return_url": base + "/return",
            "name": order["product_name"],
            "money": money(order["price_cents"]),
            "sitename": settings.get("site_name", "轻量发卡网"),
        }
        params["sign"] = epay_sign(params, settings["epay_key"])
        params["sign_type"] = "MD5"
        self.redirect(epay_submit_url(settings["epay_url"]) + "?" + urllib.parse.urlencode(params))

    def notify(self, params: dict[str, str]) -> None:
        settings = self.current_settings()
        if not settings.get("epay_key"):
            self.send_text("fail: epay not configured")
            return
        received = params.get("sign", "")
        expected = epay_sign(params, settings["epay_key"])
        if not hmac.compare_digest(received.lower(), expected.lower()):
            self.send_text("fail: bad sign")
            return
        trade_status = params.get("trade_status", "")
        if trade_status and trade_status != "TRADE_SUCCESS":
            self.send_text("success")
            return
        try:
            paid_cents = parse_paid_cents(params.get("money", "0"))
        except Exception:
            self.send_text("fail: bad money")
            return
        ok, msg = store.mark_paid(
            params.get("out_trade_no", ""),
            params.get("trade_no", ""),
            paid_cents,
        )
        self.send_text("success" if ok else f"fail: {msg}")

    def login_page(self, message: str = "") -> None:
        warn = ""
        if ADMIN_PASS == "change-me-now" or SECRET_KEY == "dev-secret-change-me":
            warn = "<div class='warn'>当前仍是默认后台密码或默认 SECRET_KEY，请部署时修改。</div>"
        body = f"""
        <section class="panel" style="max-width:420px">
          <h1 style="margin-top:0">管理员登录</h1>
          {warn}
          <form method="post" action="/admin/login">
            <label>用户名</label>
            <input name="username" value="{e(ADMIN_USER)}" autocomplete="username">
            <label>密码</label>
            <input name="password" type="password" autocomplete="current-password">
            <div style="height:10px"></div>
            <button type="submit">登录</button>
          </form>
        </section>
        """
        self.send_html("管理员登录", body, message=message)

    def admin_login(self, form: dict[str, str]) -> None:
        if form.get("username") == ADMIN_USER and hmac.compare_digest(form.get("password", ""), ADMIN_PASS):
            token = signed_session(ADMIN_USER, utc_now() + 12 * 3600)
            secure = " Secure;" if (self.headers.get("X-Forwarded-Proto") == "https") else ""
            self.send_response(302)
            self.send_header("Location", "/admin")
            self.send_header("Set-Cookie", f"lite_faka_session={token}; Path=/; HttpOnly; SameSite=Lax;{secure} Max-Age=43200")
            self.end_headers()
        else:
            self.login_page("用户名或密码错误")

    def clear_session(self) -> None:
        self.send_response(302)
        self.send_header("Location", "/admin")
        self.send_header("Set-Cookie", "lite_faka_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.end_headers()

    def admin_home(self) -> None:
        if not self.is_admin():
            self.login_page()
            return
        stats = store.stats()
        settings = self.current_settings()
        payment_warn = ""
        if not (settings.get("epay_url") and settings.get("epay_pid") and settings.get("epay_key")):
            payment_warn = "<div class='warn'>易支付还没有配置，用户可以创建订单，但不能自动跳转支付。</div>"
        body = f"""
        <section class="panel">
          <div class="row between">
            <h1 style="margin:0">后台</h1>
            <form class="inline-form" method="post" action="/admin/logout"><button class="secondary" type="submit">退出</button></form>
          </div>
          {payment_warn}
          <div class="stats">
            <div class="card stat"><span class="muted">商品</span><strong>{stats['products']}</strong></div>
            <div class="card stat"><span class="muted">可售库存</span><strong>{stats['stock']}</strong></div>
            <div class="card stat"><span class="muted">待支付</span><strong>{stats['pending']}</strong></div>
            <div class="card stat"><span class="muted">已支付</span><strong>{stats['paid']}</strong></div>
          </div>
        </section>
        <section class="grid">
          <a class="card" href="/admin/products"><h2>商品管理</h2><p class="muted">新增商品、修改价格和上下架。</p></a>
          <a class="card" href="/admin/cards"><h2>卡密管理</h2><p class="muted">按商品批量导入卡密，查看库存状态。</p></a>
          <a class="card" href="/admin/orders"><h2>订单管理</h2><p class="muted">查看支付状态和发货结果。</p></a>
          <a class="card" href="/admin/settings"><h2>支付设置</h2><p class="muted">配置易支付网关、商户 ID 和密钥。</p></a>
        </section>
        """
        self.send_html("后台", body)

    def admin_products(self) -> None:
        msg = self.parse_query().get("msg", "")
        rows = []
        for p in store.product_rows():
            active_checked = "checked" if p["active"] else ""
            rows.append(
                f"""
                <tr>
                  <td>{p['id']}</td>
                  <td>
                    <form method="post" action="/admin/products/update">
                      <input type="hidden" name="id" value="{p['id']}">
                      <label>名称</label><input name="name" value="{e(p['name'])}">
                      <label>描述</label><textarea name="description">{e(p['description'])}</textarea>
                      <div class="row">
                        <div style="flex:1"><label>价格</label><input name="price" value="{money(p['price_cents'])}"></div>
                        <div style="flex:1"><label>排序</label><input name="sort_order" value="{p['sort_order']}"></div>
                      </div>
                      <label><input style="width:auto" type="checkbox" name="active" value="1" {active_checked}> 上架</label>
                      <button type="submit">保存</button>
                    </form>
                  </td>
                  <td>可售 {int(p['stock'] or 0)}<br>锁定 {int(p['reserved'] or 0)}<br>已售 {int(p['sold'] or 0)}</td>
                  <td><a href="/admin/cards?product_id={p['id']}">管理卡密</a></td>
                </tr>
                """
            )
        body = f"""
        <section class="panel">
          <div class="row between"><h1 style="margin:0">商品管理</h1><a href="/admin">返回后台</a></div>
        </section>
        <section class="panel">
          <h2 style="margin-top:0">新增商品</h2>
          <form method="post" action="/admin/products/create">
            <label>名称</label><input name="name" required>
            <label>描述</label><textarea name="description"></textarea>
            <div class="row">
              <div style="flex:1"><label>价格</label><input name="price" placeholder="9.90" required></div>
              <div style="flex:1"><label>排序</label><input name="sort_order" value="0"></div>
            </div>
            <label><input style="width:auto" type="checkbox" name="active" value="1" checked> 上架</label>
            <button type="submit">创建商品</button>
          </form>
        </section>
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>商品</th><th>库存</th><th>操作</th></tr></thead>
            <tbody>{''.join(rows) or '<tr><td colspan="4">暂无商品</td></tr>'}</tbody>
          </table>
        </div>
        """
        self.send_html("商品管理", body, message=msg)

    def admin_cards(self) -> None:
        query = self.parse_query()
        msg = query.get("msg", "")
        products = store.product_rows()
        product_id = int(query.get("product_id") or (products[0]["id"] if products else 0) or 0)
        options = "".join(
            f"<option value='{p['id']}' {'selected' if p['id'] == product_id else ''}>{e(p['name'])}</option>"
            for p in products
        )
        rows = []
        if product_id:
            for c in store.cards_for_product(product_id):
                rows.append(
                    f"""
                    <tr>
                      <td>{c['id']}</td>
                      <td>{e(c['status'])}</td>
                      <td><code>{e(c['code'])}</code></td>
                      <td>{e(c['order_no'] or '')}</td>
                      <td>{fmt_time(c['created_at'])}</td>
                    </tr>
                    """
                )
        body = f"""
        <section class="panel">
          <div class="row between"><h1 style="margin:0">卡密管理</h1><a href="/admin">返回后台</a></div>
        </section>
        <section class="panel">
          <form method="get" action="/admin/cards">
            <label>商品</label>
            <div class="row">
              <select name="product_id" onchange="this.form.submit()">{options}</select>
              <button class="secondary" type="submit">切换</button>
            </div>
          </form>
        </section>
        <section class="panel">
          <h2 style="margin-top:0">批量导入</h2>
          <form method="post" action="/admin/cards/import">
            <input type="hidden" name="product_id" value="{product_id}">
            <label>卡密内容</label>
            <textarea name="codes" placeholder="一行一张卡密"></textarea>
            <div style="height:10px"></div>
            <button type="submit">导入卡密</button>
          </form>
        </section>
        <section class="panel">
          <form method="post" action="/admin/cards/delete-available" onsubmit="return confirm('确定删除该商品所有未售卡密吗？')">
            <input type="hidden" name="product_id" value="{product_id}">
            <button class="danger" type="submit">删除当前商品未售卡密</button>
          </form>
        </section>
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>状态</th><th>卡密</th><th>订单</th><th>导入时间</th></tr></thead>
            <tbody>{''.join(rows) or '<tr><td colspan="5">暂无卡密</td></tr>'}</tbody>
          </table>
        </div>
        """
        self.send_html("卡密管理", body, message=msg)

    def admin_orders(self) -> None:
        rows = []
        for o in store.recent_orders():
            rows.append(
                f"""
                <tr>
                  <td><a href="/order?order_no={e(o['order_no'])}" target="_blank">{e(o['order_no'])}</a></td>
                  <td>{e(o['product_name'])}</td>
                  <td>￥{money(o['price_cents'])}</td>
                  <td>{PAY_LABEL.get(o['pay_type'], e(o['pay_type']))}</td>
                  <td>{STATUS_LABEL.get(o['status'], e(o['status']))}</td>
                  <td>{e(o['contact'])}</td>
                  <td>{fmt_time(o['created_at'])}<br>{fmt_time(o['paid_at'])}</td>
                </tr>
                """
            )
        body = f"""
        <section class="panel">
          <div class="row between"><h1 style="margin:0">订单管理</h1><a href="/admin">返回后台</a></div>
        </section>
        <div class="table-wrap">
          <table>
            <thead><tr><th>订单号</th><th>商品</th><th>金额</th><th>支付</th><th>状态</th><th>备注</th><th>时间</th></tr></thead>
            <tbody>{''.join(rows) or '<tr><td colspan="7">暂无订单</td></tr>'}</tbody>
          </table>
        </div>
        """
        self.send_html("订单管理", body)

    def admin_settings(self) -> None:
        msg = self.parse_query().get("msg", "")
        settings = self.current_settings()
        body = f"""
        <section class="panel">
          <div class="row between"><h1 style="margin:0">支付设置</h1><a href="/admin">返回后台</a></div>
        </section>
        <section class="panel">
          <form method="post" action="/admin/settings">
            <label>站点名称</label>
            <input name="site_name" value="{e(settings.get('site_name', '轻量发卡网'))}">
            <label>站点外部地址</label>
            <input name="base_url" value="{e(settings.get('base_url', ''))}" placeholder="http://faka.example.com:18080">
            <p class="muted">用于生成支付回调地址。直连端口时填完整端口，反向代理 HTTPS 时填 HTTPS 域名。</p>
            <label>易支付网关</label>
            <input name="epay_url" value="{e(settings.get('epay_url', ''))}" placeholder="https://pay.example.com">
            <label>商户 ID</label>
            <input name="epay_pid" value="{e(settings.get('epay_pid', ''))}">
            <label>商户密钥</label>
            <input name="epay_key" type="password" placeholder="留空则不修改">
            <div style="height:10px"></div>
            <button type="submit">保存设置</button>
          </form>
        </section>
        """
        self.send_html("支付设置", body, message=msg)


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), App)
    print(f"Lite Faka listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
