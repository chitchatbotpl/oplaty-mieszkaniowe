from flask import Flask, render_template, request, redirect, url_for, send_file, flash, session, abort
import os, sqlite3, csv, io, json, urllib.request, urllib.error
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from datetime import datetime, date, timedelta
from calendar import monthrange
from zoneinfo import ZoneInfo

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
APP_TIMEZONE = ZoneInfo(os.environ.get('APP_TIMEZONE', 'Europe/Warsaw'))


def now_local():
    return datetime.now(APP_TIMEZONE)


def current_period():
    return now_local().strftime('%Y-%m')


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
            """CREATE TABLE IF NOT EXISTS apartments (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, address TEXT,
                owner TEXT, notes TEXT, active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS sections (
                id SERIAL PRIMARY KEY, apartment_id INTEGER NOT NULL
                REFERENCES apartments(id) ON DELETE CASCADE, name TEXT NOT NULL,
                unit TEXT DEFAULT '', notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS readings (
                id SERIAL PRIMARY KEY, section_id INTEGER NOT NULL
                REFERENCES sections(id) ON DELETE CASCADE, period TEXT NOT NULL,
                reading DOUBLE PRECISION, previous_reading DOUBLE PRECISION,
                consumption DOUBLE PRECISION, rate DOUBLE PRECISION,
                amount_due DOUBLE PRECISION NOT NULL DEFAULT 0,
                paid INTEGER NOT NULL DEFAULT 0, paid_date DATE,
                payment_due_date DATE, notes TEXT DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(section_id, period))""",
            """CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL
                REFERENCES users(id) ON DELETE CASCADE,
                apartment_id INTEGER REFERENCES apartments(id) ON DELETE CASCADE,
                section_id INTEGER REFERENCES sections(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, day_of_month INTEGER NOT NULL DEFAULT 1,
                time_hm TEXT NOT NULL DEFAULT '08:00',
                days_before INTEGER NOT NULL DEFAULT 0,
                schedule_mode TEXT NOT NULL DEFAULT 'fixed',
                reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL,
                payment_due_date DATE,
                active INTEGER NOT NULL DEFAULT 1, last_sent_key TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(kind IN ('reading','payment')),
                CHECK(day_of_month BETWEEN 1 AND 31),
                CHECK(days_before BETWEEN 0 AND 31))""",
            """CREATE TABLE IF NOT EXISTS reminder_logs (
                id SERIAL PRIMARY KEY, reminder_id INTEGER
                REFERENCES reminders(id) ON DELETE SET NULL,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                recipient_email TEXT NOT NULL, kind TEXT NOT NULL,
                period TEXT NOT NULL,
                sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent',
                details TEXT DEFAULT '')""",
            """CREATE INDEX IF NOT EXISTS idx_reminders_active
               ON reminders(active, day_of_month, time_hm)""",
            """CREATE INDEX IF NOT EXISTS idx_reminder_logs_period
               ON reminder_logs(period, user_id)"""
        ]
        for s in statements:
            con.execute(s)
    else:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS apartments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            address TEXT, owner TEXT, notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, apartment_id INTEGER NOT NULL,
            name TEXT NOT NULL, unit TEXT DEFAULT '', notes TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(apartment_id) REFERENCES apartments(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, section_id INTEGER NOT NULL,
            period TEXT NOT NULL, reading REAL, previous_reading REAL,
            consumption REAL, rate REAL, amount_due REAL NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0, paid_date TEXT, payment_due_date TEXT, notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(section_id, period),
            FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            apartment_id INTEGER, section_id INTEGER, kind TEXT NOT NULL,
            day_of_month INTEGER NOT NULL DEFAULT 1,
            time_hm TEXT NOT NULL DEFAULT '08:00',
            days_before INTEGER NOT NULL DEFAULT 0,
            schedule_mode TEXT NOT NULL DEFAULT 'fixed',
            reading_id INTEGER,
            payment_due_date TEXT,
            active INTEGER NOT NULL DEFAULT 1, last_sent_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(apartment_id) REFERENCES apartments(id) ON DELETE CASCADE,
            FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE,
            FOREIGN KEY(reading_id) REFERENCES readings(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS reminder_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, reminder_id INTEGER,
            user_id INTEGER, recipient_email TEXT NOT NULL,
            kind TEXT NOT NULL, period TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent',
            details TEXT DEFAULT '',
            FOREIGN KEY(reminder_id) REFERENCES reminders(id) ON DELETE SET NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL);
        CREATE INDEX IF NOT EXISTS idx_reminders_active
            ON reminders(active, day_of_month, time_hm);
        CREATE INDEX IF NOT EXISTS idx_reminder_logs_period
            ON reminder_logs(period, user_id);
        """)
    # Bezpieczne migracje: dodają nowe pola bez usuwania istniejących danych.
    if using_postgres():
        con.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS payment_due_date DATE")
        con.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS schedule_mode TEXT NOT NULL DEFAULT 'fixed'")
        con.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL")
        con.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS payment_due_date DATE")
    else:
        reading_columns = {row['name'] for row in con.execute("PRAGMA table_info(readings)").fetchall()}
        if 'payment_due_date' not in reading_columns:
            con.execute("ALTER TABLE readings ADD COLUMN payment_due_date TEXT")
        reminder_columns = {row['name'] for row in con.execute("PRAGMA table_info(reminders)").fetchall()}
        if 'schedule_mode' not in reminder_columns:
            con.execute("ALTER TABLE reminders ADD COLUMN schedule_mode TEXT NOT NULL DEFAULT 'fixed'")
        if 'reading_id' not in reminder_columns:
            con.execute("ALTER TABLE reminders ADD COLUMN reading_id INTEGER REFERENCES readings(id) ON DELETE SET NULL")
        if 'payment_due_date' not in reminder_columns:
            con.execute("ALTER TABLE reminders ADD COLUMN payment_due_date TEXT")


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
            ('Mieszkanie 03', 'ul. Przykładowa 1/3', '')
        ]:
            db_execute(con, 'INSERT INTO apartments(name,address,owner) VALUES (?,?,?)', row)
    con.commit()
    con.close()


@app.template_filter('money')
def money(v):
    return f'{float(v or 0):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', ' ')


def get_apartment(con, apartment_id):
    return db_execute(con, 'SELECT * FROM apartments WHERE id=?', (apartment_id,)).fetchone()


def send_brevo_email(recipient_email, subject, html_content, text_content=None):
    api_key = os.environ.get('BREVO_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Brak zmiennej BREVO_API_KEY w środowisku Render.')
    sender_email = os.environ.get('BREVO_SENDER_EMAIL', '').strip()
    sender_name = os.environ.get('BREVO_SENDER_NAME', 'OPŁATY').strip()
    if not sender_email:
        raise RuntimeError('Brak zmiennej BREVO_SENDER_EMAIL w środowisku Render.')
    payload = {'sender': {'name': sender_name, 'email': sender_email},
               'to': [{'email': recipient_email}], 'subject': subject,
               'htmlContent': html_content}
    if text_content:
        payload['textContent'] = text_content
    req = urllib.request.Request(
        'https://api.brevo.com/v3/smtp/email',
        data=json.dumps(payload).encode('utf-8'),
        headers={'accept': 'application/json', 'api-key': api_key,
                 'content-type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Brevo API zwróciło błąd HTTP {e.code}: {e.read().decode("utf-8", errors="replace")}')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Nie udało się połączyć z Brevo: {e.reason}')


@app.post('/test-email')
@admin_required
def test_email():
    recipient = session.get('email')
    if not recipient:
        flash('Nie znaleziono adresu e-mail zalogowanego użytkownika.')
        return redirect(url_for('index'))
    try:
        result = send_brevo_email(
            recipient, 'OPŁATY — test wiadomości e-mail',
            '<html><body style="font-family:Arial"><h2>OPŁATY</h2>'
            '<p>To jest testowa wiadomość wysłana przez aplikację.</p>'
            '<p>Połączenie OPŁATY → Brevo działa poprawnie.</p></body></html>',
            'OPŁATY — test wiadomości e-mail\n\nPołączenie OPŁATY → Brevo działa poprawnie.')
        flash(f'Testowy e-mail został wysłany. ID wiadomości: {result.get("messageId", "brak")}')
    except Exception as e:
        flash(f'Nie udało się wysłać wiadomości: {e}')
    return redirect(url_for('index'))


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
    month = request.args.get('month') or current_period()
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


@app.route('/apartment/new', methods=['GET', 'POST'])
@login_required
def new_apartment():
    if request.method == 'POST':
        con = get_db()
        db_execute(con, 'INSERT INTO apartments(name,address,owner,notes) VALUES (?,?,?,?)',
                   (request.form.get('name','').strip(), request.form.get('address','').strip(),
                    request.form.get('owner','').strip(), request.form.get('notes','').strip()))
        con.commit()
        if using_postgres():
            new_id = db_execute(con, 'SELECT id FROM apartments ORDER BY id DESC LIMIT 1').fetchone()['id']
        else:
            new_id = con.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=new_id))
    return render_template('apartment_form.html', apartment=None)


@app.route('/apartment/<int:apartment_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_apartment(apartment_id):
    con = get_db()
    apartment = get_apartment(con, apartment_id)
    if not apartment:
        con.close()
        return 'Nie znaleziono mieszkania', 404
    if request.method == 'POST':
        db_execute(con, 'UPDATE apartments SET name=?, address=?, owner=?, notes=? WHERE id=?',
                   (request.form.get('name','').strip(), request.form.get('address','').strip(),
                    request.form.get('owner','').strip(), request.form.get('notes','').strip(), apartment_id))
        con.commit()
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=apartment_id))
    con.close()
    return render_template('apartment_form.html', apartment=apartment)


@app.post('/apartment/<int:apartment_id>/delete')
@login_required
def delete_apartment(apartment_id):
    con = get_db()
    if get_apartment(con, apartment_id):
        db_execute(con, 'DELETE FROM apartments WHERE id=?', (apartment_id,))
        con.commit()
    con.close()
    return redirect(url_for('index'))


@app.route('/apartment/<int:apartment_id>')
@login_required
def apartment_detail(apartment_id):
    con = get_db()
    apartment = get_apartment(con, apartment_id)
    if not apartment:
        con.close()
        return 'Nie znaleziono mieszkania', 404
    month = request.args.get('month') or current_period()
    sections = db_execute(con, '''
        SELECT s.*, r.id AS reading_id, r.period, r.reading, r.previous_reading,
               r.consumption, r.rate, r.amount_due, r.paid, r.paid_date, r.payment_due_date,
               r.notes AS reading_notes
        FROM sections s LEFT JOIN readings r ON r.section_id=s.id AND r.period=?
        WHERE s.apartment_id=? AND s.active=1 ORDER BY s.name
    ''', (month, apartment_id)).fetchall()
    total = sum(float(x['amount_due'] or 0) for x in sections)
    paid = sum(float(x['amount_due'] or 0) for x in sections if x['paid'])
    con.close()
    return render_template('apartment.html', apartment=apartment, sections=sections,
                           month=month, total=total, paid=paid)


@app.post('/apartment/<int:apartment_id>/section/new')
@login_required
def new_section(apartment_id):
    con = get_db()
    db_execute(con, 'INSERT INTO sections(apartment_id,name,unit,notes) VALUES (?,?,?,?)',
               (apartment_id, request.form.get('name','').strip(),
                request.form.get('unit','').strip(), request.form.get('notes','').strip()))
    con.commit()
    con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id, month=request.form.get('month')))


@app.route('/section/<int:section_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_section(section_id):
    con = get_db()
    section = db_execute(con, 'SELECT * FROM sections WHERE id=?', (section_id,)).fetchone()
    if not section:
        con.close()
        return 'Nie znaleziono sekcji', 404
    if request.method == 'POST':
        db_execute(con, 'UPDATE sections SET name=?, unit=?, notes=? WHERE id=?',
                   (request.form.get('name','').strip(), request.form.get('unit','').strip(),
                    request.form.get('notes','').strip(), section_id))
        con.commit()
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=request.form.get('month')))
    con.close()
    return render_template('section_form.html', section=section)


@app.post('/section/<int:section_id>/delete')
@login_required
def delete_section(section_id):
    con = get_db()
    section = db_execute(con, 'SELECT apartment_id FROM sections WHERE id=?', (section_id,)).fetchone()
    if section:
        db_execute(con, 'DELETE FROM sections WHERE id=?', (section_id,))
        con.commit()
        apartment_id = section['apartment_id']
    else:
        apartment_id = None
    con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id)) if apartment_id else redirect(url_for('index'))


@app.route('/section/<int:section_id>/reading/new', methods=['GET', 'POST'])
@login_required
def new_reading(section_id):
    con = get_db()
    section = db_execute(con, 'SELECT * FROM sections WHERE id=?', (section_id,)).fetchone()
    if not section:
        con.close()
        return 'Nie znaleziono sekcji', 404
    if request.method == 'POST':
        f = request.form
        reading = float(f['reading']) if f.get('reading') else None
        previous = float(f['previous_reading']) if f.get('previous_reading') else None
        consumption = float(f['consumption']) if f.get('consumption') else (reading - previous if reading is not None and previous is not None else None)
        rate = float(f['rate']) if f.get('rate') else None
        amount = float(f.get('amount_due') or 0)
        try:
            db_execute(con, '''INSERT INTO readings(section_id,period,reading,previous_reading,consumption,rate,amount_due,paid,paid_date,payment_due_date,notes)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (section_id, f['period'], reading, previous, consumption, rate, amount,
                        1 if f.get('paid') == 'on' else 0, f.get('paid_date') or None, f.get('payment_due_date') or None, f.get('notes','')))
            con.commit()
        except Exception:
            con.rollback()
            flash('Dla tej sekcji i tego miesiąca istnieje już odczyt. Możesz go edytować.')
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=f['period']))
    con.close()
    return render_template('reading_form.html', section=section, reading=None, month=request.args.get('month') or current_period())


@app.route('/reading/<int:reading_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_reading(reading_id):
    con = get_db()
    reading = db_execute(con, '''SELECT r.*, s.name AS section_name, s.unit, s.apartment_id
                                 FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''', (reading_id,)).fetchone()
    if not reading:
        con.close()
        return 'Nie znaleziono odczytu', 404
    if request.method == 'POST':
        f = request.form
        current = float(f['reading']) if f.get('reading') else None
        previous = float(f['previous_reading']) if f.get('previous_reading') else None
        consumption = float(f['consumption']) if f.get('consumption') else (current - previous if current is not None and previous is not None else None)
        db_execute(con, '''UPDATE readings SET period=?, reading=?, previous_reading=?, consumption=?, rate=?, amount_due=?, paid=?, paid_date=?, payment_due_date=?, notes=? WHERE id=?''',
                   (f['period'], current, previous, consumption, float(f['rate']) if f.get('rate') else None,
                    float(f.get('amount_due') or 0), 1 if f.get('paid') == 'on' else 0,
                    f.get('paid_date') or None, f.get('payment_due_date') or None, f.get('notes',''), reading_id))
        con.commit()
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=reading['apartment_id'], month=f['period']))
    reading_reminders = db_execute(con, '''SELECT id, kind, day_of_month, time_hm, days_before,
            schedule_mode, payment_due_date, active
        FROM reminders WHERE user_id=? AND reading_id=?
        ORDER BY active DESC, time_hm''', (session['user_id'], reading_id)).fetchall()
    con.close()
    return render_template('reading_form.html', section=reading, reading=reading,
                           month=reading['period'], reading_reminders=reading_reminders)


@app.post('/reading/<int:reading_id>/paid')
@login_required
def toggle_paid(reading_id):
    con = get_db()
    r = db_execute(con, 'SELECT * FROM readings WHERE id=?', (reading_id,)).fetchone()
    if r:
        paid = 0 if r['paid'] else 1
        paid_date = now_local().strftime('%Y-%m-%d') if paid else None
        db_execute(con, 'UPDATE readings SET paid=?, paid_date=? WHERE id=?', (paid, paid_date, reading_id))
        con.commit()
        section = db_execute(con, 'SELECT apartment_id FROM sections WHERE id=?', (r['section_id'],)).fetchone()
        con.close()
        return redirect(url_for('apartment_detail', apartment_id=section['apartment_id'], month=r['period']))
    con.close()
    return redirect(url_for('index'))


@app.post('/reading/<int:reading_id>/delete')
@login_required
def delete_reading(reading_id):
    con = get_db()
    r = db_execute(con, '''SELECT r.period,s.apartment_id FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''', (reading_id,)).fetchone()
    if r:
        db_execute(con, 'DELETE FROM readings WHERE id=?', (reading_id,))
        con.commit()
        apartment_id, month = r['apartment_id'], r['period']
    else:
        apartment_id, month = None, None
    con.close()
    return redirect(url_for('apartment_detail', apartment_id=apartment_id, month=month)) if apartment_id else redirect(url_for('index'))


def get_report_data(year, apartment_id=None):
    con = get_db()
    apartments = db_execute(con, 'SELECT id,name FROM apartments WHERE active=1 ORDER BY name').fetchall()
    apartment_sql = ''
    apt_param = []
    if apartment_id:
        apartment_sql = ' AND a.id=?'
        apt_param = [int(apartment_id)]
    sections = db_execute(con, f'''SELECT s.id AS section_id,s.name AS section,s.unit,a.id AS apartment_id,a.name AS apartment
        FROM sections s JOIN apartments a ON a.id=s.apartment_id WHERE s.active=1 AND a.active=1 {apartment_sql}
        ORDER BY a.name,s.name''', tuple(apt_param)).fetchall()
    readings = db_execute(con, f'''SELECT r.id,r.period,r.section_id,r.reading,r.previous_reading,r.consumption,r.rate,r.amount_due,r.paid,r.paid_date,r.notes
        FROM readings r JOIN sections s ON s.id=r.section_id JOIN apartments a ON a.id=s.apartment_id
        WHERE r.period LIKE ? {apartment_sql}''', tuple([str(year) + '-%'] + apt_param)).fetchall()
    con.close()
    reading_map = {(r['section_id'], r['period']): r for r in readings}
    rows = []
    for m in range(1, 13):
        period = f'{int(year):04d}-{m:02d}'
        for sec in sections:
            r = reading_map.get((sec['section_id'], period))
            status = 'BRAK ODCZYTU / KWOTY' if r is None else ('OPŁACONE' if r['paid'] else 'NIEOPŁACONE')
            rows.append({'period': period, 'apartment': sec['apartment'], 'apartment_id': sec['apartment_id'],
                         'section': sec['section'], 'unit': sec['unit'] or '', 'reading_id': r['id'] if r else None,
                         'reading': r['reading'] if r else None, 'previous_reading': r['previous_reading'] if r else None,
                         'consumption': r['consumption'] if r else None, 'rate': r['rate'] if r else None,
                         'amount_due': float(r['amount_due'] or 0) if r else 0.0,
                         'paid': bool(r['paid']) if r else False, 'paid_date': r['paid_date'] if r else None,
                         'notes': r['notes'] if r else '', 'status': status})
    return rows, apartments


@app.route('/reports')
@login_required
def reports():
    year = request.args.get('year', now_local().strftime('%Y'))
    try: year = str(int(year))
    except ValueError: year = now_local().strftime('%Y')
    apartment_id = request.args.get('apartment_id', '').strip()
    apartment_id = int(apartment_id) if apartment_id.isdigit() else None
    rows, apartments = get_report_data(year, apartment_id)
    names = ['Styczeń','Luty','Marzec','Kwiecień','Maj','Czerwiec','Lipiec','Sierpień','Wrzesień','Październik','Listopad','Grudzień']
    months = []
    for i, name in enumerate(names, 1):
        mr = [r for r in rows if r['period'].endswith(f'-{i:02d}')]
        months.append({'period': f'{year}-{i:02d}', 'name': name, 'rows': mr,
                       'total': sum(r['amount_due'] for r in mr if r['reading_id']),
                       'unpaid': sum(r['amount_due'] for r in mr if r['reading_id'] and not r['paid']),
                       'missing': sum(1 for r in mr if not r['reading_id'])})
    return render_template('reports.html', months=months, apartments=apartments, year=year, apartment_id=apartment_id)


def report_query_params():
    year = request.args.get('year', now_local().strftime('%Y'))
    try: year = str(int(year))
    except ValueError: year = now_local().strftime('%Y')
    apartment_id = request.args.get('apartment_id', '').strip()
    return year, int(apartment_id) if apartment_id.isdigit() else None


@app.route('/export.csv')
@login_required
def export_csv():
    year, apartment_id = report_query_params()
    rows, _ = get_report_data(year, apartment_id)
    out = io.StringIO()
    w = csv.writer(out, delimiter=';')
    w.writerow(['Miesiąc','Mieszkanie','Sekcja','Jednostka','Odczyt','Poprzedni','Zużycie','Stawka','Kwota','Zapłacono','Data zapłaty','Status','Notatki'])
    for r in rows:
        w.writerow([r['period'],r['apartment'],r['section'],r['unit'],r['reading'] if r['reading'] is not None else '',
                    r['previous_reading'] if r['previous_reading'] is not None else '',r['consumption'] if r['consumption'] is not None else '',
                    r['rate'] if r['rate'] is not None else '',r['amount_due'] if r['reading_id'] else '',
                    'TAK' if r['paid'] else 'NIE',r['paid_date'] or '',r['status'],r['notes']])
    fn = f'oplaty_{year}' + (f'_mieszkanie_{apartment_id}' if apartment_id else '') + '.csv'
    return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')), mimetype='text/csv', as_attachment=True, download_name=fn)


@app.route('/export.pdf')
@login_required
def export_pdf():
    year, apartment_id = report_query_params()
    rows, apartments = get_report_data(year, apartment_id)
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        return 'Brak biblioteki reportlab.', 500
    reg = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    bold = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    rf, bf = 'Helvetica', 'Helvetica-Bold'
    if Path(reg).exists() and Path(bold).exists():
        pdfmetrics.registerFont(TTFont('AppSans', reg)); pdfmetrics.registerFont(TTFont('AppSansBold', bold)); rf, bf = 'AppSans', 'AppSansBold'
    apt_name = 'Wszystkie mieszkania'
    for a in apartments:
        if apartment_id and int(a['id']) == apartment_id: apt_name = a['name']
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), rightMargin=8*mm, leftMargin=8*mm, topMargin=10*mm, bottomMargin=10*mm, title=f'Raport OPŁAT {year}')
    styles = getSampleStyleSheet()
    title = ParagraphStyle('AppTitle', parent=styles['Title'], fontName=bf, fontSize=17, leading=20, alignment=TA_LEFT)
    small = ParagraphStyle('Small', parent=styles['BodyText'], fontName=rf, fontSize=7.5, leading=9)
    normal = ParagraphStyle('NormalApp', parent=styles['BodyText'], fontName=rf, fontSize=8, leading=10)
    story = [Paragraph(f'OPŁATY — raport za rok {year}', title), Paragraph(f'Filtr: {apt_name}', normal), Spacer(1,5*mm)]
    names = ['Styczeń','Luty','Marzec','Kwiecień','Maj','Czerwiec','Lipiec','Sierpień','Wrzesień','Październik','Listopad','Grudzień']
    for i, name in enumerate(names, 1):
        mr = [r for r in rows if r['period'].endswith(f'-{i:02d}')]
        if not mr: continue
        story.append(Paragraph(name.upper(), ParagraphStyle(f'M{i}', parent=normal, fontName=bf, fontSize=11, spaceBefore=4*mm, spaceAfter=2*mm)))
        data = [['Mieszkanie','Sekcja','Odczyt','Zużycie','Kwota','Status']]
        for r in mr:
            data.append([r['apartment'],r['section'],str(r['reading']) if r['reading'] is not None else '—',str(r['consumption']) if r['consumption'] is not None else '—',f"{r['amount_due']:.2f} zł" if r['reading_id'] else '—',r['status']])
        t = Table(data, repeatRows=1, colWidths=[42*mm,43*mm,25*mm,25*mm,27*mm,55*mm])
        t.setStyle(TableStyle([('FONTNAME',(0,0),(-1,-1),rf),('FONTNAME',(0,0),(-1,0),bf),('FONTSIZE',(0,0),(-1,-1),7.2),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e9e9e9')),('GRID',(0,0),(-1,-1),0.35,colors.HexColor('#bbbbbb')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f7f7f7')])]))
        story += [t, Paragraph(f"Suma: {sum(r['amount_due'] for r in mr if r['reading_id']):.2f} zł · Do zapłaty: {sum(r['amount_due'] for r in mr if r['reading_id'] and not r['paid']):.2f} zł · Brak wpisów: {sum(1 for r in mr if not r['reading_id'])}", small)]
    doc.build(story); buf.seek(0)
    fn = f'oplaty_{year}' + (f'_mieszkanie_{apartment_id}' if apartment_id else '') + '.pdf'
    return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name=fn)


# ============================== V12: PRZYPOMNIENIA ==============================

def get_reminder(con, reminder_id, user_id=None):
    sql = '''SELECT r.*, a.name AS apartment_name, s.name AS section_name, s.unit AS section_unit
             FROM reminders r LEFT JOIN apartments a ON a.id=r.apartment_id
             LEFT JOIN sections s ON s.id=r.section_id WHERE r.id=?'''
    params = [reminder_id]
    if user_id is not None:
        sql += ' AND r.user_id=?'; params.append(user_id)
    return db_execute(con, sql, tuple(params)).fetchone()


def parse_reminder_form(form):
    kind = form.get('kind', 'reading')
    if kind not in ('reading', 'payment'):
        kind = 'reading'
    a, s = form.get('apartment_id', '').strip(), form.get('section_id', '').strip()
    try:
        day = max(1, min(31, int(form.get('day_of_month', '1'))))
    except ValueError:
        day = 1
    tm = form.get('time_hm', '08:00').strip()
    try:
        datetime.strptime(tm, '%H:%M')
    except ValueError:
        tm = '08:00'
    try:
        before = max(0, min(31, int(form.get('days_before', '0'))))
    except ValueError:
        before = 0
    schedule_mode = form.get('schedule_mode', 'fixed')
    if schedule_mode not in ('fixed', 'due_date'):
        schedule_mode = 'fixed'
    reading_value = form.get('reading_id', '').strip()
    reading_id = int(reading_value) if reading_value.isdigit() else None
    payment_due_date = form.get('payment_due_date', '').strip() or None
    if kind != 'payment':
        schedule_mode, reading_id, payment_due_date = 'fixed', None, None
    return {
        'kind': kind, 'apartment_id': int(a) if a.isdigit() else None,
        'section_id': int(s) if s.isdigit() else None, 'day_of_month': day,
        'time_hm': tm, 'days_before': before, 'schedule_mode': schedule_mode,
        'reading_id': reading_id, 'payment_due_date': payment_due_date,
        'active': 1 if form.get('active') == 'on' else 0
    }


def apply_linked_reading_scope(con, values):
    if values['kind'] == 'payment' and values['schedule_mode'] == 'due_date' and values['reading_id']:
        row = db_execute(con, '''SELECT r.section_id,s.apartment_id
            FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''',
            (values['reading_id'],)).fetchone()
        if row:
            values['section_id'] = row['section_id']
            values['apartment_id'] = row['apartment_id']
    return values


def validate_reminder_target(con, values):
    apartment_id = values['apartment_id']
    section_id = values['section_id']
    if apartment_id and not db_execute(con, 'SELECT id FROM apartments WHERE id=? AND active=1', (apartment_id,)).fetchone():
        return False, 'Wybrany obszar opłat nie istnieje.'
    if section_id:
        sec = db_execute(con, 'SELECT id,apartment_id FROM sections WHERE id=? AND active=1', (section_id,)).fetchone()
        if not sec:
            return False, 'Wybrana pozycja opłat nie istnieje.'
        if apartment_id and int(sec['apartment_id']) != int(apartment_id):
            return False, 'Pozycja opłat nie należy do wybranego obszaru.'
    if values['kind'] == 'payment' and values['schedule_mode'] == 'due_date':
        if values['reading_id']:
            reading = db_execute(con, '''SELECT r.id, r.section_id, r.payment_due_date, s.apartment_id
                FROM readings r JOIN sections s ON s.id=r.section_id WHERE r.id=?''',
                (values['reading_id'],)).fetchone()
            if not reading:
                return False, 'Wybrany odczyt nie istnieje.'
            if not reading['payment_due_date']:
                return False, 'Wybrany odczyt nie ma ustawionego terminu płatności.'
            if section_id and int(section_id) != int(reading['section_id']):
                return False, 'Wybrany odczyt nie należy do wskazanej pozycji opłat.'
            if apartment_id and int(apartment_id) != int(reading['apartment_id']):
                return False, 'Wybrany odczyt nie należy do wskazanego obszaru opłat.'
        elif not values['payment_due_date']:
            return False, 'Wybierz odczyt z terminem płatności albo wpisz termin płatności bezpośrednio w przypomnieniu.'
        else:
            try:
                date.fromisoformat(values['payment_due_date'])
            except ValueError:
                return False, 'Termin płatności ma nieprawidłowy format.'
    return True, ''


@app.route('/reminders', methods=['GET','POST'])
@login_required
def reminders():
    con = get_db(); uid = session['user_id']
    if request.method == 'POST':
        v = apply_linked_reading_scope(con, parse_reminder_form(request.form))
        ok, msg = validate_reminder_target(con, v)
        if not ok:
            flash(msg)
        else:
            db_execute(con, '''INSERT INTO reminders(user_id,apartment_id,section_id,kind,day_of_month,time_hm,days_before,schedule_mode,reading_id,payment_due_date,active)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (uid,v['apartment_id'],v['section_id'],v['kind'],v['day_of_month'],v['time_hm'],v['days_before'],v['schedule_mode'],v['reading_id'],v['payment_due_date'],v['active']))
            con.commit(); flash('Przypomnienie zostało dodane.')
    rows = db_execute(con, '''SELECT r.*,a.name AS apartment_name,s.name AS section_name,s.unit AS section_unit
        FROM reminders r LEFT JOIN apartments a ON a.id=r.apartment_id LEFT JOIN sections s ON s.id=r.section_id
        WHERE r.user_id=? ORDER BY r.active DESC,r.kind,r.day_of_month,r.time_hm''', (uid,)).fetchall()
    apartments_rows = db_execute(con, 'SELECT id,name FROM apartments WHERE active=1 ORDER BY name').fetchall()
    sections_rows = db_execute(con, '''SELECT s.id,s.name,s.unit,s.apartment_id,a.name AS apartment_name
        FROM sections s JOIN apartments a ON a.id=s.apartment_id WHERE s.active=1 AND a.active=1 ORDER BY a.name,s.name''').fetchall()
    reading_options = db_execute(con, '''SELECT r.id,r.period,r.payment_due_date,r.amount_due,r.paid,
        s.id AS section_id,s.name AS section_name,s.apartment_id,a.name AS apartment_name
        FROM readings r JOIN sections s ON s.id=r.section_id JOIN apartments a ON a.id=s.apartment_id
        WHERE s.active=1 AND a.active=1 AND r.payment_due_date IS NOT NULL
        ORDER BY r.payment_due_date DESC,a.name,s.name,r.period DESC''').fetchall()
    logs = db_execute(con, '''SELECT l.*,a.name AS apartment_name,s.name AS section_name FROM reminder_logs l
        LEFT JOIN reminders r ON r.id=l.reminder_id LEFT JOIN apartments a ON a.id=r.apartment_id
        LEFT JOIN sections s ON s.id=r.section_id WHERE l.user_id=? ORDER BY l.sent_at DESC LIMIT 50''', (uid,)).fetchall()
    con.close()
    return render_template('reminders.html', reminders=rows, apartments=apartments_rows, sections=sections_rows, reading_options=reading_options, logs=logs)


@app.post('/reminders/<int:reminder_id>/edit')
@login_required
def edit_reminder(reminder_id):
    con = get_db(); uid = session['user_id']
    if not get_reminder(con, reminder_id, uid):
        con.close(); return 'Nie znaleziono przypomnienia', 404
    v = apply_linked_reading_scope(con, parse_reminder_form(request.form))
    ok, msg = validate_reminder_target(con, v)
    if not ok:
        con.close(); flash(msg); return redirect(url_for('reminders'))
    db_execute(con, '''UPDATE reminders SET apartment_id=?,section_id=?,kind=?,day_of_month=?,time_hm=?,days_before=?,schedule_mode=?,reading_id=?,payment_due_date=?,active=?
                       WHERE id=? AND user_id=?''', (v['apartment_id'],v['section_id'],v['kind'],v['day_of_month'],v['time_hm'],v['days_before'],v['schedule_mode'],v['reading_id'],v['payment_due_date'],v['active'],reminder_id,uid))
    con.commit(); con.close(); flash('Przypomnienie zostało zapisane.')
    return redirect(url_for('reminders'))


@app.post('/reminders/<int:reminder_id>/toggle')
@login_required
def toggle_reminder(reminder_id):
    con = get_db(); r = get_reminder(con, reminder_id, session['user_id'])
    if r:
        active = 0 if r['active'] else 1
        db_execute(con, 'UPDATE reminders SET active=? WHERE id=? AND user_id=?', (active,reminder_id,session['user_id']))
        con.commit(); flash('Przypomnienie zostało włączone.' if active else 'Przypomnienie zostało wyłączone.')
    con.close(); return redirect(url_for('reminders'))


@app.post('/reminders/<int:reminder_id>/delete')
@login_required
def delete_reminder(reminder_id):
    con = get_db(); db_execute(con, 'DELETE FROM reminders WHERE id=? AND user_id=?', (reminder_id,session['user_id']))
    con.commit(); con.close(); flash('Przypomnienie zostało usunięte.')
    return redirect(url_for('reminders'))


def reminder_due_now(r, now, payment_due_date=None):
    try:
        h, m = map(int, str(r['time_hm']).split(':'))
    except Exception:
        h, m = 8, 0
    if now < now.replace(hour=h, minute=m, second=0, microsecond=0):
        return False
    if r['kind'] == 'payment' and r['schedule_mode'] == 'due_date':
        if not payment_due_date:
            return False
        return now.date() == payment_due_date - timedelta(days=int(r['days_before'] or 0))
    last = monthrange(now.year, now.month)[1]
    due = date(now.year, now.month, min(int(r['day_of_month']), last))
    return now.date() == due - timedelta(days=int(r['days_before'] or 0))


def reminder_payment_due_date(con, reminder):
    if reminder['kind'] != 'payment' or reminder['schedule_mode'] != 'due_date':
        return None
    if reminder['reading_id']:
        row = db_execute(con, 'SELECT payment_due_date FROM readings WHERE id=?', (reminder['reading_id'],)).fetchone()
        value = row['payment_due_date'] if row else None
    else:
        value = reminder['payment_due_date']
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def reminder_sections(con, r):
    where, params = ['s.active=1','a.active=1'], []
    if r['section_id']:
        where.append('s.id=?'); params.append(r['section_id'])
    elif r['apartment_id']:
        where.append('s.apartment_id=?'); params.append(r['apartment_id'])
    return db_execute(con, f'''SELECT s.id,s.name,s.unit,s.apartment_id,a.name AS apartment_name
        FROM sections s JOIN apartments a ON a.id=s.apartment_id WHERE {' AND '.join(where)} ORDER BY a.name,s.name''', tuple(params)).fetchall()


def reminder_content(con, r, period):
    sections = reminder_sections(con, r)
    if not sections: return None
    ids = [s['id'] for s in sections]; ph = ','.join('?' for _ in ids)
    readings = db_execute(con, f'SELECT * FROM readings WHERE period=? AND section_id IN ({ph})', tuple([period] + ids)).fetchall()
    rm = {x['section_id']: x for x in readings}
    if r['kind'] == 'reading':
        missing = [s for s in sections if s['id'] not in rm]
        if not missing: return None
        subject = f'OPŁATY — przypomnienie o odczycie ({period})'
        items = ''.join(f"<li><strong>{s['apartment_name']}</strong> — {s['name']}" + (f" ({s['unit']})" if s['unit'] else '') + '</li>' for s in missing)
        html = f'''<html><body style="font-family:Arial;line-height:1.5"><h2>OPŁATY</h2>
        <p>Przypomnienie o wykonaniu odczytu za <strong>{period}</strong>.</p><ul>{items}</ul>
        <p>Wiadomość automatyczna z aplikacji OPŁATY.</p></body></html>'''
        text = 'OPŁATY — przypomnienie o odczycie\n\nMiesiąc: ' + period + '\n\n' + '\n'.join(f"- {s['apartment_name']} — {s['name']}" for s in missing)
        return subject, html, text
    unpaid = [s for s in sections if s['id'] in rm and not rm[s['id']]['paid']]
    if not unpaid: return None
    total = sum(float(rm[s['id']]['amount_due'] or 0) for s in unpaid)
    subject = f'OPŁATY — przypomnienie o płatności ({period})'
    items = ''.join(
        f"<li><strong>{s['apartment_name']}</strong> — {s['name']}: <strong>{float(rm[s['id']]['amount_due'] or 0):.2f} zł</strong>"
        + (f" — termin płatności: {rm[s['id']]['payment_due_date']}" if rm[s['id']]['payment_due_date'] else "")
        + "</li>" for s in unpaid
    )
    html = f'''<html><body style="font-family:Arial;line-height:1.5"><h2>OPŁATY</h2>
    <p>Przypomnienie o nieopłaconych należnościach za <strong>{period}</strong>.</p><ul>{items}</ul>
    <p><strong>Razem do zapłaty: {total:.2f} zł</strong></p><p>Wiadomość automatyczna z aplikacji OPŁATY.</p></body></html>'''
    text_items = []
    for s in unpaid:
        line = f"- {s['apartment_name']} — {s['name']}: {float(rm[s['id']]['amount_due'] or 0):.2f} zł"
        if rm[s['id']]['payment_due_date']:
            line += f" — termin płatności: {rm[s['id']]['payment_due_date']}"
        text_items.append(line)
    text = 'OPŁATY — przypomnienie o płatności\n\nMiesiąc: ' + period + '\n\n' + '\n'.join(text_items) + f'\n\nRazem do zapłaty: {total:.2f} zł'
    return subject, html, text


def run_reminders():
    now = now_local()
    current_month = now.strftime('%Y-%m')
    con = get_db()
    rows = db_execute(con, '''SELECT r.*,u.email AS user_email,u.active AS user_active FROM reminders r
        JOIN users u ON u.id=r.user_id WHERE r.active=1 AND u.active=1 ORDER BY r.id''').fetchall()
    sent = skipped = failed = 0
    for r in rows:
        due_date = reminder_payment_due_date(con, r)
        if not reminder_due_now(r, now, due_date):
            continue
        reminder_period = current_month
        if r['kind'] == 'payment' and r['schedule_mode'] == 'due_date':
            if r['reading_id']:
                reading_period = db_execute(con, 'SELECT period FROM readings WHERE id=?', (r['reading_id'],)).fetchone()
                if reading_period:
                    reminder_period = reading_period['period']
            elif due_date:
                reminder_period = due_date.strftime('%Y-%m')
        if r['schedule_mode'] == 'due_date' and r['kind'] == 'payment':
            key = f"due:{due_date.isoformat()}:{now.date().isoformat()}"
        else:
            key = f'{reminder_period}:{now.date().isoformat()}'
        if r['last_sent_key'] == key:
            skipped += 1
            continue
        try:
            content = reminder_content(con, r, reminder_period)
            if content is None:
                skipped += 1
                continue
            subject, html, text = content
            result = send_brevo_email(r['user_email'], subject, html, text)
            message_id = result.get('messageId', '') if isinstance(result, dict) else ''
            db_execute(con, '''INSERT INTO reminder_logs(reminder_id,user_id,recipient_email,kind,period,subject,status,details)
                VALUES (?,?,?,?,?,?,?,?)''', (r['id'],r['user_id'],r['user_email'],r['kind'],reminder_period,subject,'sent',message_id))
            db_execute(con, 'UPDATE reminders SET last_sent_key=? WHERE id=?', (key,r['id']))
            con.commit()
            sent += 1
        except Exception as exc:
            con.rollback()
            failed += 1
            try:
                db_execute(con, '''INSERT INTO reminder_logs(reminder_id,user_id,recipient_email,kind,period,subject,status,details)
                    VALUES (?,?,?,?,?,?,?,?)''', (r['id'],r['user_id'],r['user_email'],r['kind'],reminder_period,'Błąd wysyłki — '+r['kind'],'error',str(exc)))
                con.commit()
            except Exception:
                con.rollback()
    con.close()
    return {'checked':len(rows),'sent':sent,'skipped':skipped,'failed':failed}


@app.route('/reminders/run', methods=['GET','POST'])
def reminder_runner():
    token = os.environ.get('REMINDER_CRON_TOKEN','').strip()
    if not token: return 'Reminder runner is not configured.', 503
    supplied = request.headers.get('X-Reminder-Token','').strip() or request.args.get('token','').strip()
    if supplied != token: abort(404)
    result = run_reminders()
    return f"OK | checked={result['checked']} sent={result['sent']} skipped={result['skipped']} failed={result['failed']}", 200


@app.post('/reminders/<int:reminder_id>/test')
@login_required
def test_reminder(reminder_id):
    con = get_db()
    uid = session['user_id']
    r = get_reminder(con, reminder_id, uid)

    if not r:
        con.close()
        return 'Nie znaleziono przypomnienia', 404

    period = current_period()
    subject = '[TEST] OPŁATY — test przypomnienia'
    html = '''
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
      <h2>OPŁATY — test przypomnienia</h2>
      <p>To jest testowa wiadomość z aplikacji OPŁATY.</p>
      <p>Jeśli ją otrzymujesz, wysyłka przez Brevo działa poprawnie.</p>
    </div>
    '''
    text = (
        'OPŁATY — test przypomnienia\n\n'
        'To jest testowa wiadomość z aplikacji OPŁATY.\n'
        'Wysyłka przez Brevo działa poprawnie.'
    )

    try:
        result = send_brevo_email(session['email'], subject, html, text)
        message_id = result.get('messageId', '') if isinstance(result, dict) else ''

        db_execute(con, '''
            INSERT INTO reminder_logs
                (reminder_id, user_id, recipient_email, kind,
                 period, subject, status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            r['id'], uid, session['email'], r['kind'], period,
            subject, 'sent', f'Test e-maila. ID wiadomości: {message_id}'
        ))
        con.commit()
        flash('Testowy e-mail wysłany i zapisany w historii.')

    except Exception as exc:
        con.rollback()
        try:
            db_execute(con, '''
                INSERT INTO reminder_logs
                    (reminder_id, user_id, recipient_email, kind,
                     period, subject, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                r['id'], uid, session['email'], r['kind'], period,
                subject, 'error', str(exc)
            ))
            con.commit()
        except Exception:
            con.rollback()
        flash(f'Nie udało się wykonać testu: {exc}')
    finally:
        con.close()

    return redirect(url_for('reminders'))


init_db()
ensure_admin_user()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
