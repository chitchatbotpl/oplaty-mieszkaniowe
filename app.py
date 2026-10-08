from flask import Flask, render_template, request, redirect, url_for, send_file, flash, session
import os, sqlite3, csv, io
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from datetime import datetime

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'oplaty-dev-key')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
)
BASE_DIR = Path(__file__).resolve().parent
DB = BASE_DIR / 'oplaty.db'


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'admin':
            flash('Brak uprawnień administratora.')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {'current_user_email': session.get('email'), 'current_user_role': session.get('role')}


def using_postgres():
    return bool(os.environ.get('DATABASE_URL', '').strip())


def get_db():
    url = os.environ.get('DATABASE_URL', '').strip()
    if url:
        if psycopg is None:
            raise RuntimeError('DATABASE_URL jest ustawiony, ale brakuje pakietu psycopg.')
        if url.startswith('postgres://'):
            url = 'postgresql://' + url[len('postgres://'):]
        return psycopg.connect(url, row_factory=dict_row)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    return con


def db_execute(con, sql, params=()):
    if using_postgres():
        sql = sql.replace('?', '%s')
    return con.execute(sql, params)


def db_script_init(con):
    if using_postgres():
        statements = [
            '''CREATE TABLE IF NOT EXISTS apartments (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                address TEXT,
                owner TEXT,
                notes TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS sections (
                id SERIAL PRIMARY KEY,
                apartment_id INTEGER NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                unit TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS readings (
                id SERIAL PRIMARY KEY,
                section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
                period TEXT NOT NULL,
                reading DOUBLE PRECISION,
                previous_reading DOUBLE PRECISION,
                consumption DOUBLE PRECISION,
                rate DOUBLE PRECISION,
                amount_due DOUBLE PRECISION NOT NULL DEFAULT 0,
                paid INTEGER NOT NULL DEFAULT 0,
                paid_date DATE,
                notes TEXT DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(section_id, period)
            )'''
            ,'''CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )'''
        ]
        for s in statements:
            con.execute(s)
    else:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS apartments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            owner TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            apartment_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            unit TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(apartment_id) REFERENCES apartments(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            reading REAL,
            previous_reading REAL,
            consumption REAL,
            rate REAL,
            amount_due REAL NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            paid_date TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(section_id, period),
            FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')


def ensure_admin_user():
    email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    password = os.environ.get('ADMIN_PASSWORD', '')
    if not email or not password:
        return
    con = get_db()
    count = db_execute(con, 'SELECT COUNT(*) AS n FROM users').fetchone()['n']
    if count == 0:
        db_execute(con, 'INSERT INTO users(email,password_hash,role,active) VALUES (?,?,?,1)',
                   (email, generate_password_hash(password), 'admin'))
        con.commit()
        print(f'=== ADMIN USER CREATED: {email} ===')
    con.close()


def init_db():
    con = get_db()
    db_script_init(con)
    count = db_execute(con, 'SELECT COUNT(*) AS n FROM apartments').fetchone()['n']
    if count == 0:
        for row in [
            ('Mieszkanie 01', 'ul. Przykładowa 1/1', ''),
            ('Mieszkanie 02', 'ul. Przykładowa 1/2', ''),
            ('Mieszkanie 03', 'ul. Przykładowa 1/3', ''),
        ]:
            db_execute(con, 'INSERT INTO apartments(name,address,owner) VALUES (?,?,?)', row)
    con.commit(); con.close()


@app.template_filter('money')
def money(v):
    return f'{float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', ' ')


def get_apartment(con, apartment_id):
    return db_execute(con, 'SELECT * FROM apartments WHERE id=?', (apartment_id,)).fetchone()


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        con = get_db()
        user = db_execute(con, 'SELECT * FROM users WHERE email=? AND active=1', (email,)).fetchone()
        con.close()
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            session['email'] = user['email']
            session['role'] = user['role']
            next_url = request.form.get('next') or url_for('index')
            if not next_url.startswith('/') or next_url.startswith('//'):
                next_url = url_for('index')
            return redirect(next_url)
        flash('Nieprawidłowy e-mail lub hasło.')
    return render_template('login.html', next=request.args.get('next', ''))


@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/users', methods=['GET', 'POST'])
@admin_required
def users():
    con = get_db()
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user') if request.form.get('role') in ('user', 'admin') else 'user'
        if not email or not password:
            flash('E-mail i hasło są wymagane.')
        elif len(password) < 8:
            flash('Hasło musi mieć co najmniej 8 znaków.')
        else:
            try:
                db_execute(con, 'INSERT INTO users(email,password_hash,role,active) VALUES (?,?,?,1)',
                           (email, generate_password_hash(password), role))
                con.commit()
                flash('Użytkownik został dodany.')
            except Exception:
                con.rollback()
                flash('Nie udało się dodać użytkownika. Sprawdź, czy e-mail nie jest już zajęty.')
    users_rows = db_execute(con, 'SELECT id,email,role,active,created_at FROM users ORDER BY email').fetchall()
    con.close()
    return render_template('users.html', users=users_rows)


@app.post('/users/<int:user_id>/delete')
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        flash('Nie możesz usunąć własnego konta podczas bieżącej sesji.')
        return redirect(url_for('users'))
    con = get_db()
    user = db_execute(con, 'SELECT id,role FROM users WHERE id=?', (user_id,)).fetchone()
    if user:
        if user['role'] == 'admin':
            admins = db_execute(con, "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1").fetchone()['n']
            if admins <= 1:
                con.close()
                flash('Nie można usunąć ostatniego aktywnego administratora.')
                return redirect(url_for('users'))
        db_execute(con, 'DELETE FROM users WHERE id=?', (user_id,))
        con.commit()
        flash('Użytkownik został usunięty.')
    con.close()
    return redirect(url_for('users'))


@app.route('/')
@login_required
def index():
    con = get_db()
    apartments = db_execute(con, '''
        SELECT a.*, COUNT(DISTINCT s.id) AS section_count
        FROM apartments a LEFT JOIN sections s ON s.apartment_id=a.id AND s.active=1
        WHERE a.active=1 GROUP BY a.id ORDER BY a.name
    ''').fetchall()
    month = request.args.get('month') or datetime.now().strftime('%Y-%m')
    totals = db_execute(con, '''
        SELECT COALESCE(SUM(r.amount_due),0) total,
               COALESCE(SUM(CASE WHEN r.paid=1 THEN r.amount_due ELSE 0 END),0) paid
        FROM readings r WHERE r.period=?
    ''', (month,)).fetchone()
    con.close()
    return render_template('index.html', apartments=apartments, month=month, totals=totals)


@app.route('/apartments')
@login_required
def apartments():
    return redirect(url_for('index'))


@app.route('/apartment/new', methods=['GET','POST'])
@login_required
def new_apartment():
    if request.method == 'POST':
        con = get_db()
        db_execute(con, 'INSERT INTO apartments(name,address,owner,notes) VALUES (?,?,?,?)', (
            request.form.get('name','').strip(), request.form.get('address','').strip(),
            request.form.get('owner','').strip(), request.form.get('notes','').strip()))
        con.commit(); new_id = db_execute(con, 'SELECT MAX(id) AS id FROM apartments').fetchone()['id']; con.close()
        return redirect(url_for('apartment_detail', apartment_id=new_id))
    return render_template('apartment_form.html', apartment=None)


@app.route('/apartment/<int:apartment_id>/edit', methods=['GET','POST'])
@login_required
def edit_apartment(apartment_id):
    con = get_db(); apartment = get_apartment(con, apartment_id)
    if not apartment:
        con.close(); return 'Nie znaleziono mieszkania', 404
    if request.method == 'POST':
        db_execute(con, 'UPDATE apartments SET name=?, address=?, owner=?, notes=? WHERE id=?', (
            request.form.get('name','').strip(), request.form.get('address','').strip(),
            request.form.get('owner','').strip(), request.form.get('notes','').strip(), apartment_id))
        con.commit(); con.close()
        return redirect(url_for('apartment_detail', apartment_id=apartment_id))
    con.close()
    return render_template('apartment_form.html', apartment=apartment)


@app.post('/apartment/<int:apartment_id>/delete')
@login_required
def delete_apartment(apartment_id):
    con = get_db()
    apartment = get_apartment(con, apartment_id)
    if apartment:
        # Sekcje i odczyty zostaną usunięte przez ON DELETE CASCADE.
        db_execute(con, 'DELETE FROM apartments WHERE id=?', (apartment_id,))
        con.commit()
    con.close()
    return redirect(url_for('index'))


@app.route('/apartment/<int:apartment_id>')
@login_required
def apartment_detail(apartment_id):
    con = get_db(); apartment = get_apartment(con, apartment_id)
    if not apartment:
        con.close(); return 'Nie znaleziono mieszkania', 404
    month = request.args.get('month') or datetime.now().strftime('%Y-%m')
    sections = db_execute(con, '''
        SELECT s.*, r.id AS reading_id, r.period, r.reading, r.previous_reading,
               r.consumption, r.rate, r.amount_due, r.paid, r.paid_date, r.notes AS reading_notes
        FROM sections s LEFT JOIN readings r ON r.section_id=s.id AND r.period=?
        WHERE s.apartment_id=? AND s.active=1 ORDER BY s.name
    ''', (month, apartment_id)).fetchall()
    total = sum(float(x['amount_due'] or 0) for x in sections)
    paid = sum(float(x['amount_due'] or 0) for x in sections if x['paid'])
    con.close()
    return render_template('apartment.html', apartment=apartment, sections=sections, month=month, total=total, paid=paid)


@app.post('/apartment/<int:apartment_id>/section/new')
@login_required
def new_section(apartment_id):
    con = get_db()
    db_execute(con, 'INSERT INTO sections(apartment_id,name,unit,notes) VALUES (?,?,?,?)', (
        apartment_id, request.form.get('name','').strip(), request.form.get('unit','').strip(), request.form.get('notes','').strip()))
    con.commit(); con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id, month=request.form.get('month')))


@app.route('/section/<int:section_id>/edit', methods=['GET','POST'])
@login_required
def edit_section(section_id):
    con = get_db(); section = db_execute(con, 'SELECT * FROM sections WHERE id=?', (section_id,)).fetchone()
    if not section:
        con.close(); return 'Nie znaleziono sekcji', 404
    if request.method == 'POST':
        db_execute(con, 'UPDATE sections SET name=?, unit=?, notes=? WHERE id=?', (
            request.form.get('name','').strip(), request.form.get('unit','').strip(), request.form.get('notes','').strip(), section_id))
        con.commit(); con.close()
        return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=request.form.get('month')))
    con.close()
    return render_template('section_form.html', section=section)


@app.post('/section/<int:section_id>/delete')
@login_required
def delete_section(section_id):
    con = get_db(); section = db_execute(con, 'SELECT apartment_id FROM sections WHERE id=?', (section_id,)).fetchone()
    if section:
        db_execute(con, 'DELETE FROM sections WHERE id=?', (section_id,)); con.commit()
        apartment_id = section['apartment_id']
    else:
        apartment_id = None
    con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id)) if apartment_id else redirect(url_for('index'))


@app.route('/section/<int:section_id>/reading/new', methods=['GET','POST'])
@login_required
def new_reading(section_id):
    con = get_db(); section = db_execute(con, 'SELECT * FROM sections WHERE id=?', (section_id,)).fetchone()
    if not section:
        con.close(); return 'Nie znaleziono sekcji', 404
    if request.method == 'POST':
        f=request.form
        reading = float(f['reading']) if f.get('reading') else None
        previous = float(f['previous_reading']) if f.get('previous_reading') else None
        consumption = float(f['consumption']) if f.get('consumption') else (reading-previous if reading is not None and previous is not None else None)
        rate = float(f['rate']) if f.get('rate') else None
        amount = float(f.get('amount_due') or 0)
        try:
            db_execute(con, '''INSERT INTO readings(section_id,period,reading,previous_reading,consumption,rate,amount_due,paid,paid_date,notes)
                               VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                section_id, f['period'], reading, previous, consumption, rate, amount,
                1 if f.get('paid') == 'on' else 0, f.get('paid_date') or None, f.get('notes','')))
            con.commit()
        except Exception:
            con.rollback(); flash('Dla tej sekcji i tego miesiąca istnieje już odczyt. Możesz go edytować.')
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=f['period']))
    con.close()
    return render_template('reading_form.html', section=section, reading=None, month=request.args.get('month') or datetime.now().strftime('%Y-%m'))


@app.route('/reading/<int:reading_id>/edit', methods=['GET','POST'])
@login_required
def edit_reading(reading_id):
    con = get_db(); reading = db_execute(con, '''SELECT r.*, s.name AS section_name, s.unit, s.apartment_id
                                                FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''', (reading_id,)).fetchone()
    if not reading:
        con.close(); return 'Nie znaleziono odczytu', 404
    if request.method == 'POST':
        f=request.form
        current = float(f['reading']) if f.get('reading') else None
        previous = float(f['previous_reading']) if f.get('previous_reading') else None
        consumption = float(f['consumption']) if f.get('consumption') else (current-previous if current is not None and previous is not None else None)
        db_execute(con, '''UPDATE readings SET period=?, reading=?, previous_reading=?, consumption=?, rate=?, amount_due=?, paid=?, paid_date=?, notes=? WHERE id=?''', (
            f['period'], current, previous, consumption, float(f['rate']) if f.get('rate') else None,
            float(f.get('amount_due') or 0), 1 if f.get('paid')=='on' else 0, f.get('paid_date') or None, f.get('notes',''), reading_id))
        con.commit(); con.close()
        return redirect(url_for('apartment_detail', apartment_id=reading['apartment_id'], month=f['period']))
    con.close()
    return render_template('reading_form.html', section=reading, reading=reading, month=reading['period'])


@app.post('/reading/<int:reading_id>/paid')
@login_required
def toggle_paid(reading_id):
    con=get_db(); r=db_execute(con, 'SELECT * FROM readings WHERE id=?', (reading_id,)).fetchone()
    if r:
        paid = 0 if r['paid'] else 1
        paid_date = datetime.now().strftime('%Y-%m-%d') if paid else None
        db_execute(con, 'UPDATE readings SET paid=?, paid_date=? WHERE id=?', (paid, paid_date, reading_id)); con.commit()
        section=db_execute(con, 'SELECT apartment_id FROM sections WHERE id=?', (r['section_id'],)).fetchone()
        con.close(); return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=r['period']))
    con.close(); return redirect(url_for('index'))


@app.post('/reading/<int:reading_id>/delete')
@login_required
def delete_reading(reading_id):
    con=get_db(); r=db_execute(con, '''SELECT r.period,s.apartment_id FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''',(reading_id,)).fetchone()
    if r:
        db_execute(con,'DELETE FROM readings WHERE id=?',(reading_id,)); con.commit()
        apartment_id=r['apartment_id']; month=r['period']
    else: apartment_id=None; month=None
    con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id, month=month)) if apartment_id else redirect(url_for('index'))


@app.route('/reports')
@login_required
def reports():
    con=get_db(); year=request.args.get('year',datetime.now().strftime('%Y'))
    rows=db_execute(con, '''SELECT r.period, a.name AS apartment, s.name AS section, r.reading, r.previous_reading,
                            r.consumption, r.rate, r.amount_due, r.paid
                            FROM readings r JOIN sections s ON s.id=r.section_id JOIN apartments a ON a.id=s.apartment_id
                            WHERE r.period LIKE ? ORDER BY r.period DESC,a.name,s.name''',(year+'-%',)).fetchall()
    con.close(); return render_template('reports.html',rows=rows,year=year)


@app.route('/export.csv')
@login_required
def export_csv():
    con=get_db(); rows=db_execute(con, '''SELECT r.period,a.name AS apartment,s.name AS section,r.reading,r.previous_reading,
        r.consumption,r.rate,r.amount_due,r.paid,r.paid_date,r.notes
        FROM readings r JOIN sections s ON s.id=r.section_id JOIN apartments a ON a.id=s.apartment_id
        ORDER BY r.period DESC,a.name,s.name''').fetchall(); con.close()
    out=io.StringIO(); w=csv.writer(out,delimiter=';')
    w.writerow(['Okres','Mieszkanie','Sekcja','Odczyt','Poprzedni','Zużycie','Stawka','Kwota','Zapłacono','Data zapłaty','Notatki'])
    for r in rows: w.writerow(list(r))
    return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='oplaty.csv')


init_db()
ensure_admin_user()

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=5000,debug=False)
