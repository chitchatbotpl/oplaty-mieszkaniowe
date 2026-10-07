from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
import os
import sqlite3
from pathlib import Path
from datetime import datetime
import csv, io

try:
    import psycopg
except ImportError:
    psycopg = None

BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / "oplaty.db"

app = Flask(__name__)

CATEGORIES = [
    ("czynsz", "Czynsz"),
    ("woda", "Woda"),
    ("ogrzewanie", "Ogrzewanie"),
    ("prad", "Prąd"),
    ("smieci", "Śmieci"),
    ("inne", "Inne"),
]

def get_db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL jest ustawiony, ale brakuje pakietu psycopg.")
        # Render/other hosts may provide postgres://; psycopg expects postgresql://
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        con = psycopg.connect(url, row_factory=psycopg.rows.dict_row)
        return con
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def db_execute(con, sql, params=()):
    # Convert a small subset of SQLite placeholders to PostgreSQL placeholders.
    if os.environ.get("DATABASE_URL", "").strip():
        sql = sql.replace("?", "%s")
    return con.execute(sql, params)

def db_script_init(con):
    if os.environ.get("DATABASE_URL", "").strip():
        db_execute(con,"""
        CREATE TABLE IF NOT EXISTS apartments (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT,
            owner TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)
        db_execute(con,"""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            apartment_id INTEGER NOT NULL REFERENCES apartments(id),
            period TEXT NOT NULL,
            czynsz DOUBLE PRECISION NOT NULL DEFAULT 0,
            woda DOUBLE PRECISION NOT NULL DEFAULT 0,
            ogrzewanie DOUBLE PRECISION NOT NULL DEFAULT 0,
            prad DOUBLE PRECISION NOT NULL DEFAULT 0,
            smieci DOUBLE PRECISION NOT NULL DEFAULT 0,
            inne DOUBLE PRECISION NOT NULL DEFAULT 0,
            paid DOUBLE PRECISION NOT NULL DEFAULT 0,
            paid_date DATE,
            notes TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(apartment_id, period)
        )
        """)
    else:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS apartments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            owner TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apartment_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            czynsz REAL NOT NULL DEFAULT 0,
            woda REAL NOT NULL DEFAULT 0,
            ogrzewanie REAL NOT NULL DEFAULT 0,
            prad REAL NOT NULL DEFAULT 0,
            smieci REAL NOT NULL DEFAULT 0,
            inne REAL NOT NULL DEFAULT 0,
            paid REAL NOT NULL DEFAULT 0,
            paid_date TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(apartment_id, period)
        );
        """)



def init_db():
    con = get_db()
    db_script_init(con)
    count = db_execute(con, "SELECT COUNT(*) AS n FROM apartments").fetchone()["n"]
    if count == 0:
        for row in [
            ("Mieszkanie 01", "ul. Przykładowa 1/1", ""),
            ("Mieszkanie 02", "ul. Przykładowa 1/2", ""),
            ("Mieszkanie 03", "ul. Przykładowa 1/3", ""),
        ]:
            db_execute(con, "INSERT INTO apartments(name,address,owner) VALUES (?,?,?)", row)
    con.commit()
    con.close()


@app.template_filter("money")
def money(v):
    return f"{float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", " ")

@app.route("/")
def index():
    con = get_db()
    apartments = db_execute(con,"SELECT * FROM apartments WHERE active=1 ORDER BY name").fetchall()
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    rows = db_execute(con,"""
        SELECT p.*, a.name AS apartment_name,
        (p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne) AS total,
        ((p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne)-p.paid) AS balance
        FROM payments p JOIN apartments a ON a.id=p.apartment_id
        WHERE p.period=? ORDER BY a.name
    """, (month,)).fetchall()
    summary = db_execute(con,"""
        SELECT
        COALESCE(SUM(czynsz+woda+ogrzewanie+prad+smieci+inne),0) AS total,
        COALESCE(SUM(paid),0) AS paid,
        COALESCE(SUM((czynsz+woda+ogrzewanie+prad+smieci+inne)-paid),0) AS balance
        FROM payments WHERE period=?
    """, (month,)).fetchone()
    con.close()
    return render_template("index.html", apartments=apartments, rows=rows, summary=summary, month=month)

@app.route("/payments")
def payments():
    con = get_db()
    month = request.args.get("month", "")
    apartment_id = request.args.get("apartment_id", "")
    sql = """
        SELECT p.*, a.name AS apartment_name,
        (p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne) AS total,
        ((p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne)-p.paid) AS balance
        FROM payments p JOIN apartments a ON a.id=p.apartment_id WHERE 1=1
    """
    args=[]
    if month:
        sql += " AND p.period=?"; args.append(month)
    if apartment_id:
        sql += " AND p.apartment_id=?"; args.append(apartment_id)
    sql += " ORDER BY p.period DESC, a.name"
    rows = db_execute(con,sql, args).fetchall()
    apartments = db_execute(con,"SELECT * FROM apartments WHERE active=1 ORDER BY name").fetchall()
    con.close()
    return render_template("payments.html", rows=rows, apartments=apartments, month=month, apartment_id=apartment_id)

@app.route("/payment/new", methods=["GET","POST"])
def new_payment():
    con = get_db()
    if request.method == "POST":
        data = request.form
        try:
            db_execute(con,"""
                INSERT INTO payments
                (apartment_id,period,czynsz,woda,ogrzewanie,prad,smieci,inne,paid,paid_date,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data["apartment_id"], data["period"],
                float(data.get("czynsz") or 0), float(data.get("woda") or 0),
                float(data.get("ogrzewanie") or 0), float(data.get("prad") or 0),
                float(data.get("smieci") or 0), float(data.get("inne") or 0),
                float(data.get("paid") or 0), data.get("paid_date") or None,
                data.get("notes","")
            ))
            con.commit()
            con.close()
            return redirect(url_for("payments", month=data["period"]))
        except Exception:
            con.close()
            return render_template("payment_form.html", apartments=con_apartments(), error="Dla tego mieszkania i miesiąca istnieje już wpis.")
    apartments = db_execute(con,"SELECT * FROM apartments WHERE active=1 ORDER BY name").fetchall()
    con.close()
    return render_template("payment_form.html", apartments=apartments, payment=None, error=None)

def con_apartments():
    con=get_db()
    a=db_execute(con,"SELECT * FROM apartments WHERE active=1 ORDER BY name").fetchall()
    con.close()
    return a

@app.route("/payment/<int:payment_id>/edit", methods=["GET","POST"])
def edit_payment(payment_id):
    con=get_db()
    if request.method=="POST":
        d=request.form
        db_execute(con,"""
            UPDATE payments SET apartment_id=?, period=?, czynsz=?, woda=?, ogrzewanie=?,
            prad=?, smieci=?, inne=?, paid=?, paid_date=?, notes=? WHERE id=?
        """, (
            d["apartment_id"], d["period"], float(d.get("czynsz") or 0),
            float(d.get("woda") or 0), float(d.get("ogrzewanie") or 0),
            float(d.get("prad") or 0), float(d.get("smieci") or 0),
            float(d.get("inne") or 0), float(d.get("paid") or 0),
            d.get("paid_date") or None, d.get("notes",""), payment_id
        ))
        con.commit(); con.close()
        return redirect(url_for("payments"))
    payment=db_execute(con,"SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    apartments=db_execute(con,"SELECT * FROM apartments WHERE active=1 ORDER BY name").fetchall()
    con.close()
    return render_template("payment_form.html", apartments=apartments, payment=payment, error=None)

@app.post("/payment/<int:payment_id>/delete")
def delete_payment(payment_id):
    con=get_db()
    db_execute(con,"DELETE FROM payments WHERE id=?", (payment_id,))
    con.commit(); con.close()
    return redirect(url_for("payments"))

@app.route("/apartments", methods=["GET","POST"])
def apartments():
    con=get_db()
    if request.method=="POST":
        d=request.form
        db_execute(con,"INSERT INTO apartments(name,address,owner,notes) VALUES (?,?,?,?)",
                    (d["name"],d.get("address",""),d.get("owner",""),d.get("notes","")))
        con.commit()
    rows=db_execute(con,"SELECT * FROM apartments ORDER BY active DESC,name").fetchall()
    con.close()
    return render_template("apartments.html", apartments=rows)

@app.post("/apartment/<int:apartment_id>/toggle")
def toggle_apartment(apartment_id):
    con=get_db()
    db_execute(con,"UPDATE apartments SET active=1-active WHERE id=?", (apartment_id,))
    con.commit(); con.close()
    return redirect(url_for("apartments"))

@app.route("/reports")
def reports():
    con=get_db()
    year=request.args.get("year", datetime.now().strftime("%Y"))
    rows=db_execute(con,"""
      SELECT period,
      SUM(czynsz) czynsz,SUM(woda) woda,SUM(ogrzewanie) ogrzewanie,
      SUM(prad) prad,SUM(smieci) smieci,SUM(inne) inne,SUM(paid) paid,
      SUM(czynsz+woda+ogrzewanie+prad+smieci+inne) total,
      SUM(czynsz+woda+ogrzewanie+prad+smieci+inne-paid) balance
      FROM payments WHERE period LIKE ? GROUP BY period ORDER BY period
    """,(year+"-%",)).fetchall()
    con.close()
    return render_template("reports.html", rows=rows, year=year)

@app.route("/export.csv")
def export_csv():
    con=get_db()
    rows=db_execute(con,"""
      SELECT p.period,a.name AS apartment,p.czynsz,p.woda,p.ogrzewanie,p.prad,p.smieci,p.inne,p.paid,
      (p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne) AS total,
      ((p.czynsz+p.woda+p.ogrzewanie+p.prad+p.smieci+p.inne)-p.paid) AS balance
      FROM payments p JOIN apartments a ON a.id=p.apartment_id ORDER BY p.period DESC,a.name
    """).fetchall()
    con.close()
    out=io.StringIO()
    w=csv.writer(out, delimiter=';')
    w.writerow(["Okres","Mieszkanie","Czynsz","Woda","Ogrzewanie","Prąd","Śmieci","Inne","Zapłacono","Razem","Saldo"])
    for r in rows: w.writerow(list(r))
    mem=io.BytesIO(out.getvalue().encode("utf-8-sig"))
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="oplaty.csv")

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)