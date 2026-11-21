import io
import os
import re
import hmac
import time
import html
import hashlib
import secrets
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import (
    FastAPI, Form, HTTPException, Request, Depends, status, Response, UploadFile, File
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import Column, Float, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
import cloudinary
import cloudinary.uploader

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dobrozau")

cloudinary.config(secure=True)

# --- НАСТРОЙКИ И СЕКРЕТЫ (из переменных окружения) ---
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "adminZao")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "2217190808")

_env_secret = os.getenv("SECRET_KEY")
if _env_secret:
    SECRET_KEY = _env_secret
else:
    logger.warning(
        "ВНИМАНИЕ: переменная SECRET_KEY не задана! Добавьте случайный SECRET_KEY в Environment Variables."
    )
    SECRET_KEY = hashlib.sha256(
        f"fallback:{DATABASE_URL or 'local'}:{ADMIN_PASSWORD}".encode("utf-8")
    ).hexdigest()

if ADMIN_PASSWORD == "2217190808":
    logger.warning(
        "ВНИМАНИЕ: используется стандартный пароль админа! Задайте новый ADMIN_PASSWORD в Environment Variables."
    )

# Ограничения на файлы и данные
MAX_UPLOAD_SIZE = 15 * 1024 * 1024  # 15 МБ
SESSION_MAX_AGE = 86400 * 30        # 30 дней
ALLOWED_MEDIA_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/webm", "video/quicktime"
}
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")

# --- БАЗА ДАННЫХ ---
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
else:
    engine = create_engine(
        "sqlite:///database.db", connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# --- ТАБЛИЦЫ ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    username = Column(String, unique=True, index=True, nullable=True)
    password_hash = Column(String, nullable=True)
    role = Column(String, default="volunteer")


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    date = Column(String, nullable=False)
    location = Column(String, default="По договорённости")
    category = Column(String, default="Волонтёрство")
    description = Column(String, nullable=True)
    video_url = Column(String, nullable=True)


class HoursLog(Base):
    __tablename__ = "hours_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    event_id = Column(Integer, ForeignKey("events.id"))
    hours = Column(Float)


class EventMedia(Base):
    __tablename__ = "event_media"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    media_url = Column(String, nullable=False)


class EventSuggestion(Base):
    __tablename__ = "event_suggestions"
    id = Column(Integer, primary_key=True, index=True)
    author_name = Column(String, nullable=False)
    contact = Column(String, nullable=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(String, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(String, nullable=False)
    actor = Column(String, nullable=False)
    ip = Column(String, nullable=True)
    action = Column(String, nullable=False)
    details = Column(String, nullable=True)


Base.metadata.create_all(bind=engine)

migrations = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR;",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS location VARCHAR DEFAULT 'По договорённости';",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS video_url VARCHAR;",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS category VARCHAR DEFAULT 'Волонтёрство';",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS description VARCHAR;",
]
for sql in migrations:
    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
    except Exception as e:
        logger.debug(f"Миграция пропущена ({sql}): {e}")


# --- МОДУЛЬ БЕЗОПАСНОСТИ ---
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def log_audit(db, actor: str, ip: str, action: str, details: str = ""):
    try:
        entry = AuditLog(
            created_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            actor=actor[:80],
            ip=ip[:64],
            action=action[:80],
            details=details[:500]
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка записи в AuditLog: {e}")


def hash_password(password: str) -> str:
    """Надёжное хэширование PBKDF2-HMAC-SHA256 (310 000 итераций + уникальная соль)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000)
    return f"pbkdf2$310000${salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> tuple[bool, bool]:
    """
    Возвращает (is_valid, needs_rehash).
    Поддерживает старые хэши sha256 и автоматически помечает их для обновления.
    """
    if not hashed:
        return False, False

    if hashed.startswith("pbkdf2$"):
        parts = hashed.split("$")
        try:
            if len(parts) == 4:
                _, iter_str, salt, hex_dk = parts
                iterations = int(iter_str)
            elif len(parts) == 3:
                _, salt, hex_dk = parts
                iterations = 200_000
            else:
                return False, False
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
            valid = secrets.compare_digest(dk.hex(), hex_dk)
            return valid, (valid and iterations < 310_000)
        except Exception:
            return False, False

    # Совместимость со старым форматом sha256
    legacy_hash = hashlib.sha256((password + "DobroZAU_Salt_2026").encode("utf-8")).hexdigest()
    valid = secrets.compare_digest(legacy_hash, hashed)
    return valid, valid


def _pw_fingerprint(password_hash: Optional[str]) -> str:
    """Короткий отпечаток хэша пароля: при смене пароля старые сессии мгновенно сгорают."""
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:10]


def sign_session(user_id: int, password_hash: Optional[str]) -> str:
    issued_at = int(time.time())
    fp = _pw_fingerprint(password_hash)
    payload = f"{user_id}.{issued_at}.{fp}"
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_cookie(cookie_value: Optional[str]) -> Optional[tuple[int, str]]:
    """Проверяет подпись и срок действия куки. Возвращает (user_id, pw_fingerprint) или None."""
    if not cookie_value:
        return None
    parts = cookie_value.split(".")
    if len(parts) != 4:
        return None
    uid_str, issued_str, fp, sig = parts
    if not (uid_str.isdigit() and issued_str.isdigit()):
        return None

    if int(time.time()) - int(issued_str) > SESSION_MAX_AGE:
        return None

    payload = f"{uid_str}.{issued_str}.{fp}"
    expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if secrets.compare_digest(sig, expected_sig):
        return int(uid_str), fp
    return None


def get_current_user(request: Request, db) -> Optional[User]:
    parsed = verify_session_cookie(request.cookies.get("volunteer_user_id"))
    if not parsed:
        return None
    user_id, fp = parsed
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None
    if not secrets.compare_digest(_pw_fingerprint(user.password_hash), fp):
        return None
    return user


def set_session_cookie(resp: Response, request: Request, user: User):
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(
        key="volunteer_user_id",
        value=sign_session(user.id, user.password_hash),
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=SESSION_MAX_AGE
    )


# Rate Limiter с защитой от утечки памяти
_rate_buckets = defaultdict(list)


def check_rate_limit(request: Request, action: str, max_requests: int, window_seconds: int):
    now = time.time()
    if len(_rate_buckets) > 5000:
        _rate_buckets.clear()

    client_ip = get_client_ip(request)
    key = f"{action}:{client_ip}"
    _rate_buckets[key] = [ts for ts in _rate_buckets[key] if now - ts < window_seconds]
    if len(_rate_buckets[key]) >= max_requests:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток. Подождите немного и попробуйте снова."
        )
    _rate_buckets[key].append(now)


def sanitize_url(url: str) -> Optional[str]:
    clean = url.strip()
    if not clean:
        return None
    parsed = urlparse(clean)
    if parsed.scheme in ("https", "http") and parsed.netloc:
        return clean[:1000]
    return None


def safe_excel_cell(value):
    """Защита от Excel Formula Injection (=, +, -, @ в начале текста)."""
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def is_valid_media_magic_bytes(data: bytes) -> bool:
    """Проверка реальной сигнатуры файла (magic bytes), а не только расширения."""
    if len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[4:8] == b"ftyp":
        return True
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    return False


async def validate_and_upload_media(file: UploadFile, folder: str) -> Optional[str]:
    if not file or not file.filename:
        return None
    if file.content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Разрешена загрузка только фото и видео.")
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="Файл слишком большой (максимум 15 МБ).")
    if not is_valid_media_magic_bytes(content):
        raise HTTPException(status_code=400, detail="Содержимое файла не соответствует формату фото или видео.")

    upload_result = cloudinary.uploader.upload(
        io.BytesIO(content),
        resource_type="auto",
        folder=folder
    )
    return upload_result.get("secure_url")


def render_html(filename: str, **context) -> str:
    with open(filename, "r", encoding="utf-8") as f:
        template = Template(f.read(), autoescape=True)
    return template.render(**context)


def alert_redirect(message: str, redirect_url: Optional[str] = None) -> HTMLResponse:
    safe_msg = html.escape(message, quote=True)
    action = f"window.location.href='{redirect_url}';" if redirect_url else "window.history.back();"
    return HTMLResponse(
        f"<!doctype html><html lang='ru'><meta charset='utf-8'>"
        f"<script>alert({safe_msg!r}); {action}</script></html>"
    )


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def parse_event_date(date_str: str):
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def get_semester_bounds(today: date):
    if 9 <= today.month <= 12:
        return date(today.year, 9, 1), date(today.year, 12, 31), "1-й семестр (сен–дек)"
    elif 1 <= today.month <= 6:
        return date(today.year, 1, 1), date(today.year, 6, 30), "2-й семестр (янв–июн)"
    else:
        return date(today.year, 7, 1), date(today.year, 8, 31), "Летний период (июл–авг)"


def is_in_period(ev_date, period: str, today: date) -> bool:
    if period == "all":
        return True
    if not ev_date:
        return False
    if period == "month":
        return ev_date.year == today.year and ev_date.month == today.month
    if period == "semester":
        sem_start, sem_end, _ = get_semester_bounds(today)
        return sem_start <= ev_date <= sem_end
    return True


def build_achievements(user_events: list, total_hours: float) -> list:
    events_count = len(user_events)
    categories = [(ev.category or "Волонтёрство").strip().lower() for ev in user_events if ev]

    eco_count = sum(1 for c in categories if "эко" in c)
    zozh_count = sum(1 for c in categories if "зож" in c)
    help_count = sum(1 for c in categories if "адресн" in c)

    rules = [
        ("🌱", "Первый шаг", "Участие в 1 акции", events_count, 1, "акц."),
        ("🔥", "Активист", "Участие в 5 акциях", events_count, 5, "акц."),
        ("⭐", "10 часов добра", "Набрано 10 волонтёрских часов", round(total_hours, 1), 10, "ч."),
        ("🏆", "25 часов добра", "Набрано 25 волонтёрских часов", round(total_hours, 1), 25, "ч."),
        ("👑", "50 часов добра", "Набрано 50 волонтёрских часов", round(total_hours, 1), 50, "ч."),
        ("🌿", "Эко-герой", "Участие в 3 эко-акциях", eco_count, 3, "акц."),
        ("🏃", "За ЗОЖ", "Участие в 3 акциях ЗОЖ", zozh_count, 3, "акц."),
        ("🤝", "Доброе сердце", "3 акции адресной помощи", help_count, 3, "акц."),
    ]

    result = []
    for icon, title, desc, current, target, unit in rules:
        unlocked = current >= target
        shown_current = target if unlocked else current
        percent = min(100, int((current / target) * 100)) if target else 100
        result.append({
            "icon": icon,
            "title": title,
            "desc": desc,
            "unlocked": unlocked,
            "progress_text": f"{shown_current} / {target} {unit}",
            "percent": percent,
        })
    return result


# --- ПРИЛОЖЕНИЕ И MIDDLEWARE БЕЗОПАСНОСТИ ---
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Защита от CSRF на всех изменяющих запросах
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        sec_fetch_site = request.headers.get("sec-fetch-site")
        if sec_fetch_site and sec_fetch_site == "cross-site":
            return HTMLResponse("Forbidden: cross-site request blocked", status_code=403)

        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin:
            origin_host = urlparse(origin).netloc.lower()
            if host and origin_host and origin_host != host:
                return HTMLResponse("Forbidden: invalid origin", status_code=403)
        elif referer:
            ref_host = urlparse(referer).netloc.lower()
            if host and ref_host and ref_host != host:
                return HTMLResponse("Forbidden: invalid referer", status_code=403)

    # 2. Обработка запроса без утечки внутренних ошибок
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception(f"Внутренняя ошибка на {request.url.path}: {exc}")
        return HTMLResponse("Внутренняя ошибка сервера. Попробуйте позже.", status_code=500)

    # 3. Заголовки безопасности
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https:; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    if is_https:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


# --- ГЛАВНАЯ СТРАНИЦА ---
@app.get("/", response_class=HTMLResponse)
def home():
    db = SessionLocal()
    try:
        today = date.today()
        today_str = today.isoformat()
        sem_start, sem_end, sem_label = get_semester_bounds(today)

        all_events = db.query(Event).all()
        all_logs = db.query(HoursLog).all()
        users = db.query(User).all()

        users_map = {u.id: u for u in users}

        logs_by_event = {}
        logs_by_user = {}
        for log in all_logs:
            logs_by_event.setdefault(log.event_id, []).append(log)
            logs_by_user.setdefault(log.user_id, []).append(log)

        events_data = []
        month_events_count = 0
        month_hours_sum = 0.0
        semester_events_count = 0
        semester_hours_sum = 0.0

        for ev in all_events:
            ev_logs = logs_by_event.get(ev.id, [])
            participants = []
            ev_hours = 0.0
            for log in ev_logs:
                ev_hours += (log.hours or 0.0)
                u = users_map.get(log.user_id)
                if u:
                    participants.append({
                        "name": u.name,
                        "username": u.username,
                        "hours": log.hours
                    })

           for ev in all_events:
            ev_logs = logs_by_event.get(ev.id, [])
            participants = []
            for log in ev_logs:
                u = users_map.get(log.user_id)
                if u:
                    participants.append({
                        "name": u.name,
                        "username": u.username,
                        "hours": log.hours
                    })

            # Длительность самой акции (максимум часов среди её участников, а не сумма)
            ev_duration = max(((log.hours or 0.0) for log in ev_logs), default=0.0)

            ev_date = parse_event_date(ev.date)
            if ev_date:
                if ev_date.year == today.year and ev_date.month == today.month:
                    month_events_count += 1
                    month_hours_sum += ev_duration
                if sem_start <= ev_date <= sem_end:
                    semester_events_count += 1
                    semester_hours_sum += ev_duration

            events_data.append({
                "id": ev.id,
                "title": ev.title,
                "date": ev.date,
                "category": ev.category or "Волонтёрство",
                "description": ev.description,
                "location": ev.location or "По договорённости",
                "video_url": ev.video_url,
                "is_past": str(ev.date) < today_str,
                "participants": participants
            })

        active_events = [e for e in events_data if not e["is_past"]]
        active_events.sort(key=lambda x: str(x["date"]))

        past_events = [e for e in events_data if e["is_past"]]
        past_events.sort(key=lambda x: str(x["date"]), reverse=True)

        leaderboard = []
        for u in users:
            u_logs = logs_by_user.get(u.id, [])
            total_hours = sum((l.hours or 0.0) for l in u_logs)
            leaderboard.append({
                "name": u.name,
                "username": u.username,
                "hours": round(total_hours, 1),
                "role": u.role,
            })

        leaderboard.sort(key=lambda x: x["hours"], reverse=True)
        top_3 = leaderboard[:3]

        stats = {
            "volunteers_total": len(users),
            "month_events": month_events_count,
            "month_hours": round(month_hours_sum, 1),
            "semester_events": semester_events_count,
            "semester_hours": round(semester_hours_sum, 1),
            "semester_label": sem_label,
        }
    finally:
        db.close()

    return render_html(
        "index.html",
        active_events=active_events,
        past_events=past_events,
        leaderboard=top_3,
        stats=stats
    )


# --- СТРАНИЦА ПОДРОБНОСТЕЙ АКЦИИ И ГАЛЕРЕИ ---
@app.get("/event/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int):
    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.id == event_id).first()
        if not ev:
            raise HTTPException(status_code=404, detail="Акция не найдена")

        logs = db.query(HoursLog).filter(HoursLog.event_id == ev.id).all()
        participants = []
        for log in logs:
            u = db.query(User).filter(User.id == log.user_id).first()
            if u:
                participants.append({
                    "name": u.name,
                    "username": u.username,
                    "hours": log.hours
                })

        media_items = (
            db.query(EventMedia)
            .filter(EventMedia.event_id == ev.id)
            .order_by(EventMedia.id.desc())
            .all()
        )
        today_str = date.today().isoformat()
        is_past = str(ev.date) < today_str

        return render_html(
            "event_detail.html",
            event=ev,
            is_past=is_past,
            participants=participants,
            media_items=media_items
        )
    finally:
        db.close()


# --- ФОРМА «ПРЕДЛОЖИТЬ АКЦИЮ» ---
@app.post("/suggest-event")
def suggest_event(
    request: Request,
    author_name: str = Form(...),
    contact: str = Form(""),
    title: str = Form(...),
    description: str = Form("")
):
    check_rate_limit(request, "suggest_event", max_requests=3, window_seconds=3600)

    clean_author = author_name.strip()[:120]
    clean_title = title.strip()[:200]
    if not clean_author or not clean_title:
        return alert_redirect("Пожалуйста, укажите имя и название акции.")

    db = SessionLocal()
    try:
        suggestion = EventSuggestion(
            author_name=clean_author,
            contact=contact.strip()[:120] if contact.strip() else None,
            title=clean_title,
            description=description.strip()[:2000] if description.strip() else None,
            created_at=date.today().isoformat()
        )
        db.add(suggestion)
        db.commit()
    finally:
        db.close()

    return alert_redirect("Спасибо! Ваша идея отправлена координаторам.", "/")


# --- АВТОРИЗАЦИЯ АДМИНИСТРАТОРА ---
security = HTTPBasic()


def check_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        check_rate_limit(request, "admin_fail", max_requests=5, window_seconds=900)
        db = SessionLocal()
        try:
            log_audit(db, credentials.username or "unknown", get_client_ip(request), "admin_login_failed")
        finally:
            db.close()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# --- СТРАНИЦА АДМИНИСТРАТОРА ---
@app.get("/admin", response_class=HTMLResponse)
def admin_page(admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        events = db.query(Event).order_by(Event.date.desc()).all()
        users = db.query(User).all()
        logs = db.query(HoursLog).order_by(HoursLog.id.desc()).limit(20).all()
        suggestions = db.query(EventSuggestion).order_by(EventSuggestion.id.desc()).all()
        all_media = db.query(EventMedia).order_by(EventMedia.id.desc()).all()

        event_media_map = {}
        for m in all_media:
            event_media_map.setdefault(m.event_id, []).append(m)

        history = []
        for l in logs:
            u = db.query(User).filter(User.id == l.user_id).first()
            ev = db.query(Event).filter(Event.id == l.event_id).first()
            history.append({
                "id": l.id,
                "volunteer_name": u.name if u else "Удалённый волонтёр",
                "event_title": ev.title if ev else "Удалённая акция",
                "hours": l.hours
            })

        return render_html(
            "admin.html",
            events=events,
            users=users,
            history=history,
            suggestions=suggestions,
            event_media_map=event_media_map
        )
    finally:
        db.close()


# Создание акции
@app.post("/admin/add-event")
async def add_event(
    request: Request,
    title: str = Form(...),
    date: str = Form(...),
    location: str = Form("По договорённости"),
    category: str = Form("Волонтёрство"),
    description: str = Form(""),
    video_url: str = Form(""),
    media_file: UploadFile = File(None),
    admin: str = Depends(check_admin)
):
    final_media_url = sanitize_url(video_url)
    if media_file and media_file.filename:
        final_media_url = await validate_and_upload_media(media_file, "dobrozau_events")

    db = SessionLocal()
    try:
        new_event = Event(
            title=title.strip()[:200],
            date=date.strip()[:20],
            location=location.strip()[:200] if location.strip() else "По договорённости",
            category=category.strip()[:100] if category.strip() else "Волонтёрство",
            description=description.strip()[:5000] if description.strip() else None,
            video_url=final_media_url
        )
        db.add(new_event)
        db.commit()
        log_audit(db, admin, get_client_ip(request), "add_event", new_event.title)
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Редактирование акции
@app.post("/admin/edit-event/{event_id}")
async def edit_event(
    request: Request,
    event_id: int,
    title: str = Form(...),
    date: str = Form(...),
    location: str = Form("По договорённости"),
    category: str = Form("Волонтёрство"),
    description: str = Form(""),
    video_url: str = Form(""),
    media_file: UploadFile = File(None),
    admin: str = Depends(check_admin)
):
    uploaded_url = None
    if media_file and media_file.filename:
        uploaded_url = await validate_and_upload_media(media_file, "dobrozau_events")

    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev:
            ev.title = title.strip()[:200]
            ev.date = date.strip()[:20]
            ev.location = location.strip()[:200] if location.strip() else "По договорённости"
            ev.category = category.strip()[:100] if category.strip() else "Волонтёрство"
            ev.description = description.strip()[:5000] if description.strip() else None

            if uploaded_url:
                ev.video_url = uploaded_url
            elif video_url.strip():
                safe_vid = sanitize_url(video_url)
                if safe_vid:
                    ev.video_url = safe_vid

            db.commit()
            log_audit(db, admin, get_client_ip(request), "edit_event", f"id={event_id} {ev.title}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Загрузка фото/видео в галерею акции
@app.post("/admin/event/{event_id}/add-media")
async def add_event_media(
    request: Request,
    event_id: int,
    files: List[UploadFile] = File(...),
    admin: str = Depends(check_admin)
):
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Можно загрузить не более 20 файлов за один раз.")

    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev:
            added = 0
            for file in files:
                if file and file.filename:
                    url = await validate_and_upload_media(file, "dobrozau_gallery")
                    if url:
                        db.add(EventMedia(event_id=event_id, media_url=url))
                        added += 1
            db.commit()
            log_audit(db, admin, get_client_ip(request), "add_media", f"event_id={event_id}, count={added}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Удаление файла из галереи акции
@app.post("/admin/delete-media/{media_id}")
def delete_event_media(request: Request, media_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        media = db.query(EventMedia).filter(EventMedia.id == media_id).first()
        if media:
            db.delete(media)
            db.commit()
            log_audit(db, admin, get_client_ip(request), "delete_media", f"media_id={media_id}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Удаление предложенной акции
@app.post("/admin/delete-suggestion/{suggestion_id}")
def delete_suggestion(request: Request, suggestion_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        s = db.query(EventSuggestion).filter(EventSuggestion.id == suggestion_id).first()
        if s:
            db.delete(s)
            db.commit()
            log_audit(db, admin, get_client_ip(request), "delete_suggestion", f"id={suggestion_id}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Экспорт отчёта в Excel (.xlsx)
@app.get("/admin/export-excel")
def export_excel(request: Request, period: str = "all", admin: str = Depends(check_admin)):
    if period not in ("all", "month", "semester"):
        period = "all"

    db = SessionLocal()
    try:
        today = date.today()
        all_events = db.query(Event).order_by(Event.date.desc()).all()
        all_users = db.query(User).all()
        all_logs = db.query(HoursLog).all()
        log_audit(db, admin, get_client_ip(request), "export_excel", f"period={period}")
    finally:
        db.close()

    filtered_events = [ev for ev in all_events if is_in_period(parse_event_date(ev.date), period, today)]
    filtered_event_ids = {ev.id for ev in filtered_events}
    filtered_logs = [l for l in all_logs if l.event_id in filtered_event_ids]

    wb = Workbook()

    # Лист 1: Сводка по волонтёрам
    ws_users = wb.active
    ws_users.title = "Волонтёры"
    ws_users.append(["ФИО", "Логин", "Кол-во акций", "Часы"])
    for cell in ws_users[1]:
        cell.font = Font(bold=True)

    user_stats = []
    for u in all_users:
        u_logs = [l for l in filtered_logs if l.user_id == u.id]
        u_hours = round(sum((l.hours or 0.0) for l in u_logs), 1)
        u_events_cnt = len({l.event_id for l in u_logs})
        if period == "all" or u_events_cnt > 0:
            user_stats.append((
                safe_excel_cell(u.name),
                safe_excel_cell(u.username or ""),
                u_events_cnt,
                u_hours
            ))

    user_stats.sort(key=lambda x: x[3], reverse=True)
    for row in user_stats:
        ws_users.append(list(row))

    # Лист 2: Список акций
    ws_events = wb.create_sheet(title="Акции")
    ws_events.append(["Название", "Категория", "Дата", "Место", "Участников", "Всего часов"])
    for cell in ws_events[1]:
        cell.font = Font(bold=True)

    for ev in filtered_events:
        ev_logs = [l for l in filtered_logs if l.event_id == ev.id]
        ev_hours = round(sum((l.hours or 0.0) for l in ev_logs), 1)
        ws_events.append([
            safe_excel_cell(ev.title),
            safe_excel_cell(ev.category or "Волонтёрство"),
            safe_excel_cell(ev.date),
            safe_excel_cell(ev.location or "По договорённости"),
            len(ev_logs),
            ev_hours
        ])

    for ws in (ws_users, ws_events):
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 45)

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)

    filename = f"DobroZAU_report_{period}_{today.isoformat()}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# Начисление часов
@app.post("/admin/add-hours")
def add_hours(
    request: Request,
    user_id: int = Form(...),
    event_id: int = Form(...),
    hours: float = Form(...),
    admin: str = Depends(check_admin)
):
    if hours <= 0 or hours > 100:
        return alert_redirect("Количество часов должно быть от 0.1 до 100.")

    db = SessionLocal()
    try:
        new_log = HoursLog(user_id=user_id, event_id=event_id, hours=round(hours, 2))
        db.add(new_log)
        db.commit()
        log_audit(db, admin, get_client_ip(request), "add_hours", f"user={user_id}, event={event_id}, hours={hours}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Ручное добавление волонтёра
@app.post("/admin/add-user")
def add_user_manual(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form("123456"),
    admin: str = Depends(check_admin)
):
    clean_username = username.strip().lstrip("@").lower()
    if not USERNAME_RE.match(clean_username):
        return alert_redirect("Логин должен содержать 3–32 символа (латиница, цифры, _, ., -).")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == clean_username).first()
        if existing:
            return alert_redirect("Волонтёр с таким логином уже зарегистрирован!")

        new_user = User(
            name=name.strip()[:120],
            username=clean_username,
            password_hash=hash_password(password.strip()),
            role="volunteer"
        )
        db.add(new_user)
        db.commit()
        log_audit(db, admin, get_client_ip(request), "add_user", clean_username)
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-user/{user_id}")
def delete_user(request: Request, user_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            uname = user.username or str(user_id)
            db.query(HoursLog).filter(HoursLog.user_id == user_id).delete()
            db.delete(user)
            db.commit()
            log_audit(db, admin, get_client_ip(request), "delete_user", uname)
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-hours/{log_id}")
def delete_hours(request: Request, log_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        log = db.query(HoursLog).filter(HoursLog.id == log_id).first()
        if log:
            db.delete(log)
            db.commit()
            log_audit(db, admin, get_client_ip(request), "delete_hours", f"log_id={log_id}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-event/{event_id}")
def delete_event(request: Request, event_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        db.query(HoursLog).filter(HoursLog.event_id == event_id).delete()
        db.query(EventMedia).filter(EventMedia.event_id == event_id).delete()
        event = db.query(Event).filter(Event.id == event_id).first()
        if event:
            title = event.title
            db.delete(event)
            db.commit()
            log_audit(db, admin, get_client_ip(request), "delete_event", f"id={event_id} {title}")
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при удалении акции {event_id}: {e}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# --- АВТОРИЗАЦИЯ И ЛИЧНЫЙ КАБИНЕТ ВОЛОНТЁРА ---
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    db = SessionLocal()
    try:
        if get_current_user(request, db):
            return RedirectResponse(url="/profile", status_code=303)
    finally:
        db.close()
    with open("login.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/auth/login")
def auth_login(request: Request, login: str = Form(...), password: str = Form(...)):
    check_rate_limit(request, "user_login", max_requests=10, window_seconds=300)

    db = SessionLocal()
    try:
        login_clean = login.strip().lstrip("@").lower()
        user = db.query(User).filter(User.username == login_clean).first()

        is_valid, needs_rehash = (
            verify_password(password, user.password_hash) if user else (False, False)
        )
        if not user or not is_valid:
            log_audit(db, login_clean or "unknown", get_client_ip(request), "user_login_failed")
            return alert_redirect("Неверный логин или пароль!", "/login")

        if needs_rehash:
            user.password_hash = hash_password(password)
            db.commit()

        resp = RedirectResponse(url="/profile", status_code=303)
        set_session_cookie(resp, request, user)
        return resp
    finally:
        db.close()


@app.post("/auth/register")
def auth_register(
    request: Request,
    name: str = Form(...),
    login: str = Form(...),
    password: str = Form(...)
):
    check_rate_limit(request, "user_register", max_requests=5, window_seconds=3600)

    clean_name = name.strip()[:120]
    login_clean = login.strip().lstrip("@").lower()

    if not clean_name:
        return alert_redirect("Введите ваше ФИО.")
    if not USERNAME_RE.match(login_clean):
        return alert_redirect("Логин должен содержать 3–32 символа (латиница, цифры, _, ., -).")
    if len(password) < 6 or len(password) > 128:
        return alert_redirect("Пароль должен быть от 6 до 128 символов.")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == login_clean).first()
        if existing:
            return alert_redirect("Этот логин уже занят! Выберите другой.")

        new_user = User(
            name=clean_name,
            username=login_clean,
            password_hash=hash_password(password)
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        log_audit(db, login_clean, get_client_ip(request), "user_register")

        resp = RedirectResponse(url="/profile", status_code=303)
        set_session_cookie(resp, request, new_user)
        return resp
    finally:
        db.close()


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("volunteer_user_id")
    return resp


@app.get("/profile", response_class=HTMLResponse)
def user_profile(request: Request):
    db = SessionLocal()
    try:
        user = get_current_user(request, db)
        if not user:
            resp = RedirectResponse(url="/login", status_code=303)
            resp.delete_cookie("volunteer_user_id")
            return resp

        logs = (
            db.query(HoursLog)
            .filter(HoursLog.user_id == user.id)
            .order_by(HoursLog.id.desc())
            .all()
        )
        user_history = []
        user_events = []
        total_hours = 0.0

        for log in logs:
            ev = db.query(Event).filter(Event.id == log.event_id).first()
            if ev:
                user_events.append(ev)
            user_history.append({
                "event_id": ev.id if ev else None,
                "event_title": ev.title if ev else "Акция штаба",
                "event_date": ev.date if ev else "—",
                "hours": log.hours
            })
            total_hours += (log.hours or 0.0)

        achievements = build_achievements(user_events, total_hours)

        return render_html(
            "profile.html",
            user=user,
            total_hours=round(total_hours, 1),
            history=user_history,
            achievements=achievements
        )
    finally:
        db.close()


@app.post("/profile/update")
def update_profile(
    request: Request,
    new_login: str = Form(...),
    old_password: str = Form(...),
    new_password: str = Form("")
):
    check_rate_limit(request, "profile_update", max_requests=10, window_seconds=600)

    db = SessionLocal()
    try:
        user = get_current_user(request, db)
        if not user:
            return RedirectResponse(url="/login", status_code=303)

        is_valid, _ = verify_password(old_password, user.password_hash)
        if not is_valid:
            return alert_redirect("Текущий пароль введён неверно!")

        login_clean = new_login.strip().lstrip("@").lower()
        if not USERNAME_RE.match(login_clean):
            return alert_redirect("Логин должен содержать 3–32 символа (латиница, цифры, _, ., -).")

        if login_clean != user.username:
            taken = db.query(User).filter(User.username == login_clean).first()
            if taken:
                return alert_redirect("Такой логин уже занят!")
            user.username = login_clean

        if new_password.strip():
            clean_new_pw = new_password.strip()
            if len(clean_new_pw) < 6 or len(clean_new_pw) > 128:
                return alert_redirect("Новый пароль должен быть от 6 до 128 символов.")
            user.password_hash = hash_password(clean_new_pw)

        db.commit()
        log_audit(db, user.username or str(user.id), get_client_ip(request), "profile_update")

        resp = alert_redirect("Данные профиля успешно обновлены!", "/profile")
        set_session_cookie(resp, request, user)
        return resp
    finally:
        db.close()
