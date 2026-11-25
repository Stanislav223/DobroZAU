import io
import os
import re
import hmac
import time
import html
import json
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template
from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
import cloudinary
import cloudinary.uploader

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dobrozau")
cloudinary.config(secure=True)

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "adminZao")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "2217190808")

_env_secret = os.getenv("SECRET_KEY")
if _env_secret:
    SECRET_KEY = _env_secret
else:
    logger.warning("ВНИМАНИЕ: переменная SECRET_KEY не задана!")
    SECRET_KEY = hashlib.sha256(
        f"fallback:{DATABASE_URL or 'local'}:{ADMIN_PASSWORD}".encode("utf-8")
    ).hexdigest()

MAX_UPLOAD_SIZE = 15 * 1024 * 1024
SESSION_MAX_AGE = 86400 * 30
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_MEDIA_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/webm", "video/quicktime"
}
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
else:
    engine = create_engine("sqlite:///database.db", connect_args={"check_same_thread": False})

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
    avatar_url = Column(String, nullable=True)
    podium_photo_url = Column(String, nullable=True)


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
    status = Column(String, default="new")


class NewsPost(Base):
    __tablename__ = "news_posts"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    content = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    is_internal = Column(Boolean, default=False)


class Achievement(Base):
    __tablename__ = "achievements"
    id = Column(Integer, primary_key=True, index=True)
    icon = Column(String, default="🏆")
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    metric_type = Column(String, nullable=False)
    category_filter = Column(String, nullable=True)
    target_value = Column(Float, nullable=False)


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
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR;",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS podium_photo_url VARCHAR;",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS location VARCHAR DEFAULT 'По договорённости';",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS video_url VARCHAR;",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS category VARCHAR DEFAULT 'Волонтёрство';",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS description VARCHAR;",
    "ALTER TABLE event_suggestions ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'new';",
]
for sql in migrations:
    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
    except Exception as e:
        logger.debug(f"Миграция пропущена ({sql}): {e}")


def seed_default_achievements():
    db = SessionLocal()
    try:
        if db.query(Achievement).count() == 0:
            defaults = [
                ("🌱", "Первый шаг", "Участие в 1 акции", "events_count", None, 1),
                ("🔥", "Активист", "Участие в 5 акциях", "events_count", None, 5),
                ("⭐", "10 часов добра", "Набрано 10 волонтёрских часов", "total_hours", None, 10),
                ("🏆", "25 часов добра", "Набрано 25 волонтёрских часов", "total_hours", None, 25),
                ("👑", "50 часов добра", "Набрано 50 волонтёрских часов", "total_hours", None, 50),
                ("🌿", "Эко-герой", "Участие в 3 эко-акциях", "category_count", "эко", 3),
                ("🏃", "За ЗОЖ", "Участие в 3 акциях ЗОЖ", "category_count", "зож", 3),
                ("🤝", "Доброе сердце", "3 акции адресной помощи", "category_count", "адресн", 3),
            ]
            for icon, title, desc, m_type, cat, target in defaults:
                db.add(Achievement(
                    icon=icon, title=title, description=desc,
                    metric_type=m_type, category_filter=cat, target_value=target
                ))
            db.commit()
    finally:
        db.close()


seed_default_achievements()


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
            actor=actor[:80], ip=ip[:64], action=action[:80], details=details[:500]
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка записи в AuditLog: {e}")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000)
    return f"pbkdf2$310000${salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> tuple[bool, bool]:
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
    legacy_hash = hashlib.sha256((password + "DobroZAU_Salt_2026").encode("utf-8")).hexdigest()
    valid = secrets.compare_digest(legacy_hash, hashed)
    return valid, valid


def _pw_fingerprint(password_hash: Optional[str]) -> str:
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:10]


def sign_session(user_id: int, password_hash: Optional[str]) -> str:
    issued_at = int(time.time())
    fp = _pw_fingerprint(password_hash)
    payload = f"{user_id}.{issued_at}.{fp}"
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_cookie(cookie_value: Optional[str]) -> Optional[tuple[int, str]]:
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
        httponly=True, secure=is_https, samesite="lax", max_age=SESSION_MAX_AGE
    )


_rate_buckets = defaultdict(list)


def check_rate_limit(request: Request, action: str, max_requests: int, window_seconds: int):
    now = time.time()
    if len(_rate_buckets) > 5000:
        _rate_buckets.clear()
    key = f"{action}:{get_client_ip(request)}"
    _rate_buckets[key] = [ts for ts in _rate_buckets[key] if now - ts < window_seconds]
    if len(_rate_buckets[key]) >= max_requests:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Подождите немного.")
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
    if isinstance(value, str) and value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def is_valid_media_magic_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):
        return True
    if data[:4] == b"RIFF" and data == b"WEBP":
        return True
    if data[4:8] == b"ftyp" or data.startswith(b"\x1a\x45\xdf\xa3"):
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
    upload_result = cloudinary.uploader.upload(io.BytesIO(content), resource_type="auto", folder=folder)
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


def get_semester_code_for_date(d: date) -> str:
    return f"{d.year}-S1" if 9 <= d.month <= 12 else f"{d.year}-S2"


def format_semester_label(code: str) -> str:
    year_str, sem = code.split("-")
    return f"Осень {year_str} (сен–дек)" if sem == "S1" else f"Весна/лето {year_str} (янв–авг)"


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


def build_events_collections(db):
    today_str = date.today().isoformat()
    all_events = db.query(Event).all()
    all_logs = db.query(HoursLog).all()
    users_map = {u.id: u for u in db.query(User).all()}

    logs_by_event = defaultdict(list)
    for log in all_logs:
        logs_by_event[log.event_id].append(log)

    events_data = []
    for ev in all_events:
        participants = []
        for log in logs_by_event.get(ev.id, []):
            u = users_map.get(log.user_id)
            if u:
                participants.append({"name": u.name, "username": u.username, "hours": log.hours})
        events_data.append({
            "id": ev.id, "title": ev.title, "date": ev.date,
            "category": ev.category or "Волонтёрство",
            "description": ev.description,
            "location": ev.location or "По договорённости",
            "video_url": ev.video_url,
            "is_past": str(ev.date) < today_str,
            "participants": participants
        })

    active_events = sorted([e for e in events_data if not e["is_past"]], key=lambda x: str(x["date"]))
    past_events = sorted([e for e in events_data if e["is_past"]], key=lambda x: str(x["date"]), reverse=True)
    return active_events, past_events


def build_achievements(db, user_events: list, total_hours: float) -> tuple[list, int]:
    all_rules = db.query(Achievement).order_by(Achievement.id.asc()).all()
    events_count = len(user_events)
    categories = [(ev.category or "Волонтёрство").strip().lower() for ev in user_events if ev]

    unlocked = []
    for rule in all_rules:
        if rule.metric_type == "events_count":
            current = events_count
        elif rule.metric_type == "total_hours":
            current = round(total_hours, 1)
        elif rule.metric_type == "category_count":
            needle = (rule.category_filter or "").strip().lower()
            current = sum(1 for c in categories if needle and needle in c)
        else:
            current = 0

        if current >= (rule.target_value or 1):
            unlocked.append({
                "icon": rule.icon or "🏆",
                "title": rule.title,
                "desc": rule.description,
            })

    return unlocked, len(all_rules)


# --- ПРИЛОЖЕНИЕ И MIDDLEWARE ---
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/static/logo.png.jpg", media_type="image/jpeg")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if request.headers.get("sec-fetch-site") == "cross-site":
            return HTMLResponse("Forbidden: cross-site request blocked", status_code=403)
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if origin and host and urlparse(origin).netloc.lower() != host:
            return HTMLResponse("Forbidden: invalid origin", status_code=403)
        elif not origin and referer and host and urlparse(referer).netloc.lower() != host:
            return HTMLResponse("Forbidden: invalid referer", status_code=403)

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.exception(f"Внутренняя ошибка на {request.url.path}: {exc}")
        return HTMLResponse("Внутренняя ошибка сервера. Попробуйте позже.", status_code=500)

    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https:; "
        "script-src 'self' 'unsafe-inline' [https://cdn.tailwindcss.com](https://cdn.tailwindcss.com) [https://unpkg.com](https://unpkg.com) [https://cdn.jsdelivr.net](https://cdn.jsdelivr.net); "
        "style-src 'self' 'unsafe-inline' [https://fonts.googleapis.com](https://fonts.googleapis.com) [https://cdn.tailwindcss.com](https://cdn.tailwindcss.com); "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "font-src 'self' data: [https://fonts.gstatic.com](https://fonts.gstatic.com); "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self';"
    )
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    if is_https:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# --- ПУБЛИЧНЫЕ СТРАНИЦЫ ---
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    db = SessionLocal()
    try:
        today = date.today()
        sem_start, sem_end, sem_label = get_semester_bounds(today)
        current_user = get_current_user(request, db)
        all_events = db.query(Event).all()
        all_logs = db.query(HoursLog).all()
        users = db.query(User).all()

        logs_by_event = defaultdict(list)
        logs_by_user = defaultdict(list)
        for log in all_logs:
            logs_by_event[log.event_id].append(log)
            logs_by_user[log.user_id].append(log)

        month_events_count = semester_events_count = 0
        month_hours_sum = semester_hours_sum = 0.0
        sem_event_ids = set()

        for ev in all_events:
            ev_duration = max(((l.hours or 0.0) for l in logs_by_event.get(ev.id, [])), default=0.0)
            ev_date = parse_event_date(ev.date)
            if ev_date:
                if ev_date.year == today.year and ev_date.month == today.month:
                    month_events_count += 1
                    month_hours_sum += ev_duration
                if sem_start <= ev_date <= sem_end:
                    semester_events_count += 1
                    semester_hours_sum += ev_duration
                    sem_event_ids.add(ev.id)

        podium_candidates = []
        for u in users:
            u_logs = logs_by_user.get(u.id, [])
            sem_hours = sum((l.hours or 0.0) for l in u_logs if l.event_id in sem_event_ids)
            total_hours = sum((l.hours or 0.0) for l in u_logs)
            podium_candidates.append({
                "id": u.id,
                "name": u.name,
                "hours": round(total_hours, 1),
                "semester_hours": round(sem_hours, 1),
                "podium_photo_url": u.podium_photo_url,
            })

        podium_top3 = sorted(
            podium_candidates,
            key=lambda x: (x["semester_hours"], x["hours"]),
            reverse=True
        )[:3]

        active_events, past_events = build_events_collections(db)
        stats = {
            "volunteers_total": len(users),
            "month_events": month_events_count,
            "month_hours": round(month_hours_sum, 1),
            "semester_events": semester_events_count,
            "semester_hours": round(semester_hours_sum, 1),
            "semester_label": sem_label,
        }
        return render_html(
            "index.html",
            active_events=active_events,
            past_events=past_events[:3],
            total_past_count=len(past_events),
            stats=stats,
            podium_top3=podium_top3,
            semester_label=sem_label,
            current_user=current_user,
            active_page="home"
        )
    finally:
        db.close()


@app.get("/events", response_class=HTMLResponse)
def all_events_page(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user(request, db)
        active_events, past_events = build_events_collections(db)
        return render_html(
            "events.html",
            active_events=active_events,
            past_events=past_events,
            current_user=current_user,
            active_page="events"
        )
    finally:
        db.close()


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, mode: str = "year", period: str = ""):
    if mode not in ("year", "semester"):
        mode = "year"
    db = SessionLocal()
    try:
        current_user = get_current_user(request, db)
        today = date.today()
        today_str = today.isoformat()

        all_events = db.query(Event).all()
        all_suggestions = db.query(EventSuggestion).all()
        users = db.query(User).all()
        all_logs = db.query(HoursLog).all()

        years_set = {today.year}
        semesters_set = {get_semester_code_for_date(today)}
        for ev in all_events:
            d = parse_event_date(ev.date)
            if d:
                years_set.add(d.year)
                semesters_set.add(get_semester_code_for_date(d))
        for s in all_suggestions:
            d = parse_event_date(s.created_at)
            if d:
                years_set.add(d.year)
                semesters_set.add(get_semester_code_for_date(d))

        available_years = sorted(years_set, reverse=True)
        available_semesters = [
            {"code": code, "label": format_semester_label(code)}
            for code in sorted(semesters_set, reverse=True)
        ]

        if mode == "year":
            selected_period = period if (period.isdigit() and int(period) in years_set) else str(today.year)
        else:
            valid_sem_codes = {item["code"] for item in available_semesters}
            selected_period = period if period in valid_sem_codes else get_semester_code_for_date(today)

        def matches_selected(d: Optional[date]) -> bool:
            if not d:
                return False
            return str(d.year) == selected_period if mode == "year" else get_semester_code_for_date(d) == selected_period

        completed_events = [
            ev for ev in all_events
            if str(ev.date) < today_str and matches_selected(parse_event_date(ev.date))
        ]

        month_names_full = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
        ]
        if mode == "year":
            target_months = list(range(1, 13))
        else:
            _, sem_part = selected_period.split("-")
            target_months = [9, 10, 11, 12] if sem_part == "S1" else [1, 2, 3, 4, 5, 6, 7, 8]

        month_counts = {m: 0 for m in target_months}
        category_counts = defaultdict(int)
        for ev in completed_events:
            d = parse_event_date(ev.date)
            if d and d.month in month_counts:
                month_counts[d.month] += 1
            category_counts[(ev.category or "Волонтёрство").strip()] += 1

        chart_labels = [month_names_full[m - 1] for m in target_months]
        chart_values = [month_counts[m] for m in target_months]
        cat_labels = list(category_counts.keys()) if category_counts else ["Нет данных"]
        cat_values = list(category_counts.values()) if category_counts else [0]

        period_suggestions = [s for s in all_suggestions if matches_selected(parse_event_date(s.created_at))]
        total_sugg = len(period_suggestions)
        accepted_sugg = sum(1 for s in period_suggestions if (s.status or "new") == "accepted")
        winrate_percent = round((accepted_sugg / total_sugg) * 100) if total_sugg > 0 else 0

        sem_start, sem_end, sem_label = get_semester_bounds(today)
        sem_event_ids = {
            ev.id for ev in all_events
            if (d := parse_event_date(ev.date)) and sem_start <= d <= sem_end
        }

        logs_by_user = defaultdict(list)
        for l in all_logs:
            logs_by_user[l.user_id].append(l)

        leaderboard = []
        for u in users:
            u_logs = logs_by_user.get(u.id, [])
            sem_hours = sum((l.hours or 0.0) for l in u_logs if l.event_id in sem_event_ids)
            total_hours = sum((l.hours or 0.0) for l in u_logs)
            leaderboard.append({
                "id": u.id,
                "name": u.name,
                "hours": round(total_hours, 1),
                "semester_hours": round(sem_hours, 1),
                "events_count": len({l.event_id for l in u_logs}),
                "podium_photo_url": u.podium_photo_url,
            })

        podium_top3 = sorted(
            leaderboard, key=lambda x: (x["semester_hours"], x["hours"]), reverse=True
        )[:3]

        return render_html(
            "analytics.html",
            current_user=current_user,
            active_page="analytics",
            mode=mode,
            selected_period=selected_period,
            available_years=available_years,
            available_semesters=available_semesters,
            completed_count=len(completed_events),
            winrate_percent=winrate_percent,
            accepted_sugg=accepted_sugg,
            total_sugg=total_sugg,
            chart_labels_json=json.dumps(chart_labels, ensure_ascii=False),
            chart_values_json=json.dumps(chart_values),
            cat_labels_json=json.dumps(cat_labels, ensure_ascii=False),
            cat_values_json=json.dumps(cat_values),
            podium_top3=podium_top3,
            semester_label=sem_label
        )
    finally:
        db.close()


@app.get("/volunteers", response_class=HTMLResponse)
def volunteers_page(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user(request, db)
        users = db.query(User).order_by(User.name.asc()).all()
        logs_by_user = defaultdict(list)
        for l in db.query(HoursLog).all():
            logs_by_user[l.user_id].append(l)

        volunteers_list = [
            {
                "name": u.name,
                "events_count": len({l.event_id for l in logs_by_user.get(u.id, [])}),
                "avatar_url": u.avatar_url
            }
            for u in users
        ]
        return render_html(
            "volunteers.html",
            volunteers=volunteers_list,
            current_user=current_user,
            active_page="volunteers"
        )
    finally:
        db.close()


@app.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request):
    db = SessionLocal()
    try:
        current_user = get_current_user(request, db)
        public_news = db.query(NewsPost).filter(NewsPost.is_internal == False).order_by(NewsPost.id.desc()).all()  # noqa: E712
        internal_changes = (
            db.query(NewsPost).filter(NewsPost.is_internal == True).order_by(NewsPost.id.desc()).all()  # noqa: E712
            if current_user else []
        )
        return render_html(
            "updates.html",
            public_news=public_news,
            internal_changes=internal_changes,
            current_user=current_user,
            active_page="updates"
        )
    finally:
        db.close()


@app.get("/event/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: int):
    db = SessionLocal()
    try:
        current_user = get_current_user(request, db)
        ev = db.query(Event).filter(Event.id == event_id).first()
        if not ev:
            raise HTTPException(status_code=404, detail="Акция не найдена")
        participants = []
        for log in db.query(HoursLog).filter(HoursLog.event_id == ev.id).all():
            u = db.query(User).filter(User.id == log.user_id).first()
            if u:
                participants.append({"name": u.name, "hours": log.hours})
        media_items = db.query(EventMedia).filter(EventMedia.event_id == ev.id).order_by(EventMedia.id.desc()).all()
        return render_html(
            "event_detail.html",
            event=ev,
            is_past=str(ev.date) < date.today().isoformat(),
            participants=participants,
            media_items=media_items,
            current_user=current_user,
            active_page="events"
        )
    finally:
        db.close()


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
        db.add(EventSuggestion(
            author_name=clean_author,
            contact=contact.strip()[:120] if contact.strip() else None,
            title=clean_title,
            description=description.strip()[:2000] if description.strip() else None,
            created_at=date.today().isoformat(),
            status="new"
        ))
        db.commit()
    finally:
        db.close()
    return alert_redirect("Спасибо! Ваша идея отправлена координаторам.", "/")


# --- АДМИН-ПАНЕЛЬ ---
security = HTTPBasic()


def check_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, ADMIN_USERNAME)
            and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)):
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


@app.get("/admin", response_class=HTMLResponse)
def admin_page(admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        events = db.query(Event).order_by(Event.date.desc()).all()
        users = db.query(User).all()
        logs = db.query(HoursLog).order_by(HoursLog.id.desc()).limit(20).all()
        suggestions = db.query(EventSuggestion).order_by(EventSuggestion.id.desc()).all()
        news_posts = db.query(NewsPost).order_by(NewsPost.id.desc()).all()
        achievements = db.query(Achievement).order_by(Achievement.id.asc()).all()
        all_media = db.query(EventMedia).order_by(EventMedia.id.desc()).all()

        event_media_map = defaultdict(list)
        for m in all_media:
            event_media_map[m.event_id].append(m)

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
            events=events, users=users, history=history,
            suggestions=suggestions, news_posts=news_posts,
            achievements=achievements, event_media_map=event_media_map
        )
    finally:
        db.close()


@app.post("/admin/add-event")
async def add_event(
    request: Request, title: str = Form(...), date: str = Form(...),
    location: str = Form("По договорённости"), category: str = Form("Волонтёрство"),
    description: str = Form(""), video_url: str = Form(""),
    media_file: UploadFile = File(None), admin: str = Depends(check_admin)
):
    final_media_url = sanitize_url(video_url)
    if media_file and media_file.filename:
        final_media_url = await validate_and_upload_media(media_file, "dobrozau_events")
    db = SessionLocal()
    try:
        ev = Event(
            title=title.strip()[:200], date=date.strip()[:20],
            location=location.strip()[:200] if location.strip() else "По договорённости",
            category=category.strip()[:100] if category.strip() else "Волонтёрство",
            description=description.strip()[:5000] if description.strip() else None,
            video_url=final_media_url
        )
        db.add(ev)
        db.commit()
        log_audit(db, admin, get_client_ip(request), "add_event", ev.title)
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/edit-event/{event_id}")
async def edit_event(
    request: Request, event_id: int, title: str = Form(...), date: str = Form(...),
    location: str = Form("По договорённости"), category: str = Form("Волонтёрство"),
    description: str = Form(""), video_url: str = Form(""),
    media_file: UploadFile = File(None), admin: str = Depends(check_admin)
):
    uploaded_url = await validate_and_upload_media(media_file, "dobrozau_events") if (media_file and media_file.filename) else None
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
            log_audit(db, admin, get_client_ip(request), "edit_event", f"id={event_id}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/event/{event_id}/add-media")
async def add_event_media(
    request: Request, event_id: int, files: List[UploadFile] = File(...), admin: str = Depends(check_admin)
):
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Можно загрузить не более 20 файлов за один раз.")
    db = SessionLocal()
    try:
        if db.query(Event).filter(Event.id == event_id).first():
            for file in files:
                if file and file.filename:
                    url = await validate_and_upload_media(file, "dobrozau_gallery")
                    if url:
                        db.add(EventMedia(event_id=event_id, media_url=url))
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-media/{media_id}")
def delete_event_media(request: Request, media_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        m = db.query(EventMedia).filter(EventMedia.id == media_id).first()
        if m:
            db.delete(m)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/accept-suggestion/{suggestion_id}")
def accept_suggestion(request: Request, suggestion_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        s = db.query(EventSuggestion).filter(EventSuggestion.id == suggestion_id).first()
        if s:
            s.status = "accepted"
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-suggestion/{suggestion_id}")
def delete_suggestion(request: Request, suggestion_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        s = db.query(EventSuggestion).filter(EventSuggestion.id == suggestion_id).first()
        if s:
            if (s.status or "new") == "new":
                s.status = "rejected"
            else:
                db.delete(s)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-news")
def add_news_post(
    request: Request, title: str = Form(...), content: str = Form(...),
    audience: str = Form("public"), admin: str = Depends(check_admin)
):
    db = SessionLocal()
    try:
        db.add(NewsPost(
            title=title.strip()[:200], content=content.strip()[:4000],
            created_at=date.today().isoformat(), is_internal=(audience == "internal")
        ))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-news/{news_id}")
def delete_news_post(request: Request, news_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        p = db.query(NewsPost).filter(NewsPost.id == news_id).first()
        if p:
            db.delete(p)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-achievement")
def add_achievement(
    request: Request, icon: str = Form("🏆"), title: str = Form(...),
    description: str = Form(...), metric_type: str = Form(...),
    category_filter: str = Form(""), target_value: float = Form(...),
    admin: str = Depends(check_admin)
):
    if metric_type not in ("events_count", "total_hours", "category_count") or target_value <= 0:
        return alert_redirect("Проверьте корректность условия и цели достижения.")
    db = SessionLocal()
    try:
        db.add(Achievement(
            icon=(icon.strip() or "🏆")[:8], title=title.strip()[:120],
            description=description.strip()[:250], metric_type=metric_type,
            category_filter=category_filter.strip()[:80] if metric_type == "category_count" else None,
            target_value=target_value
        ))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/edit-achievement/{ach_id}")
def edit_achievement(
    request: Request, ach_id: int, icon: str = Form("🏆"), title: str = Form(...),
    description: str = Form(...), metric_type: str = Form(...),
    category_filter: str = Form(""), target_value: float = Form(...),
    admin: str = Depends(check_admin)
):
    db = SessionLocal()
    try:
        ach = db.query(Achievement).filter(Achievement.id == ach_id).first()
        if ach and metric_type in ("events_count", "total_hours", "category_count") and target_value > 0:
            ach.icon = (icon.strip() or "🏆")[:8]
            ach.title = title.strip()[:120]
            ach.description = description.strip()[:250]
            ach.metric_type = metric_type
            ach.category_filter = category_filter.strip()[:80] if metric_type == "category_count" else None
            ach.target_value = target_value
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-achievement/{ach_id}")
def delete_achievement(request: Request, ach_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        ach = db.query(Achievement).filter(Achievement.id == ach_id).first()
        if ach:
            db.delete(ach)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/user/{user_id}/podium-photo")
async def upload_podium_photo(
    request: Request, user_id: int, photo_file: UploadFile = File(...), admin: str = Depends(check_admin)
):
    if not photo_file or not photo_file.filename:
        return alert_redirect("Выберите PNG/WEBP файл.")
    if photo_file.content_type not in {"image/png", "image/webp", "image/jpeg"}:
        return alert_redirect("Поддерживаются форматы PNG, WEBP или JPG.")
    content = await photo_file.read()
    if len(content) > 10 * 1024 * 1024 or not is_valid_media_magic_bytes(content):
        return alert_redirect("Некорректный файл или превышен размер 10 МБ.")
    url = cloudinary.uploader.upload(io.BytesIO(content), resource_type="image", folder="dobrozau_podium").get("secure_url")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user and url:
            user.podium_photo_url = url
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/user/{user_id}/podium-photo/delete")
def delete_podium_photo(request: Request, user_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.podium_photo_url = None
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


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
    finally:
        db.close()

    filtered_events = [ev for ev in all_events if is_in_period(parse_event_date(ev.date), period, today)]
    filtered_event_ids = {ev.id for ev in filtered_events}
    filtered_logs = [l for l in all_logs if l.event_id in filtered_event_ids]

    wb = Workbook()
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
            user_stats.append((safe_excel_cell(u.name), safe_excel_cell(u.username or ""), u_events_cnt, u_hours))
    user_stats.sort(key=lambda x: x[3], reverse=True)
    for row in user_stats:
        ws_users.append(list(row))

    ws_events = wb.create_sheet(title="Акции")
    ws_events.append(["Название", "Категория", "Дата", "Место", "Участников", "Всего часов"])
    for cell in ws_events[1]:
        cell.font = Font(bold=True)
    for ev in filtered_events:
        ev_logs = [l for l in filtered_logs if l.event_id == ev.id]
        ws_events.append([
            safe_excel_cell(ev.title), safe_excel_cell(ev.category or "Волонтёрство"),
            safe_excel_cell(ev.date), safe_excel_cell(ev.location or "По договорённости"),
            len(ev_logs), round(sum((l.hours or 0.0) for l in ev_logs), 1)
        ])

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    filename = f"DobroZAU_report_{period}_{today.isoformat()}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.post("/admin/add-hours")
def add_hours(
    request: Request, user_id: int = Form(...), event_id: int = Form(...),
    hours: float = Form(...), admin: str = Depends(check_admin)
):
    if hours <= 0 or hours > 100:
        return alert_redirect("Количество часов должно быть от 0.1 до 100.")
    db = SessionLocal()
    try:
        db.add(HoursLog(user_id=user_id, event_id=event_id, hours=round(hours, 2)))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/add-user")
def add_user_manual(
    request: Request, name: str = Form(...), username: str = Form(...),
    password: str = Form("123456"), admin: str = Depends(check_admin)
):
    clean_username = username.strip().lstrip("@").lower()
    if not USERNAME_RE.match(clean_username):
        return alert_redirect("Логин должен содержать 3–32 символа (латиница, цифры, _, ., -).")
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == clean_username).first():
            return alert_redirect("Волонтёр с таким логином уже зарегистрирован!")
        db.add(User(name=name.strip()[:120], username=clean_username, password_hash=hash_password(password.strip())))
        db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-user/{user_id}")
def delete_user(request: Request, user_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            db.query(HoursLog).filter(HoursLog.user_id == user_id).delete()
            db.delete(user)
            db.commit()
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
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-event/{event_id}")
def delete_event(request: Request, event_id: int, admin: str = Depends(check_admin)):
    db = SessionLocal()
    try:
        db.query(HoursLog).filter(HoursLog.event_id == event_id).delete()
        db.query(EventMedia).filter(EventMedia.event_id == event_id).delete()
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev:
            db.delete(ev)
            db.commit()
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# --- ЛИЧНЫЙ КАБИНЕТ И АВТОРИЗАЦИЯ ---
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
        is_valid, needs_rehash = verify_password(password, user.password_hash) if user else (False, False)
        if not user or not is_valid:
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
def auth_register(request: Request, name: str = Form(...), login: str = Form(...), password: str = Form(...)):
    check_rate_limit(request, "user_register", max_requests=5, window_seconds=3600)
    clean_name = name.strip()[:120]
    login_clean = login.strip().lstrip("@").lower()
    if not clean_name or not USERNAME_RE.match(login_clean) or not (6 <= len(password) <= 128):
        return alert_redirect("Проверьте ФИО, логин (3–32 лат. символа) и пароль (от 6 символов).")
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == login_clean).first():
            return alert_redirect("Этот логин уже занят!")
        new_user = User(name=clean_name, username=login_clean, password_hash=hash_password(password))
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
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
        logs = db.query(HoursLog).filter(HoursLog.user_id == user.id).order_by(HoursLog.id.desc()).all()
        user_history, user_events, total_hours = [], [], 0.0
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
        unlocked_achievements, total_achievements_count = build_achievements(db, user_events, total_hours)
        return render_html(
            "profile.html",
            user=user, total_hours=round(total_hours, 1),
            history=user_history,
            achievements=unlocked_achievements,
            total_achievements_count=total_achievements_count
        )
    finally:
        db.close()


@app.post("/profile/avatar")
async def upload_avatar(request: Request, avatar_file: UploadFile = File(...)):
    check_rate_limit(request, "upload_avatar", max_requests=10, window_seconds=600)
    db = SessionLocal()
    try:
        user = get_current_user(request, db)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        if not avatar_file or avatar_file.content_type not in ALLOWED_IMAGE_TYPES:
            return alert_redirect("Можно загружать только изображения (JPG, PNG, WEBP, GIF).")
        content = await avatar_file.read()
        if len(content) > 5 * 1024 * 1024 or not is_valid_media_magic_bytes(content):
            return alert_redirect("Некорректный файл или размер превышает 5 МБ.")
        upload_result = cloudinary.uploader.upload(
            io.BytesIO(content), resource_type="image", folder="dobrozau_avatars",
            transformation=[{"width": 400, "height": 400, "crop": "fill", "gravity": "face"}]
        )
        user.avatar_url = upload_result.get("secure_url")
        db.commit()
        return RedirectResponse(url="/profile", status_code=303)
    finally:
        db.close()


@app.post("/profile/avatar/delete")
def delete_avatar(request: Request):
    db = SessionLocal()
    try:
        user = get_current_user(request, db)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        user.avatar_url = None
        db.commit()
        return RedirectResponse(url="/profile", status_code=303)
    finally:
        db.close()


@app.post("/profile/update")
def update_profile(
    request: Request, new_login: str = Form(...),
    old_password: str = Form(...), new_password: str = Form("")
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
            if db.query(User).filter(User.username == login_clean).first():
                return alert_redirect("Такой логин уже занят!")
            user.username = login_clean
        if new_password.strip():
            if not (6 <= len(new_password.strip()) <= 128):
                return alert_redirect("Новый пароль должен быть от 6 до 128 символов.")
            user.password_hash = hash_password(new_password.strip())
        db.commit()
        resp = alert_redirect("Данные профиля успешно обновлены!", "/profile")
        set_session_cookie(resp, request, user)
        return resp
    finally:
        db.close()
