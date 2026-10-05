import os
import time
import hashlib
import hmac
import secrets
from datetime import date
from fastapi import FastAPI, Form, HTTPException, Request, Depends, status, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
from sqlalchemy import Column, Float, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from fastapi.staticfiles import StaticFiles
# --- НАСТРОЙКИ ---
BOT_TOKEN = "8647618080:AAFb0elmPHwZXmZlOPPLHDnNaFes1QZfGmI"
BOT_USERNAME = "DobroZAU_bot"
# База данных: берем PostgreSQL из настроек Render, иначе SQLite для локального запуска
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Исправление формата префикса для SQLAlchemy
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
    username = Column(String, unique=True, index=True, nullable=True)  # Логин волонтёра
    password_hash = Column(String, nullable=True)                      # Хэш пароля
    role = Column(String, default="volunteer")# 'admin' или 'volunteer'
    
def hash_password(password: str) -> str:
    salt = "DobroZAU_Salt_2026"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

class Event(Base):
  __tablename__ = "events"
  id = Column(Integer, primary_key=True)
  title = Column(String)
  date = Column(String)


class HoursLog(Base):
  __tablename__ = "hours_logs"
  id = Column(Integer, primary_key=True)
  user_id = Column(Integer, ForeignKey("users.id"))
  event_id = Column(Integer, ForeignKey("events.id"))
  hours = Column(Float)


Base.metadata.create_all(bind=engine)
# Автоматическое добавление новой колонки password_hash, если её ещё нет в базе
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR;"))
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS location VARCHAR DEFAULT 'По договорённости';"))
        conn.commit()
except Exception as e:
    print(f"Миграция users: {e}")
    
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- ГЛАВНАЯ СТРАНИЦА: РЕЙТИНГ ---
@app.get("/", response_class=HTMLResponse)
def home():
    db = SessionLocal()
    today_str = date.today().isoformat()

    all_events = db.query(Event).all()
    events_data = []

    for ev in all_events:
        # 1. Находим все часы, начисленные именно за это мероприятие
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

        # 2. Собираем словарь со всеми нужными полями и списком участников
        events_data.append({
            "id": ev.id,
            "title": ev.title,
            "date": ev.date,
            "category": getattr(ev, "category", "Волонтёрство"),
            "description": getattr(ev, "description", None),
            "location": getattr(ev, "location", "По договорённости"),
            "is_past": str(ev.date) < today_str,
            "participants": participants
        })

    # Сортируем: предстоящие по возрастанию даты, прошедшие — по убыванию
    active_events = [e for e in events_data if not e["is_past"]]
    active_events.sort(key=lambda x: str(x["date"]))

    past_events = [e for e in events_data if e["is_past"]]
    past_events.sort(key=lambda x: str(x["date"]), reverse=True)

    # Таблица рейтинга
    users = db.query(User).all()
    leaderboard = []
    for u in users:
        logs = db.query(HoursLog).filter(HoursLog.user_id == u.id).all()
        total_hours = sum(log.hours for log in logs)
        leaderboard.append({
            "name": u.name,
            "username": u.username,
            "hours": total_hours,
            "role": u.role,
        })
    db.close()

    leaderboard.sort(key=lambda x: x["hours"], reverse=True)
    top_3 = leaderboard[:3]  # Берём только первые 3 места

    with open("index.html", "r", encoding="utf-8") as f:
        template = Template(f.read())
    
    return template.render(
        active_events=active_events,
        past_events=past_events,
        leaderboard=top_3  # Вот это и есть "передать в шаблон" — отправляем в index.html только тройку лидеров
    )
# --- АВТОРИЗАЦИЯ АДМИНИСТРАТОРА ---
security = HTTPBasic()

ADMIN_USERNAME = "adminZao"
ADMIN_PASSWORD = "2217190808"  # замени на свой пароль

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
    events = db.query(Event).all()
    users = db.query(User).all()
    logs = db.query(HoursLog).order_by(HoursLog.id.desc()).limit(20).all()
    
    # Собираем понятный список последних начислений:
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

    return template.render(events=events, users=users, history=history)


# Обработка: создание акции
@app.post("/admin/add-event")
def add_event(
    title: str = Form(...),
    date: str = Form(...),
    location: str = Form("По договорённости")
):
    db = SessionLocal()
    new_event = Event(
        title=title,
        date=date,
        location=location.strip() if location.strip() else "По договорённости"
    )
    db.add(new_event)
    db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


# Обработка: начисление часов
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
  # Обработка: ручное добавление волонтера
@app.post("/admin/add-user")
def add_user_manual(name: str = Form(...), username: str = Form("")):
  db = SessionLocal()
  new_user = User(
      telegram_id=int(time.time()),  # временный ID
      name=name,
      username=username,
      role="volunteer",
  )
  db.add(new_user)
  db.commit()
  db.close()
  return RedirectResponse(url="/admin", status_code=303)
# --- УДАЛЕНИЕ ДАННЫХ (ТОЛЬКО ДЛЯ АДМИНА) ---

@app.post("/admin/delete-event/{event_id}")
def delete_event(event_id: int):
    db = SessionLocal()
    # Находим акцию
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        # Удаляем привязанные к ней записи о начисленных часах
        db.query(HoursLog).filter(HoursLog.event_id == event_id).delete()
        db.delete(event)
        db.commit()
    db.close()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/delete-user/{user_id}")
def delete_user(user_id: int):
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        # Удаляем все записи часов этого волонтёра
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
    total_hours = 0.0
    for log in logs:
        ev = db.query(Event).filter(Event.id == log.event_id).first()
        user_history.append({
            "event_title": ev.title if ev else "Акция штаба",
            "event_date": ev.date if ev else "—",
            "hours": log.hours
        })
        total_hours += log.hours
    
    db.close()
    
    with open("profile.html", "r", encoding="utf-8") as f:
        template = Template(f.read())
        
    return template.render(user=user, total_hours=total_hours, history=user_history)

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
