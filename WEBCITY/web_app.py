from flask import Flask, render_template, request, redirect, session, url_for, jsonify, flash
import hashlib
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask_socketio import SocketIO, join_room, leave_room as socketio_leave_room
import os
import time
import uuid

# Uploads
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
ALLOWED_IMAGE_EXT = {'png','jpg','jpeg','gif','webp'}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins='*')
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

# --- Утилиты ---

def leave_current_room_for(nickname):
    """Снимаем current_room у пользователя — вызывается при переходе на другие разделы."""
    if not nickname:
        return
    conn = get_db_connection()
    conn.execute("UPDATE users SET current_room = NULL WHERE nickname = ?", (nickname,))
    conn.commit()
    conn.close()


def format_to_msk(ts):
    """Возвращает строку времени/даты в часовом поясе MSK без микросекунд."""
    try:
        msk = ZoneInfo("Europe/Moscow")
    except Exception:
        msk = timezone(timedelta(hours=3))
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except Exception:
                dt = datetime.utcnow()
    else:
        dt = ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(msk).strftime("%Y-%m-%d %H:%M:%S")


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
    # add image/avatar columns if missing
    try:
        cur.execute("ALTER TABLE messages ADD COLUMN image_path TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE private_messages ADD COLUMN image_path TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT")
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
        # session avatar for navbar
        session_avatar = None
        nick = session.get('nickname')
        if nick:
            try:
                av = conn.execute("SELECT avatar_path FROM users WHERE nickname = ?", (nick,)).fetchone()
                if av and av['avatar_path']:
                    session_avatar = url_for('static', filename=av['avatar_path']) + '?v=' + str(int(time.time()))
            except Exception:
                session_avatar = None
        conn.close()
        return { 'rooms_sidebar': out, 'session_avatar': session_avatar }
    except Exception:
        return { 'rooms_sidebar': [], 'session_avatar': None }

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

    # Онлайн: пользователи с last_active в пределах последних 5 минут
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    online_count = conn.execute("SELECT COUNT(*) as c FROM users WHERE last_active >= ?", (five_minutes_ago,)).fetchone()['c']

    conn.close()
    return render_template('index.html', rooms=rooms_with_counts, logged=logged, nickname=nickname, online_count=online_count)


# ------------------------
# Профиль: просмотр/редактирование своего и чужого профиля
# ------------------------
@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    # При переходе на профиль считаем, что пользователь покидает комнату
    leave_current_room_for(nick)
    conn = get_db_connection()
    if request.method == 'POST':
        age = request.form.get('age', type=int)
        gender = request.form.get('gender')
        bio = request.form.get('bio')
        hobbies = request.form.get('hobbies')
        city = request.form.get('city')
        motto = request.form.get('motto')
        avatar_path = None
        f = request.files.get('avatar')
        if f and f.filename:
            filename = secure_filename(f.filename)
            ext = filename.rsplit('.', 1)[-1].lower()
            if ext in ALLOWED_IMAGE_EXT:
                data = f.read()
                if len(data) <= MAX_IMAGE_BYTES:
                    new_name = f"{uuid.uuid4().hex}.{ext}"
                    path = os.path.join(UPLOAD_DIR, new_name)
                    with open(path, 'wb') as fh:
                        fh.write(data)
                    avatar_path = 'uploads/' + new_name
        if avatar_path:
            conn.execute("UPDATE users SET age = ?, gender = ?, bio = ?, hobbies = ?, city = ?, motto = ?, avatar_path = ? WHERE nickname = ?",
                         (age, gender, bio, hobbies, city, motto, avatar_path, nick))
        else:
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
    # Просмотр профиля другого пользователя — не покидаем комнату
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
# Страница комнаты сукааааа сукааааааааааааааааа
#мы срем в эту комнату сука, сука, сука , сука, сука. Мы любим срать в эту комнату сука!!!!!
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
    limit = 200  # количество сообщений за раз (увеличено)

    conn = get_db_connection()
    query = """
        SELECT messages.message_id, users.nickname as nickname, messages.message_text as text, messages.timestamp as time, users.avatar_path as avatar, messages.image_path as image
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
        # avatar: prefer uploaded avatar_path, else gravatar
        avatar_path = m['avatar']
        if avatar_path:
            try:
                avatar_url = url_for('static', filename=avatar_path)
            except Exception:
                avatar_url = ''
        else:
            avatar_url = 'https://www.gravatar.com/avatar/' + gravatar_hash(m['nickname']) + '?d=identicon'
        out.append({'id': m['message_id'], 'nickname': m['nickname'], 'text': m['text'], 'time': time_str, 'avatar': avatar_url, 'image': m['image']})
    return jsonify(out)


# Список участников комнаты (JSON, с онлайн-статусом)
@app.route('/room_members/<room_name>')
def room_members(room_name):
    # Прежде чем отдавать список участников — очищаем неактивных (автоочистка)
    now = datetime.utcnow()
    conn = get_db_connection()
    # Автоочистка: снимаем room для тех, кто не активен более 10 минут
    conn.execute("UPDATE users SET current_room = NULL WHERE last_active IS NOT NULL AND (strftime('%s', 'now') - strftime('%s', last_active)) > ?", (600,))
    conn.commit()

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


@app.route('/online_count')
def online_count():
    conn = get_db_connection()
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    c = conn.execute("SELECT COUNT(*) as c FROM users WHERE last_active >= ?", (five_minutes_ago,)).fetchone()['c']
    conn.close()
    return jsonify({'online': c})





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
    # Максимальная длина сообщения
    if len(text) > 3000:
        flash('Сообщение слишком длинное (макс 3000 символов).', 'warning')
        return redirect(url_for('room', room_name=room_name))

    nick = session['nickname']
    conn = get_db_connection()
    user = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    if not user:
        # создадим юзера если вдруг
        maxid = conn.execute("SELECT COALESCE(MAX(user_id), 100000) as m FROM users").fetchone()['m']
        new_id = maxid + 1
        conn.execute("INSERT INTO users (user_id, nickname, age, gender, bio, current_room, last_active) VALUES (?, ?, NULL, NULL, NULL, ?, ?)",
                     (new_id, nick, room_name, datetime.utcnow()))
        user_id = new_id
    else:
        user_id = user['user_id']

    # rate limit: не чаще 1 сообщения в 2 секунды
    try:
        last = conn.execute("SELECT timestamp FROM messages WHERE user_id = ? AND room_name = ? ORDER BY message_id DESC LIMIT 1", (user_id, room_name)).fetchone()
        if last and last['timestamp']:
            last_ts = last['timestamp'] if isinstance(last['timestamp'], datetime) else datetime.fromisoformat(last['timestamp'])
            # Normalize to UTC for reliable comparison. If timestamp is naive, assume MSK (server's previous behavior) and convert to UTC.
            try:
                if last_ts.tzinfo is None:
                    try:
                        msk = ZoneInfo("Europe/Moscow")
                    except Exception:
                        msk = timezone(timedelta(hours=3))
                    last_ts = last_ts.replace(tzinfo=msk).astimezone(timezone.utc)
                else:
                    last_ts = last_ts.astimezone(timezone.utc)
            except Exception:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
            if (now_utc - last_ts).total_seconds() < 2:
                flash('Подождите немного перед отправкой следующего сообщения.', 'warning')
                conn.close()
                return redirect(url_for('room', room_name=room_name))
    except Exception:
        # при ошибке парсинга просто продолжаем
        pass

    now = datetime.utcnow()
    image_path = None
    f = request.files.get('image')
    if f and f.filename:
        filename = secure_filename(f.filename)
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext in ALLOWED_IMAGE_EXT:
            data = f.read()
            if len(data) <= MAX_IMAGE_BYTES:
                new_name = f"{uuid.uuid4().hex}.{ext}"
                path = os.path.join(UPLOAD_DIR, new_name)
                with open(path, 'wb') as fh:
                    fh.write(data)
                # store as URL-safe path (forward slashes) for static serving
                image_path = 'uploads/' + new_name

    # Server-side dedupe: if last message by this user in this room has identical text and image within 1 second, treat as duplicate
    try:
        last_msg = conn.execute("SELECT message_id, message_text, image_path, timestamp FROM messages WHERE user_id = ? AND room_name = ? ORDER BY message_id DESC LIMIT 1", (user_id, room_name)).fetchone()
        if last_msg:
            last_ts = last_msg['timestamp'] if isinstance(last_msg['timestamp'], datetime) else datetime.fromisoformat(last_msg['timestamp'])
            try:
                if last_ts.tzinfo is None:
                    try:
                        msk = ZoneInfo("Europe/Moscow")
                    except Exception:
                        msk = timezone(timedelta(hours=3))
                    last_ts = last_ts.replace(tzinfo=msk).astimezone(timezone.utc)
                else:
                    last_ts = last_ts.astimezone(timezone.utc)
            except Exception:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            now_utc = now.replace(tzinfo=timezone.utc)
            same_text = (last_msg['message_text'] == text)
            same_image = ((last_msg['image_path'] is None and image_path is None) or (last_msg['image_path'] == image_path))
            if same_text and same_image and (now_utc - last_ts).total_seconds() < 1:
                # Duplicate detected — return existing message id/time for AJAX callers and avoid inserting a new row.
                msg_id = last_msg['message_id']
                # update last_active
                conn.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (now, user_id))
                conn.commit()
                avatar = conn.execute("SELECT avatar_path FROM users WHERE user_id = ?", (user_id,)).fetchone()
                avatar_url = url_for('static', filename=avatar['avatar_path']) if avatar and avatar['avatar_path'] else 'https://www.gravatar.com/avatar/' + gravatar_hash(nick) + '?d=identicon'
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'id': msg_id, 'time': format_to_msk(last_msg['timestamp']), 'avatar': avatar_url, 'image': image_path})
    except Exception:
        # dedupe failed — fall through to normal insert
        pass

    cur = conn.execute(
        "INSERT INTO messages (user_id, room_name, message_text, timestamp, image_path) VALUES (?, ?, ?, ?, ?)",
        (user_id, room_name, text, now, image_path)
    )
    msg_id = cur.lastrowid
    # обновляем last_active
    conn.execute("UPDATE users SET last_active = ? WHERE user_id = ?", (now, user_id))
    conn.commit()

    # Emit через SocketIO, чтобы клиенты получали новое сообщение мгновенно (включая id и image)
    try:
        # avatar для отправителя
        avatar = conn.execute("SELECT avatar_path FROM users WHERE user_id = ?", (user_id,)).fetchone()
        avatar_url = url_for('static', filename=avatar['avatar_path']) if avatar and avatar['avatar_path'] else 'https://www.gravatar.com/avatar/' + gravatar_hash(nick) + '?d=identicon'
        payload = {'id': msg_id, 'nickname': nick, 'text': text, 'time': format_to_msk(now), 'avatar': avatar_url, 'image': image_path}
        socketio.emit('new_message', payload, room=room_name)
    except Exception:
        pass

    conn.close()
    # Если запрос был AJAX (fetch), вернём JSON с id/time
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'id': msg_id, 'time': format_to_msk(now), 'avatar': payload.get('avatar'), 'image': image_path})
    return redirect(url_for('room', room_name=room_name))

# ------------------------
# Удаление сообщения (только своё)
# ------------------------
# Убрано: удаление сообщений (функция удалена по требованию).
# Раньше тут был endpoint для удаления сообщений - удалён.

# ------------------------
# Личные сообщения — просмотр
# ------------------------
@app.route('/dm')
def dm_index():
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    # пользователь покидает комнату при переходе в DM
    leave_current_room_for(nick)
    conn = get_db_connection()
    # список всех пользователей кроме текущего
    users = conn.execute("SELECT nickname FROM users WHERE nickname != ?", (nick,)).fetchall()
    conn.close()
    return render_template('dm_index.html', users=users, nickname=nick)

@app.route('/get_dm_messages/<target_nick>')
def get_dm_messages(target_nick):
    if 'nickname' not in session:
        return jsonify([])
    nick = session['nickname']
    # Получаем id sender и receiver
    conn = get_db_connection()
    sender = conn.execute("SELECT user_id, avatar_path FROM users WHERE nickname = ?", (nick,)).fetchone()
    receiver = conn.execute("SELECT user_id, avatar_path FROM users WHERE nickname = ?", (target_nick,)).fetchone()
    if not sender or not receiver:
        conn.close()
        return jsonify([])
    sender_id = sender['user_id']
    receiver_id = receiver['user_id']
    after_id = request.args.get('after_id', type=int)
    before_id = request.args.get('before_id', type=int)
    limit = 200
    query = """
        SELECT pm.id, pm.sender_id, pm.receiver_id, pm.message_text, pm.timestamp, us.nickname as sender_nick, us.avatar_path as avatar, pm.image_path as image
        FROM private_messages pm
        JOIN users us ON pm.sender_id = us.user_id
        WHERE (pm.sender_id = ? AND pm.receiver_id = ?)
           OR (pm.sender_id = ? AND pm.receiver_id = ?)
    """
    params = [sender_id, receiver_id, receiver_id, sender_id]
    if after_id:
        query += ' AND pm.id > ? ORDER BY pm.id ASC'
        params.append(after_id)
    elif before_id:
        query += ' AND pm.id < ? ORDER BY pm.id DESC LIMIT ?'
        params.append(before_id)
        params.append(limit)
    else:
        query += ' ORDER BY pm.id DESC LIMIT ?'
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    # если загружали старые, вернём в нормальном порядке
    if before_id or not after_id:
        rows = list(reversed(rows))
    out = []
    for r in rows:
        t = r['timestamp']
        if isinstance(t, str):
            try:
                dt = datetime.fromisoformat(t)
            except Exception:
                try:
                    dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    dt = datetime.utcnow()
        else:
            dt = t
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            msk = ZoneInfo("Europe/Moscow")
        except Exception:
            msk = timezone(timedelta(hours=3))
        time_str = dt.astimezone(msk).strftime("%H:%M:%S")
        avatar_path = r['avatar']
        avatar_url = url_for('static', filename=avatar_path) if avatar_path else 'https://www.gravatar.com/avatar/' + gravatar_hash(r['sender_nick']) + '?d=identicon'
        out.append({'id': r['id'], 'sender_nick': r['sender_nick'], 'message_text': r['message_text'], 'timestamp': time_str, 'avatar': avatar_url, 'image': r['image']})
    return jsonify(out)


@app.route('/dm/<target_nick>')
def dm_view(target_nick):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    nick = session['nickname']
    # При переходе на DM пользователь покидает комнату
    leave_current_room_for(nick)
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

    raw_msgs = conn.execute("""
        SELECT pm.id, pm.message_text, pm.timestamp, pm.image_path as image, us.nickname as sender_nick, us.avatar_path as avatar
        FROM private_messages pm
        JOIN users us ON pm.sender_id = us.user_id
        JOIN users ur ON pm.receiver_id = ur.user_id
        WHERE (pm.sender_id = ? AND pm.receiver_id = ?)
           OR (pm.sender_id = ? AND pm.receiver_id = ?)
        ORDER BY pm.id ASC
        LIMIT 200
    """, (sender_id, receiver_id, receiver_id, sender_id)).fetchall()
    conn.close()
    # форматируем время для показа (MSK, без микросекунд)
    msgs = []
    for m in raw_msgs:
        avatar_url = url_for('static', filename=m['avatar']) if m['avatar'] else 'https://www.gravatar.com/avatar/' + gravatar_hash(m['sender_nick']) + '?d=identicon'
        msgs.append({
            'id': m['id'],
            'message_text': m['message_text'],
            'timestamp': format_to_msk(m['timestamp']),
            'sender_nick': m['sender_nick'],
            'avatar': avatar_url,
            'image': m['image']
        })
    return render_template('dm_view.html', messages=msgs, nick=nick, target=target_nick)

@app.route('/dm/send/<target_nick>', methods=['POST'])
def dm_send(target_nick):
    if 'nickname' not in session:
        return redirect(url_for('login'))
    text = request.form.get('message', '').strip()
    if not text:
        return redirect(url_for('dm_view', target_nick=target_nick))
    # Максимальная длина сообщения
    if len(text) > 3000:
        flash('Сообщение слишком длинное (макс 3000 символов).', 'warning')
        return redirect(url_for('dm_view', target_nick=target_nick))
    nick = session['nickname']
    conn = get_db_connection()
    sender = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (nick,)).fetchone()
    receiver = conn.execute("SELECT user_id FROM users WHERE nickname = ?", (target_nick,)).fetchone()
    if not sender or not receiver:
        conn.close()
        flash("Пользователь у кого-то нет записи в users.")
        return redirect(url_for('dm_index'))

    image_path = None
    f = request.files.get('image')
    if f and f.filename:
        filename = secure_filename(f.filename)
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext in ALLOWED_IMAGE_EXT:
            data = f.read()
            if len(data) <= MAX_IMAGE_BYTES:
                new_name = f"{uuid.uuid4().hex}.{ext}"
                path = os.path.join(UPLOAD_DIR, new_name)
                with open(path, 'wb') as fh:
                    fh.write(data)
                # store as URL-safe path (forward slashes) for static serving
                image_path = 'uploads/' + new_name
        # if image invalid — ignore silently

    # rate limit for DM (1 msg / 2s)
    try:
        last = conn.execute("SELECT timestamp FROM private_messages WHERE sender_id = ? ORDER BY id DESC LIMIT 1", (sender['user_id'],)).fetchone()
        if last and last['timestamp']:
            last_ts = last['timestamp'] if isinstance(last['timestamp'], datetime) else datetime.fromisoformat(last['timestamp'])
            # Normalize to UTC for reliable comparison.
            try:
                if last_ts.tzinfo is None:
                    try:
                        msk = ZoneInfo("Europe/Moscow")
                    except Exception:
                        msk = timezone(timedelta(hours=3))
                    last_ts = last_ts.replace(tzinfo=msk).astimezone(timezone.utc)
                else:
                    last_ts = last_ts.astimezone(timezone.utc)
            except Exception:
                # if anything fails, fall back to naive replace to avoid blocking
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
            if (now_utc - last_ts).total_seconds() < 2:
                conn.close()
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({'error': 'Rate limit: wait before sending another message.'}), 429
                flash('Подождите немного перед отправкой следующего сообщения.', 'warning')
                return redirect(url_for('dm_view', target_nick=target_nick))
    except Exception:
        pass

    now = datetime.utcnow()
    cur = conn.execute(
        "INSERT INTO private_messages (sender_id, receiver_id, message_text, timestamp, image_path) VALUES (?, ?, ?, ?, ?)",
        (sender['user_id'], receiver['user_id'], text, now, image_path)
    )
    msg_id = cur.lastrowid
    conn.commit()
    # get avatar url
    avatar = conn.execute("SELECT avatar_path FROM users WHERE user_id = ?", (sender['user_id'],)).fetchone()
    avatar_url = url_for('static', filename=avatar['avatar_path']) if avatar and avatar['avatar_path'] else 'https://www.gravatar.com/avatar/' + gravatar_hash(nick) + '?d=identicon'
    conn.close()
    # If AJAX, return JSON with msg id/time
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'id': msg_id, 'time': format_to_msk(now), 'avatar': avatar_url, 'image': image_path})
    return redirect(url_for('dm_view', target_nick=target_nick))

# Mail functionality removed — mailbox UI was removed to focus on DM. If needed, incoming private messages can still be viewed via DM interface.
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
    nick = session.get('nickname')
    if nick:
        leave_current_room_for(nick)
    session.pop('nickname', None)
    flash("Вышли из системы.")
    return redirect(url_for('index'))

# ------------------------
# SocketIO events
# ------------------------
@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room')
    nick = data.get('nick')
    if not room or not nick:
        return
    join_room(room)
    # обновим current_room и last_active
    conn = get_db_connection()
    conn.execute("UPDATE users SET current_room = ?, last_active = ? WHERE nickname = ?", (room, datetime.utcnow(), nick))
    conn.commit()
    conn.close()
    # уведомляем участников
    socketio.emit('user_joined', {'nick': nick}, room=room)


@socketio.on('leave_room')
def handle_leave_room(data):
    room = data.get('room')
    nick = data.get('nick')
    if not nick:
        return
    # снимем current_room
    leave_current_room_for(nick)
    if room:
        try:
            socketio_leave_room(room)
            socketio.emit('user_left', {'nick': nick}, room=room)
        except Exception:
            pass


# ------------------------
# Запуск
# ------------------------
if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
