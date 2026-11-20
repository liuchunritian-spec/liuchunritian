"""仓库管家 v2 —— Flask + SQLite 后端

数据真正落库：warehouse.db 是一个 SQLite 数据库文件，
断电不丢、支持并发、可用任何数据库工具打开。
"""
import csv
import io
import os
import sqlite3
import time

from flask import Flask, Response, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "warehouse.db")

app = Flask(__name__, static_folder="static", static_url_path="")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS goods (
                sku TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                loc TEXT DEFAULT '',
                unit TEXT DEFAULT '件',
                safe INTEGER DEFAULT 0,
                cost REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('in','out')),
                sku TEXT NOT NULL,
                qty INTEGER NOT NULL,
                note TEXT DEFAULT '',
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_sku ON records(sku);
            """
        )
        # v2 -> v3 迁移：老库没有 cost 列就补上
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(goods)")]
        if "cost" not in cols:
            conn.execute("ALTER TABLE goods ADD COLUMN cost REAL DEFAULT 0")


def goods_with_stock(conn):
    rows = conn.execute(
        """
        SELECT g.*,
            COALESCE(SUM(CASE WHEN r.type='in' THEN r.qty ELSE -r.qty END), 0) AS stock,
            (SELECT COUNT(*) FROM records WHERE sku=g.sku) AS rec_count
        FROM goods g LEFT JOIN records r ON r.sku=g.sku
        GROUP BY g.sku ORDER BY g.sku
        """
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/goods")
def list_goods():
    with db() as conn:
        return jsonify(goods_with_stock(conn))


@app.post("/api/goods")
def add_goods():
    d = request.get_json(force=True)
    sku = str(d.get("sku", "")).strip()
    name = str(d.get("name", "")).strip()
    if not sku or not name:
        return jsonify({"error": "SKU 和品名都要填"}), 400
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO goods(sku,name,loc,unit,safe,cost) VALUES(?,?,?,?,?,?)",
                (sku, name, str(d.get("loc", "")).strip(),
                 str(d.get("unit") or "件").strip(),
                 max(0, int(d.get("safe") or 0)),
                 max(0.0, float(d.get("cost") or 0))),
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": f"SKU {sku} 已存在"}), 409
    return jsonify({"ok": True}), 201


@app.patch("/api/goods/<sku>")
def update_goods(sku):
    """单字段更新：loc / unit / safe / cost"""
    d = request.get_json(force=True)
    field = d.get("field")
    allowed = {"loc": str, "unit": str, "safe": int, "cost": float}
    if field not in allowed:
        return jsonify({"error": "只允许改 loc / unit / safe / cost"}), 400
    raw = d.get("value")
    try:
        value = allowed[field](raw)
    except (TypeError, ValueError):
        return jsonify({"error": "值的格式不对"}), 400
    with db() as conn:
        cur = conn.execute(f"UPDATE goods SET {field}=? WHERE sku=?", (value, sku))
        if cur.rowcount == 0:
            return jsonify({"error": f"SKU {sku} 不存在"}), 404
    return jsonify({"ok": True})


@app.delete("/api/goods/<sku>")
def delete_goods(sku):
    with db() as conn:
        conn.execute("DELETE FROM goods WHERE sku=?", (sku,))
    return jsonify({"ok": True})


@app.get("/api/records")
def list_records():
    sku = request.args.get("sku", "")
    limit = min(int(request.args.get("limit") or 500), 5000)
    sql = ("SELECT r.*, g.name FROM records r LEFT JOIN goods g ON g.sku=r.sku")
    with db() as conn:
        if sku:
            rows = conn.execute(sql + " WHERE r.sku=? ORDER BY r.id DESC LIMIT ?", (sku, limit)).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/records")
def add_record():
    d = request.get_json(force=True)
    rtype = d.get("type")
    sku = str(d.get("sku", "")).strip()
    try:
        qty = int(d.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if rtype not in ("in", "out"):
        return jsonify({"error": "类型必须是 in 或 out"}), 400
    if not sku or qty <= 0:
        return jsonify({"error": "SKU 必填，数量要大于 0"}), 400
    with db() as conn:
        if not conn.execute("SELECT 1 FROM goods WHERE sku=?", (sku,)).fetchone():
            return jsonify({"error": "SKU 不存在，请先在商品档案添加"}), 404
        if rtype == "out":
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN type='in' THEN qty ELSE -qty END),0) s "
                "FROM records WHERE sku=?", (sku,),
            ).fetchone()
            if qty > row["s"]:
                return jsonify({"error": f"出库超过当前库存（现有 {row['s']}），已拦截"}), 400
        cur = conn.execute(
            "INSERT INTO records(type,sku,qty,note,ts) VALUES(?,?,?,?,?)",
            (rtype, sku, qty, str(d.get("note", "")).strip(), time.time()),
        )
        rid = cur.lastrowid
    return jsonify({"ok": True, "id": rid}), 201


@app.delete("/api/records/<int:rid>")
def delete_record(rid):
    with db() as conn:
        conn.execute("DELETE FROM records WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.get("/api/stats")
def stats():
    today_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    with db() as conn:
        goods = goods_with_stock(conn)
        total = sum(g["stock"] for g in goods)
        low = sum(1 for g in goods if g["stock"] <= g["safe"])
        today = conn.execute(
            "SELECT COUNT(*) c FROM records WHERE ts>=?", (today_start,)
        ).fetchone()["c"]
        value = sum(g["stock"] * (g["cost"] or 0) for g in goods)
    return jsonify({"sku_count": len(goods), "total_qty": total, "low": low,
                    "today": today, "value": round(value, 2)})


def _csv_response(rows, headers):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment"})


@app.get("/api/export/stock.csv")
def export_stock():
    with db() as conn:
        goods = goods_with_stock(conn)
    return _csv_response(
        [[g["sku"], g["name"], g["loc"], g["unit"], g["stock"], g["safe"],
          g["cost"], round(g["stock"] * (g["cost"] or 0), 2)] for g in goods],
        ["SKU", "品名", "库位", "单位", "当前库存", "安全库存", "成本", "货值"],
    )


@app.get("/api/export/flow.csv")
def export_flow():
    with db() as conn:
        rows = conn.execute(
            "SELECT r.*, g.name FROM records r LEFT JOIN goods g ON g.sku=r.sku ORDER BY r.id"
        ).fetchall()
    return _csv_response(
        [[time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
          "入库" if r["type"] == "in" else "出库", r["sku"], r["name"] or "", r["qty"], r["note"]]
         for r in rows],
        ["时间", "类型", "SKU", "品名", "数量", "备注"],
    )


@app.get("/api/backup")
def backup():
    with db() as conn:
        data = {
            "goods": [dict(r) for r in conn.execute("SELECT * FROM goods")],
            "records": [dict(r) for r in conn.execute("SELECT type,sku,qty,note,ts FROM records")],
        }
    return jsonify(data)


@app.post("/api/backup")
def restore():
    d = request.get_json(force=True)
    if "goods" not in d or "records" not in d:
        return jsonify({"error": "备份文件格式不对"}), 400
    with db() as conn:
        conn.execute("DELETE FROM records")
        conn.execute("DELETE FROM goods")
        for g in d["goods"]:
            conn.execute(
                "INSERT OR REPLACE INTO goods(sku,name,loc,unit,safe,cost) VALUES(?,?,?,?,?,?)",
                (g["sku"], g["name"], g.get("loc", ""), g.get("unit", "件"),
                 g.get("safe", 0), g.get("cost", 0)),
            )
        for r in d["records"]:
            conn.execute(
                "INSERT INTO records(type,sku,qty,note,ts) VALUES(?,?,?,?,?)",
                (r["type"], r["sku"], r["qty"], r.get("note", ""), r["ts"]),
            )
    return jsonify({"ok": True})


# ────────── v3：Excel / CSV 批量导入 ──────────
HEADER_MAP = {
    "sku": ["sku", "SKU", "母件", "SKU编码", "货号", "编码", "产品编号", "asin", "ASIN"],
    "name": ["品名", "名称", "商品名", "产品名", "name", "Name", "描述", "标题"],
    "loc": ["库位", "位置", "货架", "loc", "储位"],
    "unit": ["单位", "unit", "计量单位"],
    "safe": ["安全库存", "补货点", "预警库存", "safe"],
    "cost": ["成本", "单价", "进价", "采购价", "成本价", "cost", "Cost"],
}


def _match_header(h):
    h = str(h).strip()
    for key, names in HEADER_MAP.items():
        for n in names:
            if h == n or h.lower() == n.lower() or (n.lower() in h.lower() and len(h) <= len(n) + 6):
                return key
    return None


def _to_num(v, cast):
    try:
        if v is None or str(v).strip() == "":
            return None
        return cast(float(str(v).replace(",", "").replace("¥", "").replace("$", "").strip()))
    except (TypeError, ValueError):
        return None


def _parse_rows(filename, stream):
    """返回 list[dict]，字段为 sku/name/loc/unit/safe/cost 的规范名"""
    if filename.lower().endswith(".csv"):
        text = stream.read().decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        raw = list(reader)
    else:  # xlsx
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(stream.read()), data_only=True, read_only=True)
        raw = []
        for ws in wb.worksheets:
            for r in ws.iter_rows(values_only=True):
                if any(v is not None and str(v).strip() for v in r):
                    raw.append(list(r))
    rows = []
    if not raw:
        return rows
    # 找表头行：含「SKU」列的那一行
    header_idx, mapping = None, {}
    for i, r in enumerate(raw[:20]):
        m = {}
        for j, c in enumerate(r):
            if c is not None:
                k = _match_header(c)
                if k and k not in m:
                    m[k] = j
        if "sku" in m:
            header_idx, mapping = i, m
            break
    if header_idx is None:
        raise ValueError("找不到表头行——前 20 行内要有一行含「SKU」列")
    for r in raw[header_idx + 1:]:
        def cell(key):
            idx = mapping.get(key)
            if idx is None or idx >= len(r):
                return None
            return r[idx]
        sku = str(cell("sku") or "").strip()
        if not sku or sku.lower() == "none":
            continue
        rows.append({
            "sku": sku,
            "name": str(cell("name") or "").strip() or sku,
            "loc": str(cell("loc") or "").strip(),
            "unit": str(cell("unit") or "").strip() or "件",
            "safe": _to_num(cell("safe"), int) or 0,
            "cost": _to_num(cell("cost"), float) or 0,
        })
    return rows


@app.post("/api/import")
def import_goods():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "没有收到文件"}), 400
    try:
        rows = _parse_rows(f.filename, f)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"解析失败：{e}"}), 400
    if not rows:
        return jsonify({"error": "文件里没有有效数据行"}), 400
    added = updated = 0
    errors = []
    with db() as conn:
        for r in rows:
            try:
                exist = conn.execute("SELECT 1 FROM goods WHERE sku=?", (r["sku"],)).fetchone()
                if exist:
                    conn.execute(
                        "UPDATE goods SET name=?, loc=CASE WHEN ?<>'' THEN ? ELSE loc END,"
                        " unit=CASE WHEN ?<>'' THEN ? ELSE unit END,"
                        " safe=?, cost=CASE WHEN ?>0 THEN ? ELSE cost END WHERE sku=?",
                        (r["name"], r["loc"], r["loc"], r["unit"], r["unit"],
                         r["safe"], r["cost"], r["cost"], r["sku"]),
                    )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO goods(sku,name,loc,unit,safe,cost) VALUES(?,?,?,?,?,?)",
                        (r["sku"], r["name"], r["loc"], r["unit"], r["safe"], r["cost"]),
                    )
                    added += 1
            except Exception as e:
                errors.append(f"{r['sku']}: {e}")
    return jsonify({"ok": True, "added": added, "updated": updated,
                    "skipped": len(errors), "errors": errors[:10]})


@app.get("/api/import-template.csv")
def import_template():
    return _csv_response(
        [["SSB-AG-101840", "Cabot 澳洲木油", "A-01", "件", 10, 8.5],
         ["SSB-AG-101905", "Pip 胰岛素笔针头", "A-02", "盒", 20, 8.39]],
        ["SKU", "品名", "库位", "单位", "安全库存", "成本"],
    )


init_db()

if __name__ == "__main__":
    print("仓库管家 v3 已启动 → http://127.0.0.1:8787")
    app.run(host="127.0.0.1", port=8787, debug=False)
