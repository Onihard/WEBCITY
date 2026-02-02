from flask import Flask, render_template, request, redirect, session, url_for, jsonify, flash
import hashlib
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo



app = Flask(__name__)
app.secret_key = "put_a_strong_secret_here"  # поменяй на что-то своё в продакшн

# --- Приватный доступ к сайту ---
SITE_ACCESS_PASSWORD = "АУРА142288"

@app.before_request
def require_site_password():
    allowed = ["site_password", "static"]
    if request.endpoint in allowed or request.endpoint is None:
        return
    if not session.get("site_access_granted"):
        return redirect(url_for("site_password"))

@app.route("/site_password", methods=["GET", "POST"])
def site_password():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == SITE_ACCESS_PASSWORD:
            session["site_access_granted"] = True
            return redirect(url_for("index"))
        else:
            flash("Неверный пароль для входа на сайт.", "danger")
    return render_template("site_password.html")

# Кастомный фильтр для Gravatar (md5 от ника)
def gravatar_hash(s):
    return hashlib.md5(s.strip().lower().encode('utf-8')).hexdigest()
app.jinja_env.filters['gravatar_hash'] = gravatar_hash

DB_PATH = "chat_bot.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_tables():
    """Создаёт недостающие таблицы (если их нет) — не затрагивая существующие."""
    conn = get_db_connection()
    cur = conn.cursor()
    # private_messages (если нет)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS private_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER,
        message_text TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # auth таблица для веб-логина
    cur.execute("""
    CREATE TABLE IF NOT EXISTS auth (
        auth_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        nickname TEXT UNIQUE,
        password_hash TEXT,
        role TEXT DEFAULT 'user'
    )
    """)
    # Если users таблицы нет (маловероятно, у тебя уже есть бот), создать минимальную структуру
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        nickname TEXT UNIQUE,
        age INTEGER,
        gender TEXT,
        bio TEXT,
        hobbies TEXT,
        city TEXT,
        motto TEXT,
        current_room TEXT,
        last_active DATETIME
    )
    """)
    # rooms таблица — если нет, создать и добавить пару дефолтных комнат
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rooms (
        room_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        description TEXT
    )
    """)
    # messages таблица
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        room_name TEXT,
        message_text TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # --- Миграции (безопасные ALTER'ы) ---
    try:
        cur.execute("ALTER TABLE auth ADD COLUMN role TEXT DEFAULT 'user'")
    except sqlite3.OperationalError:
        pass
    for col in ("hobbies TEXT", "city TEXT", "motto TEXT", "last_active DATETIME"):
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # --- Приватный доступ к сайту ---
    from datetime import datetime
    @app.before_request
    def update_last_active_and_require_password():
        # Приватный доступ
        allowed = ["site_password", "static"]
        if request.endpoint in allowed or request.endpoint is None:
            return
        if not session.get("site_access_granted"):
            return redirect(url_for("site_password"))
        # Автоочистка неактивных пользователей из комнат
        try:
            conn = get_db_connection()
            conn.execute("UPDATE users SET current_room = NULL WHERE current_room IS NOT NULL AND last_active IS NOT NULL AND (strftime('%s','now') - strftime('%s',last_active)) > 180")
            conn.commit()
            conn.close()
        except Exception:
            pass
        # Обновление last_active
        nick = session.get("nickname")
        if nick:
            try:
                conn = get_db_connection()
                conn.execute("UPDATE users SET last_active = ? WHERE nickname = ?", (datetime.utcnow(), nick))
                conn.commit()
                conn.close()
            except Exception:
                pass
    # Удаляем все комнаты кроме нужных и вставляем только нужные
    cur.execute("DELETE FROM rooms WHERE name NOT IN (?, ?)", ("ВАЖНОЕ", "Общение"))
    default_rooms = [
        ("ВАЖНОЕ", "Тут только для важных новостей"),
        ("Общение", "Флудим и общаемся :)")
    ]
    cur.executemany("INSERT OR IGNORE INTO rooms (name, description) VALUES (?, ?)", default_rooms)
    conn.commit()
    conn.close()

ensure_tables()

# Глобально подмешиваем список комнат для боковой панели
@app.context_processor
def inject_rooms_sidebar():
    try:
        conn = get_db_connection()
        rooms = conn.execute("SELECT name, description FROM rooms ORDER BY name").fetchall()
        out = []
        for r in rooms:
            cnt = conn.execute("SELECT COUNT(*) as c FROM users WHERE current_room = ?", (r['name'],)).fetchone()['c']
            out.append({'name': r['name'], 'description': r['description'], 'count': cnt})
        conn.close()
        return { 'rooms_sidebar': out }
    except Exception:
        return { 'rooms_sidebar': [] }

# ------------------------
# Утилиты для работы с пользователем (веб)
# ------------------------
def get_current_nickname():
    return session.get("nickname")

def get_current_user_row():
    nick = get_current_nickname()
    if not nick:
        return None
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE nickname = ?", (nick,)).fetchone()
    conn.close()
    return user

def is_admin_nick(nickname: str) -> bool:
    if not nickname:
        return False
    conn = get_db_connection()
    row = conn.execute("SELECT role FROM auth WHERE nickname = ?", (nickname,)).fetchone()
    conn.close()
    return bool(row and (row['role'] or '').lower() == 'admin')

# Делаем функцию доступной внутри шаблонов Jinja
app.jinja_env.globals['is_admin_nick'] = is_admin_nick

# ------------------------
# Главная: список комнат
# ------------------------
@app.route('/')
def index():
    if 'nickname' in session:
        logged = True
        nickname = session['nickname']
    else:
        logged = False
        nickname = None

    conn = get_db_connection()
    rooms = conn.execute("SELECT name, description FROM rooms").fetchall()
    # собираем count
    rooms_with_counts = []
    for r in rooms:
        cnt = conn.execute("SELECT COUNT(*) as c FROM users WHERE current_room = ?", (r['name'],)).fetchone()['c']
        rooms_with_counts.append({'name': r['name'], 'description': r['description'], 'count': cnt})
    conn.close()
    return render_template('index.html', rooms=rooms_with_counts, logged=logged, nickname=nickname)


# ------------------------
# Профиль: просмотр/редактирование своего и чужого профиля
# ------------------------
@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    conn = get_db_connection()
    if request.method == 'POST':
        age = request.form.get('age', type=int)
        gender = request.form.get('gender')
        bio = request.form.get('bio')
        hobbies = request.form.get('hobbies')
        city = request.form.get('city')
        motto = request.form.get('motto')
        conn.execute("UPDATE users SET age = ?, gender = ?, bio = ?, hobbies = ?, city = ?, motto = ? WHERE nickname = ?",
                     (age, gender, bio, hobbies, city, motto, nick))
        conn.commit()
        flash('Профиль обновлён')
        conn.close()
        return redirect(url_for('profile'))
    user = conn.execute("SELECT * FROM users WHERE nickname = ?", (nick,)).fetchone()
    conn.close()
    return render_template('profile.html', user=user, editable=True)



@app.route('/profile/<nickname>')
def profile_view(nickname):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE nickname = ?", (nickname,)).fetchone()
    conn.close()
    if not user:
        flash('Пользователь не найден')
        return redirect(url_for('index'))
    return render_template('profile.html', user=user, editable=False)

# ------------------------
# Join room (войти в комнату)
# ------------------------
@app.route('/join/<room_name>', methods=['POST'])
def join_room(room_name):
    if 'nickname' not in session:
        flash("Войдите, чтобы войти в комнату.")
        return redirect(url_for('login'))

    nick = session['nickname']
    now = datetime.utcnow()
    conn = get_db_connection()
    # Если в таблице users нет строки для этого ника, создадим (с user_id как max+1)
    u = conn.execute("SELECT * FROM users WHERE nickname = ?", (nick,)).fetchone()
    if not u:
        # генерируем user_id как max(user_id)+1 (или 100000 если пусто)
        maxid = conn.execute("SELECT COALESCE(MAX(user_id), 100000) as m FROM users").fetchone()['m']
        new_id = maxid + 1
        conn.execute("INSERT INTO users (user_id, nickname, age, gender, bio, current_room, last_active) VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
                     (new_id, nick, room_name, now))
    else:
        conn.execute("UPDATE users SET current_room = ?, last_active = ? WHERE nickname = ?", (room_name, now, nick))
    conn.commit()
    conn.close()
    return redirect(url_for('room', room_name=room_name))

# ------------------------
# Leave room (выйти)
# ------------------------
@app.route('/leave', methods=['POST'])
def leave_room():
    if 'nickname' not in session:
        return redirect(url_for('index'))
    nick = session['nickname']
    conn = get_db_connection()
    conn.execute("UPDATE users SET current_room = NULL WHERE nickname = ?", (nick,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

# ------------------------
# Страница комнаты
# ------------------------
@app.route('/room/<room_name>')
def room(room_name):
    conn = get_db_connection()
    messages = conn.execute(
        "SELECT users.nickname, messages.message_text, messages.timestamp "
        "FROM messages JOIN users ON messages.user_id = users.user_id "
        "WHERE room_name = ? ORDER BY timestamp ASC LIMIT 200",
        (room_name,)
    ).fetchall()
    conn.close()
    nickname = session.get('nickname')
    return render_template('room.html', room_name=room_name, messages=messages, nickname=nickname)

# ------------------------
# CRUD комнат
# ------------------------
@app.route('/rooms/new', methods=['GET', 'POST'])
def rooms_new():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        if not name:
            flash('Название комнаты обязательно')
            return redirect(url_for('rooms_new'))
        conn = get_db_connection()
        try:
            conn.execute("INSERT INTO rooms (name, description) VALUES (?, ?)", (name, description))
            conn.commit()
            flash('Комната создана')
            return redirect(url_for('room', room_name=name))
        except sqlite3.IntegrityError:
            flash('Комната с таким названием уже существует')
            return redirect(url_for('rooms_new'))
        finally:
            conn.close()
    return render_template('room_form.html', mode='create', room=None)

@app.route('/rooms/<room_name>/edit', methods=['GET', 'POST'])
def rooms_edit(room_name):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    if not is_admin_nick(session['nickname']):
        flash('Требуются права администратора')
        return redirect(url_for('room', room_name=room_name))
    conn = get_db_connection()
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        try:
            conn.execute("UPDATE rooms SET name = ?, description = ? WHERE name = ?", (name, description, room_name))
            conn.commit()
            flash('Комната обновлена')
            return redirect(url_for('room', room_name=name))
        finally:
            conn.close()
    room_row = conn.execute("SELECT * FROM rooms WHERE name = ?", (room_name,)).fetchone()
    conn.close()
    return render_template('room_form.html', mode='edit', room=room_row)

@app.route('/rooms/<room_name>/delete', methods=['POST'])
def rooms_delete(room_name):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    if not is_admin_nick(session['nickname']):
        flash('Требуются права администратора')
        return redirect(url_for('room', room_name=room_name))
    conn = get_db_connection()
    conn.execute("DELETE FROM rooms WHERE name = ?", (room_name,))
    conn.commit()
    conn.close()
    flash('Комната удалена')
    return redirect(url_for('index'))

# ------------------------
# Админ-панель: список комнат
# ------------------------
@app.route('/admin/rooms')
def admin_rooms():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    if not is_admin_nick(session['nickname']):
        flash('Требуются права администратора')
        return redirect(url_for('index'))
    conn = get_db_connection()
    rooms = conn.execute("SELECT * FROM rooms ORDER BY name").fetchall()
    conn.close()
    return render_template('admin_rooms.html', rooms=rooms)

# JSON endpoint для автообновления и подгрузки
@app.route('/get_messages/<room_name>')
def get_messages(room_name):
    after_id = request.args.get('after_id', type=int)
    before_id = request.args.get('before_id', type=int)
    limit = 100  # количество сообщений за раз

    conn = get_db_connection()
    query = """
        SELECT messages.message_id, users.nickname as nickname, messages.message_text as text, messages.timestamp as time
        FROM messages
        JOIN users ON messages.user_id = users.user_id
        WHERE room_name = ?
    """
    params = [room_name]

    if after_id:  # новые сообщения
        query += " AND messages.message_id > ?"
        params.append(after_id)
        query += " ORDER BY messages.message_id ASC"
    elif before_id:  # подгрузка старых
        query += " AND messages.message_id < ?"
        params.append(before_id)
        query += " ORDER BY messages.message_id DESC LIMIT ?"
        params.append(limit)
    else:
        # начальная загрузка последних сообщений
        query += " ORDER BY messages.message_id DESC LIMIT ?"
        params.append(limit)

    msgs = conn.execute(query, params).fetchall()
    conn.close()

    # если загружали старые, возвращаем в нормальном порядке (от старых к новым)
    if before_id or not after_id:
        msgs = list(reversed(msgs))

    # Приводим время к MSK и убираем миллисекунды (HH:MM:SS)
    out = []
    # Пытаемся использовать системную БД часовых поясов; при её отсутствии — фолбэк на фиксированный UTC+3
    try:
        msk = ZoneInfo("Europe/Moscow")
    except Exception:
        msk = timezone(timedelta(hours=3))
    for m in msgs:
        t = m['time']
        # SQLite может вернуть строку или datetime — нормализуем
        if isinstance(t, str):
            try:
                # Пытаемся распарсить ISO с микросекундами
                dt = datetime.fromisoformat(t)
            except ValueError:
                # fallback: без микросекунд
                try:
                    dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    dt = datetime.utcnow()
        else:
            dt = t
        if dt.tzinfo is None:
            # считаем что это UTC и конвертируем в MSK
            dt = dt.replace(tzinfo=timezone.utc)
        dt_msk = dt.astimezone(msk)
        time_str = dt_msk.strftime("%H:%M:%S")
        out.append({'id': m['message_id'], 'nickname': m['nickname'], 'text': m['text'], 'time': time_str})
    return jsonify(out)


# Список участников комнаты (JSON, с онлайн-статусом)
@app.route('/room_members/<room_name>')
def room_members(room_name):
    conn = get_db_connection()
    now = datetime.utcnow()
    rows = conn.execute("SELECT nickname, last_active FROM users WHERE current_room = ? ORDER BY nickname", (room_name,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        # онлайн, если был активен последние 2 минуты
        online = False
        try:
            if r['last_active']:
                dt = datetime.fromisoformat(r['last_active']) if isinstance(r['last_active'], str) else r['last_active']
                online = (now - dt).total_seconds() < 120
        except Exception:
            pass
        out.append({'nickname': r['nickname'], 'online': online})
    return jsonify(out)





# ------------------------
# Отправка сообщения в комнату
# ------------------------
@app.route('/send_message/<room_name>', methods=['POST'])
def send_message(room_name):
    if 'nickname' not in session:
        flash("Нужно войти в систему, чтобы отправлять сообщения.")
        return redirect(url_for('login'))

    text = request.form.get('message', '').strip()
    if not text:
        return redirect(url_for('room', room_name=room_name))

    nick = session['nickname']
    conn = get_db_connection()
    user = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    if not user:
        # створим юзера если вдруг
        maxid = conn.execute("SELECT COALESCE(MAX(user_id), 100000) as m FROM users").fetchone()['m']
        new_id = maxid + 1
        conn.execute("INSERT INTO users (user_id, nickname, age, gender, bio, current_room, last_active) VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
                     (new_id, nick, room_name, datetime.utcnow()))
        user_id = new_id
    else:
        user_id = user['user_id']

    conn.execute(
        "INSERT INTO messages (user_id, room_name, message_text, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, room_name, text, datetime.now())
    )
    # обновляем last_active
    conn.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (datetime.utcnow(), user_id))
    conn.commit()
    conn.close()
    return redirect(url_for('room', room_name=room_name))

# ------------------------
# Удаление сообщения (только своё)
# ------------------------
@app.route('/delete_message/<int:message_id>', methods=['POST'])
def delete_message(message_id):
    if 'nickname' not in session:
        return jsonify({'ok': False, 'error': 'auth'})
    nick = session['nickname']
    conn = get_db_connection()
    msg = conn.execute("SELECT * FROM messages JOIN users ON messages.user_id = users.user_id WHERE message_id = ?", (message_id,)).fetchone()
    if not msg or msg['nickname'] != nick:
        conn.close()
        return jsonify({'ok': False, 'error': 'not_owner'})
    conn.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ------------------------
# Личные сообщения — просмотр
# ------------------------
@app.route('/dm')
def dm_index():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    conn = get_db_connection()
    # список всех пользователей кроме текущего
    users = conn.execute("SELECT nickname FROM users WHERE nickname != ?", (nick,)).fetchall()
    conn.close()
    return render_template('dm_index.html', users=users, nickname=nick)

@app.route('/dm/<target_nick>')
def dm_view(target_nick):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    conn = get_db_connection()
    # Получаем id sender и receiver
    sender = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    receiver = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (target_nick,)).fetchone()
    # Если кого-то нет в users — показываем подсказку
    if not sender or not receiver:
        conn.close()
        flash("Один из пользователей не зарегистрирован в веб-интерфейсе (через бота/веб).")
        return redirect(url_for('dm_index'))

    sender_id = sender['user_id']
    receiver_id = receiver['user_id']

    msgs = conn.execute("""
        SELECT pm.*, us.nickname as sender_nick, ur.nickname as receiver_nick
        FROM private_messages pm
        JOIN users us ON pm.sender_id = us.user_id
        JOIN users ur ON pm.receiver_id = ur.user_id
        WHERE (pm.sender_id = ? AND pm.receiver_id = ?)
           OR (pm.sender_id = ? AND pm.receiver_id = ?)
        ORDER BY pm.timestamp DESC
        LIMIT 200
    """, (sender_id, receiver_id, receiver_id, sender_id)).fetchall()
    conn.close()
    return render_template('dm_view.html', messages=msgs, nick=nick, target=target_nick)

@app.route('/dm/send/<target_nick>', methods=['POST'])
def dm_send(target_nick):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    text = request.form.get('message', '').strip()
    if not text:
        return redirect(url_for('dm_view', target_nick=target_nick))
    nick = session['nickname']
    conn = get_db_connection()
    sender = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    receiver = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (target_nick,)).fetchone()
    if not sender or not receiver:
        conn.close()
        flash("Пользователь у кого-то нет записи в users.")
        return redirect(url_for('dm_index'))

    conn.execute(
        "INSERT INTO private_messages (sender_id, receiver_id, message_text, timestamp) VALUES (?, ?, ?, ?)",
        (sender['user_id'], receiver['user_id'], text, datetime.now())
    )
    conn.commit()
    conn.close()
    return redirect(url_for('dm_view', target_nick=target_nick))

# ------------------------
# Просмотр входящих личных сообщений (как в боте /mail)
# ------------------------
@app.route('/mail')
def mail():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    conn = get_db_connection()
    user = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    if not user:
        conn.close()
        flash("Ваша учётная запись не найдена.")
        return redirect(url_for('index'))
    user_id = user['user_id']
    msgs = conn.execute("""
        SELECT pm.message_text, pm.timestamp, us.nickname as sender
        FROM private_messages pm
        JOIN users us ON pm.sender_id = us.user_id
        WHERE pm.receiver_id = ?
        ORDER BY pm.timestamp DESC
    """, (user_id,)).fetchall()
    conn.close()
    return render_template('mail.html', messages=msgs)

# ------------------------
# Регистрация и логин
# ------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nickname = request.form.get('nickname').strip()
        password = request.form.get('password')
        if not nickname or not password:
            flash("Нужно указать ник и пароль.")
            return redirect(url_for('register'))

        conn = get_db_connection()
        # проверяем, есть ли ник в auth или users
        exists = conn.execute("SELECT * FROM auth WHERE nickname = ?", (nickname,)).fetchone()
        if exists:
            conn.close()
            flash("Такой ник уже занят.")
            return redirect(url_for('register'))

        # Если в users нет строки, создадим её с новым user_id
        user = conn.execute("SELECT * FROM users WHERE nickname = ?", (nickname,)).fetchone()
        if not user:
            maxid = conn.execute("SELECT COALESCE(MAX(user_id), 100000) as m FROM users").fetchone()['m']
            new_id = maxid + 1
            conn.execute("INSERT INTO users (user_id, nickname, age, gender, bio, current_room) VALUES (?, ?, NULL, NULL, NULL, NULL)",
                         (new_id, nickname))
            user_id = new_id
        else:
            user_id = user['user_id']

        pw_hash = generate_password_hash(password)
        conn.execute("INSERT INTO auth (user_id, nickname, password_hash) VALUES (?, ?, ?)",
                     (user_id, nickname, pw_hash))
        conn.commit()
        conn.close()

        session['nickname'] = nickname
        flash("Регистрация прошла успешно!")
        return redirect(url_for('index'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        nickname = request.form.get('nickname').strip()
        password = request.form.get('password')
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM auth WHERE nickname = ?", (nickname,)).fetchone()
        conn.close()
        if row and check_password_hash(row['password_hash'], password):
            session['nickname'] = nickname
            flash("Вход выполнен.")
            return redirect(url_for('index'))
        else:
            flash("Неверный ник или пароль.")
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('nickname', None)
    flash("Вышли из системы.")
    return redirect(url_for('index'))

# ------------------------
# Запуск
# ------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)
