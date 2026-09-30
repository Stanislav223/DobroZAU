import os
import time
import hashlib
import hmac
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
from sqlalchemy import Column, Float, ForeignKey, Integer, String, create_engine
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
  id = Column(Integer, primary_key=True)
  telegram_id = Column(Integer, unique=True, index=True)
  name = Column(String)
  username = Column(String)
  role = Column(String, default="volunteer")  # 'admin' или 'volunteer'


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

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- ГЛАВНАЯ СТРАНИЦА: РЕЙТИНГ ---
@app.get("/", response_class=HTMLResponse)
def home():
    db = SessionLocal()
    
    # 1. Загружаем мероприятия
    events = db.query(Event).all()

    # 2. Собираем волонтёров и их отработанные часы
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

    # Сортируем рейтинг: сначала те, у кого больше всего часов
    leaderboard.sort(key=lambda x: x["hours"], reverse=True)

    with open("index.html", "r", encoding="utf-8") as f:
        template = Template(f.read())
    
    # Передаём И мероприятия, И рейтинг волонтеров
    return template.render(events=events, leaderboard=leaderboard)

import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, "12345")  # Укажи свой пароль
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

  with open("admin.html", "r", encoding="utf-8") as f:
    template = Template(f.read())

  db.close()
  return template.render(events=events, users=users)


# Обработка: создание акции
@app.post("/admin/add-event")
def add_event(title: str = Form(...), date: str = Form(...)):
  db = SessionLocal()
  new_event = Event(title=title, date=date)
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
