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
                safe INTEGER DEFAULT 0
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
                "INSERT INTO goods(sku,name,loc,unit,safe) VALUES(?,?,?,?,?)",
                (sku, name, str(d.get("loc", "")).strip(),
                 str(d.get("unit") or "件").strip(),
                 max(0, int(d.get("safe") or 0))),
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": f"SKU {sku} 已存在"}), 409
    return jsonify({"ok": True}), 201


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
    return jsonify({"sku_count": len(goods), "total_qty": total, "low": low, "today": today})


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
        [[g["sku"], g["name"], g["loc"], g["unit"], g["stock"], g["safe"]] for g in goods],
        ["SKU", "品名", "库位", "单位", "当前库存", "安全库存"],
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
                "INSERT OR REPLACE INTO goods(sku,name,loc,unit,safe) VALUES(?,?,?,?,?)",
                (g["sku"], g["name"], g.get("loc", ""), g.get("unit", "件"), g.get("safe", 0)),
            )
        for r in d["records"]:
            conn.execute(
                "INSERT INTO records(type,sku,qty,note,ts) VALUES(?,?,?,?,?)",
                (r["type"], r["sku"], r["qty"], r.get("note", ""), r["ts"]),
            )
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    print("仓库管家 v2 已启动 → http://127.0.0.1:8787")
    app.run(host="127.0.0.1", port=8787, debug=False)
