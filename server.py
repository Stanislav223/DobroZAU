import io
import os
import hashlib
import secrets
from datetime import date, datetime
from typing import List

from fastapi import FastAPI, Form, HTTPException, Request, Depends, status, Response, UploadFile, File
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

cloudinary.config(secure=True)

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8647618080:AAFb0elmPHwZXmZlOPPLHDnNaFes1QZfGmI"
BOT_USERNAME = "DobroZAU_bot"
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(DATABASE_URL)
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


Base.metadata.create_all(bind=engine)

# Автоматическое добавление новых колонок в существующие таблицы
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
        print(f"Миграция ({sql}): {e}")


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def hash_password(password: str) -> str:
    salt = "DobroZAU_Salt_2026"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def parse_event_date(date_str: str):
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def get_semester_bounds(today: date):
    """
    1-й семестр: сентябрь – декабрь (9–12)
    2-й семестр: январь – июнь (1–6)
    Лето: июль – август (7–8)
    """
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


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- ГЛАВНАЯ СТРАНИЦА ---
@app.get("/", response_class=HTMLResponse)
def home():
    db = SessionLocal()
    today = date.today()
    today_str = today.isoformat()
    sem_start, sem_end, sem_label = get_semester_bounds(today)

    all_events = db.query(Event).all()
    all_logs = db.query(HoursLog).all()
    users = db.query(User).all()

    users_map = {u.id: u for u in users}
    events_map = {ev.id: ev for ev in all_events}

    # Группируем логи по акциям и по волонтёрам
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

        ev_date = parse_event_date(ev.date)
        if ev_date:
            if ev_date.year == today.year and ev_date.month == today.month:
                month_events_count += 1
                month_hours_sum += ev_hours
            if sem_start <= ev_date <= sem_end:
                semester_events_count += 1
                semester_hours_sum += ev_hours

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

    db.close()

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

    with open("index.html", "r", encoding="utf-8") as f:
        template = Template(f.read())

    return template.render(
        active_events=active_events,
        past_events=past_events,
        leaderboard=top_3,
        stats=stats
    )


# --- СТРАНИЦА ПОДРОБНОСТЕЙ АКЦИИ И ГАЛЕРЕИ ---
@app.get("/event/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int):
    db = SessionLocal()
    ev = db.query(Event).filter(Event.id == event_id).first()
    if not ev:
        db.close()
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

    media_items = db.query(EventMedia).filter(EventMedia.event_id == ev.id).order_by(EventMedia.id.desc()).all()
    today_str = date.today().isoformat()
    is_past = str(ev.date) < today_str

    db.close()

    with open("event_detail.html", "r", encoding="utf-8") as f:
        template = Template(f.read())

    return template.render(
        event=ev,
        is_past=is_past,
        participants=participants,
        media_items=media_items
    )


# --- ФОРМА «ПРЕДЛОЖИТЬ АКЦИЮ» ---
@app.post("/suggest-event")
def suggest_event(
    author_name: str = Form(...),
    contact: str = Form(""),
    title: str = Form(...),
    description: str = Form("")
):
    db = SessionLocal()
    suggestion = EventSuggestion(
        author_name=author_name.strip(),
        contact=contact.strip() if contact.strip() else None,
        title=title.strip(),
        description=description.strip() if description.strip() else None,
        created_at=date.today().isoformat()
    )
    db.add(suggestion)
    db.commit()
    db.close()
    return HTMLResponse("<script>alert('Спасибо! Ваша идея отправлена координаторам.'); window.location.href='/';</script>")


# --- АВТОРИЗАЦИЯ АДМИНИСТРАТОРА ---
security = HTTPBasic()

ADMIN_USERNAME = "adminZao"
ADMIN_PASSWORD = "2217190808"


def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# --- СТРАНИЦА АДМИНИСТРАТОРА ---
@app.get("/admin", response_class=HTMLResponse)
def admin_page(username: str = Depends(check_admin)):
    db = SessionLocal()
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
    db.close()

    with open("admin.html", "r", encoding="utf-8") as f:
        template = Template(f.read())

    return template.render(
        events=events,
        users=users,
        history=history,
        suggestions=suggestions,
        event_media_map=event_media_map
    )


# Создание акции
@app.post("/admin/add-event")
def add_event(
    title: str = Form(...),
    date: str = Form(...),
    location: str = Form("По договорённости"),
    category: str = Form("Волонтёрство"),
    description: str = Form(""),
    video_url: str = Form(""),
    media_file: UploadFile = File(None)
):
    final_media_url = video_url.strip() if video_url.strip() else None

    if media_file and media_file.filename:
        upload_result = cloudinary.uploader.upload(
            media_file.file,
            resource_type="auto",
            folder="dobrozau_events"
        )
        final_media_url = upload_result.get("secure_url")

    db = SessionLocal()
    new_event = Event(
        title=title.strip(),
        date=date,
        location=location.strip() if location.strip() else "По договорённости",
        category=category.strip() if category.strip() else "Волонтёрство",
        description=description.strip() if description.strip() else None,
        video_url=final_media_url
    )
    db.add(new_event)
    db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Редактирование акции
@app.post("/admin/edit-event/{event_id}")
def edit_event(
    event_id: int,
    title: str = Form(...),
    date: str = Form(...),
    location: str = Form("По договорённости"),
    category: str = Form("Волонтёрство"),
    description: str = Form(""),
    video_url: str = Form(""),
    media_file: UploadFile = File(None)
):
    db = SessionLocal()
    ev = db.query(Event).filter(Event.id == event_id).first()
    if ev:
        ev.title = title.strip()
        ev.date = date
        ev.location = location.strip() if location.strip() else "По договорённости"
        ev.category = category.strip() if category.strip() else "Волонтёрство"
        ev.description = description.strip() if description.strip() else None

        if media_file and media_file.filename:
            upload_result = cloudinary.uploader.upload(
                media_file.file,
                resource_type="auto",
                folder="dobrozau_events"
            )
            ev.video_url = upload_result.get("secure_url")
        elif video_url.strip():
            ev.video_url = video_url.strip()

        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Загрузка фото/видео в галерею акции (поддержка нескольких файлов сразу)
@app.post("/admin/event/{event_id}/add-media")
def add_event_media(
    event_id: int,
    files: List[UploadFile] = File(...)
):
    db = SessionLocal()
    ev = db.query(Event).filter(Event.id == event_id).first()
    if ev:
        for file in files:
            if file and file.filename:
                upload_result = cloudinary.uploader.upload(
                    file.file,
                    resource_type="auto",
                    folder="dobrozau_gallery"
                )
                url = upload_result.get("secure_url")
                if url:
                    db.add(EventMedia(event_id=event_id, media_url=url))
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Удаление файла из галереи акции
@app.post("/admin/delete-media/{media_id}")
def delete_event_media(media_id: int):
    db = SessionLocal()
    media = db.query(EventMedia).filter(EventMedia.id == media_id).first()
    if media:
        db.delete(media)
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Удаление предложенной акции
@app.post("/admin/delete-suggestion/{suggestion_id}")
def delete_suggestion(suggestion_id: int):
    db = SessionLocal()
    s = db.query(EventSuggestion).filter(EventSuggestion.id == suggestion_id).first()
    if s:
        db.delete(s)
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Экспорт отчёта в Excel (.xlsx)
@app.get("/admin/export-excel")
def export_excel(period: str = "all", username: str = Depends(check_admin)):
    db = SessionLocal()
    today = date.today()
    all_events = db.query(Event).order_by(Event.date.desc()).all()
    all_users = db.query(User).all()
    all_logs = db.query(HoursLog).all()
    db.close()

    # Фильтруем акции по выбранному периоду (month / semester / all)
    filtered_events = [ev for ev in all_events if is_in_period(parse_event_date(ev.date), period, today)]
    filtered_event_ids = {ev.id for ev in filtered_events}
    filtered_logs = [l for l in all_logs if l.event_id in filtered_event_ids]

    wb = Workbook()

    # Лист 1: Сводка по волонтёрам
    ws_users = wb.active
    ws_users.title = "Волонтёры"
    headers_users = ["ФИО", "Логин", "Кол-во акций", "Часы"]
    ws_users.append(headers_users)
    for cell in ws_users[1]:
        cell.font = Font(bold=True)

    user_stats = []
    for u in all_users:
        u_logs = [l for l in filtered_logs if l.user_id == u.id]
        u_hours = round(sum((l.hours or 0.0) for l in u_logs), 1)
        u_events_cnt = len({l.event_id for l in u_logs})
        if period == "all" or u_events_cnt > 0:
            user_stats.append((u.name, u.username or "", u_events_cnt, u_hours))

    user_stats.sort(key=lambda x: x[3], reverse=True)
    for row in user_stats:
        ws_users.append(list(row))

    # Лист 2: Список акций
    ws_events = wb.create_sheet(title="Акции")
    headers_events = ["Название", "Категория", "Дата", "Место", "Участников", "Всего часов"]
    ws_events.append(headers_events)
    for cell in ws_events[1]:
        cell.font = Font(bold=True)

    for ev in filtered_events:
        ev_logs = [l for l in filtered_logs if l.event_id == ev.id]
        ev_hours = round(sum((l.hours or 0.0) for l in ev_logs), 1)
        ws_events.append([
            ev.title,
            ev.category or "Волонтёрство",
            ev.date,
            ev.location or "По договорённости",
            len(ev_logs),
            ev_hours
        ])

    # Автоширина колонок
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
    user_id: int = Form(...), event_id: int = Form(...), hours: float = Form(...)
):
    db = SessionLocal()
    new_log = HoursLog(user_id=user_id, event_id=event_id, hours=hours)
    db.add(new_log)
    db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Ручное добавление волонтёра
@app.post("/admin/add-user")
def add_user_manual(
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form("123456")
):
    db = SessionLocal()
    clean_username = username.strip().lstrip("@").lower()

    existing = db.query(User).filter(User.username == clean_username).first()
    if existing:
        db.close()
        return HTMLResponse("<script>alert('Волонтёр с таким логином уже зарегистрирован!'); window.history.back();</script>")

    new_user = User(
        name=name.strip(),
        username=clean_username,
        password_hash=hash_password(password.strip()),
        role="volunteer"
    )
    db.add(new_user)
    db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-user/{user_id}")
def delete_user(user_id: int):
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        db.query(HoursLog).filter(HoursLog.user_id == user_id).delete()
        db.delete(user)
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-hours/{log_id}")
def delete_hours(log_id: int):
    db = SessionLocal()
    log = db.query(HoursLog).filter(HoursLog.id == log_id).first()
    if log:
        db.delete(log)
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/delete-event/{event_id}")
@app.post("/admin/delete-event/{event_id}")
def delete_event(event_id: int):
    db = SessionLocal()
    try:
        db.query(HoursLog).filter(HoursLog.event_id == event_id).delete()
        db.query(EventMedia).filter(EventMedia.event_id == event_id).delete()
        event = db.query(Event).filter(Event.id == event_id).first()
        if event:
            db.delete(event)
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"Ошибка при удалении акции: {e}")
    finally:
        db.close()
    return RedirectResponse(url="/admin", status_code=303)


# --- АВТОРИЗАЦИЯ И ЛИЧНЫЙ КАБИНЕТ ВОЛОНТЁРА ---
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user_id = request.cookies.get("volunteer_user_id")
    if user_id:
        return RedirectResponse(url="/profile", status_code=303)
    with open("login.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/auth/login")
def auth_login(response: Response, login: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    login_clean = login.strip().lstrip("@").lower()
    user = db.query(User).filter(User.username == login_clean).first()

    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        db.close()
        return HTMLResponse("<script>alert('Неверный логин или пароль!'); window.location.href='/login';</script>")

    db.close()
    resp = RedirectResponse(url="/profile", status_code=303)
    resp.set_cookie(key="volunteer_user_id", value=str(user.id), httponly=True, max_age=86400 * 30)
    return resp


@app.post("/auth/register")
def auth_register(response: Response, name: str = Form(...), login: str = Form(...), password: str = Form(...)):
    db = SessionLocal()
    login_clean = login.strip().lstrip("@").lower()

    existing = db.query(User).filter(User.username == login_clean).first()
    if existing:
        db.close()
        return HTMLResponse("<script>alert('Этот логин уже занят! Выберите другой.'); window.history.back();</script>")

    new_user = User(
        name=name.strip(),
        username=login_clean,
        password_hash=hash_password(password)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    new_id = new_user.id
    db.close()

    resp = RedirectResponse(url="/profile", status_code=303)
    resp.set_cookie(key="volunteer_user_id", value=str(new_id), httponly=True, max_age=86400 * 30)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("volunteer_user_id")
    return resp


@app.get("/profile", response_class=HTMLResponse)
def user_profile(request: Request):
    user_id = request.cookies.get("volunteer_user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    db = SessionLocal()
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        db.close()
        return RedirectResponse(url="/logout", status_code=303)

    logs = db.query(HoursLog).filter(HoursLog.user_id == user.id).order_by(HoursLog.id.desc()).all()
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
    db.close()

    with open("profile.html", "r", encoding="utf-8") as f:
        template = Template(f.read())

    return template.render(
        user=user,
        total_hours=round(total_hours, 1),
        history=user_history,
        achievements=achievements
    )


@app.post("/profile/update")
def update_profile(
    request: Request,
    new_login: str = Form(...),
    old_password: str = Form(...),
    new_password: str = Form("")
):
    user_id = request.cookies.get("volunteer_user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=303)

    db = SessionLocal()
    user = db.query(User).filter(User.id == int(user_id)).first()

    if not user.password_hash or not verify_password(old_password, user.password_hash):
        db.close()
        return HTMLResponse("<script>alert('Текущий пароль введён неверно!'); window.history.back();</script>")

    login_clean = new_login.strip().lstrip("@").lower()
    if login_clean != user.username:
        taken = db.query(User).filter(User.username == login_clean).first()
        if taken:
            db.close()
            return HTMLResponse("<script>alert('Такой логин уже занят!'); window.history.back();</script>")
        user.username = login_clean

    if new_password.strip():
        user.password_hash = hash_password(new_password.strip())

    db.commit()
    db.close()
    return HTMLResponse("<script>alert('Данные профиля успешно обновлены!'); window.location.href='/profile';</script>")
