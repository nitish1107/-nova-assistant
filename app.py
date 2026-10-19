"""Nova: an AI personal assistant. Flask + SQLite backend, serves the web UI too."""
import os
import re
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

USER_NAME = "Nitish"  # shown in the greeting; change it to anything you like
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "assistant.db")
FMT = "%Y-%m-%dT%H:%M"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  priority TEXT DEFAULT 'medium', due TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meetings(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
  with_whom TEXT, starts TEXT NOT NULL, ends TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
  remind_at TEXT NOT NULL, fired INTEGER DEFAULT 0);
"""
TABLES = {
    "tasks": ("title", "priority", "due", "done"),
    "meetings": ("title", "with_whom", "starts", "ends"),
    "reminders": ("text", "remind_at", "fired"),
}
ORDER = {"tasks": "done, due IS NULL, due, id", "meetings": "starts", "reminders": "fired, remind_at"}


# ---------- database helpers ----------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def q(sql, args=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def ex(sql, args=()):
    with db() as con:
        return con.execute(sql, args).lastrowid


def clashes(starts, ends):
    return q("SELECT * FROM meetings WHERE starts < ? AND ends > ?", (ends, starts))


# ---------- natural-language time parsing ----------
TIME = (r"(?:(?:at|by|on|for)\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\bat\s+\d{1,2}(?::\d{2})?\b"
        r"|\bin\s+\d+\s*(?:min\w*|hours?|hrs?|days?)\b|\b(?:tomorrow|today|tonight|next|" + "|".join(DAYS) + r")\b")


def strip_time(text):
    return re.sub(r"\s+", " ", re.sub(TIME, "", text, flags=re.I)).strip(" ,.")


def parse_when(text, now=None):
    now = now or datetime.now().replace(second=0, microsecond=0)
    t = text.lower()
    m = re.search(r"\bin (\d+)\s*(min\w*|hours?|hrs?|days?)", t)
    if m:
        n, unit = int(m[1]), m[2]
        step = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n) if unit.startswith("h") else timedelta(days=n)
        return now + step
    day, named = now.date(), False
    if "tomorrow" in t:
        day, named = day + timedelta(days=1), True
    elif "today" in t or "tonight" in t:
        named = True
    else:
        for i, name in enumerate(DAYS):
            if name in t:
                day, named = day + timedelta(days=(i - now.weekday()) % 7 or 7), True
                break
    tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", t) or re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?()", t)
    if not tm and not named:
        return None
    hour, minute = (20 if "tonight" in t else 9), 0
    if tm:
        hour, minute = int(tm[1]), int(tm[2] or 0)
        if tm[3] == "pm" and hour < 12:
            hour += 12
        elif tm[3] == "am" and hour == 12:
            hour = 0
        elif tm[3] == "" and hour < 8:
            hour += 12  # "at 5" means 5 pm
    when = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
    if when <= now and not named:
        when += timedelta(days=1)
    return when


def nice(d):
    return d.strftime("%a %d %b, %I:%M %p")


# ---------- the assistant brain ----------
def summary():
    today = datetime.now().strftime("%Y-%m-%d")
    tasks = q("SELECT id,title,priority FROM tasks WHERE done=0 ORDER BY due IS NULL, due LIMIT 8")
    meets = q("SELECT title,starts FROM meetings WHERE starts LIKE ? ORDER BY starts", (today + "%",))
    lines = ["Today's meetings:"] + ([f"  {m['starts'][11:]}  {m['title']}" for m in meets] or ["  none"])
    lines += ["Open tasks:"] + ([f"  #{t['id']} {t['title']} ({t['priority']})" for t in tasks] or ["  all clear"])
    return "\n".join(lines)


def ask_ai(msg):
    """Works with any OpenAI-compatible service: Ollama (local), Groq, Gemini, OpenRouter..."""
    base, model, key = os.getenv("AI_BASE_URL"), os.getenv("AI_MODEL"), os.getenv("AI_API_KEY", "none")
    if not (base and model):
        return ("I can add tasks, set reminders and schedule meetings. Try: \u201cremind me to drink water in 30 minutes\u201d. "
                "Fill in the .env file to unlock open-ended AI chat.")
    try:
        from openai import OpenAI

        reply = OpenAI(base_url=base, api_key=key).chat.completions.create(
            model=model,
            max_tokens=1500,  # room for models that think before answering
            messages=[
                {"role": "system", "content": f"You are Nova, a concise, friendly personal assistant. Now: {nice(datetime.now())}.\n{summary()}"},
                {"role": "user", "content": msg},
            ],
        )
        return reply.choices[0].message.content or "I couldn't come up with a reply. Try rephrasing."
    except Exception as e:  # service down, wrong key or model name
        return f"The AI is unavailable right now ({type(e).__name__}). Check your .env settings."


def run_chat(msg):
    low = msg.lower().strip()
    m = re.search(r"\b(?:done|complete|finish)\w*\s+(?:task\s+)?#?(\d+)", low)
    if m:
        ex("UPDATE tasks SET done=1 WHERE id=?", (m[1],))
        return f"Marked task #{m[1]} as done."
    when = parse_when(msg)
    if re.match(r"(remind|reminder)", low):
        text = strip_time(re.sub(r"^(reminder|remind me)( to| about)?\s*", "", msg, flags=re.I)) or "Reminder"
        if not when:
            return "When should I remind you? Try: remind me to call mom at 6pm."
        ex("INSERT INTO reminders(text,remind_at) VALUES(?,?)", (text, when.strftime(FMT)))
        return f"Reminder set for {nice(when)}: {text}."
    if re.match(r"(schedule|book|set up)", low):
        if not when:
            return "What time? Try: schedule a meeting with Riya tomorrow at 3pm."
        title = strip_time(re.sub(r"^(schedule|book|set up)( a| an)?\s*", "", msg, flags=re.I)) or "Meeting"
        who = re.search(r"\bwith\s+([A-Za-z][\w ]*)", title)
        end = when + timedelta(hours=1)
        clash = clashes(when.strftime(FMT), end.strftime(FMT))
        if clash:
            return f"That overlaps with “{clash[0]['title']}” at {clash[0]['starts'][11:]}. Pick another time."
        ex("INSERT INTO meetings(title,with_whom,starts,ends) VALUES(?,?,?,?)",
           (title, who[1] if who else None, when.strftime(FMT), end.strftime(FMT)))
        return f"Scheduled “{title}” for {nice(when)} (1 hour)."
    if re.match(r"(add|new|create)\s+(a\s+)?(task|todo|to-do)|(task|todo)\b", low):
        title = re.sub(r"^(add|new|create)?\s*(a )?(task|todo|to-do)( to| for)?:?\s*", "", msg, flags=re.I)
        high = bool(re.search(r"\b(urgent|high priority|important)\b", low))
        title = strip_time(re.sub(r"\b(urgent|high priority|important)\b", "", title, flags=re.I)) or "Untitled task"
        ex("INSERT INTO tasks(title,priority,due) VALUES(?,?,?)",
           (title, "high" if high else "medium", when.strftime(FMT) if when else None))
        return f"Added task “{title}”" + (f", due {nice(when)}." if when else ".")
    if re.search(r"\b(tasks|to-do|todo list|agenda|what'?s on|today)\b", low):
        return summary()
    return ask_ai(msg)


# ---------- routes ----------
@app.get("/")
def index():
    for folder in (app.static_folder, HERE):  # also accept index.html placed next to app.py
        if os.path.exists(os.path.join(folder, "index.html")):
            return send_from_directory(folder, "index.html")
    return ("index.html was not found. Put it in a folder named 'static' next to app.py "
            f"(looked in: {app.static_folder})"), 404


@app.get("/api/me")
def me():
    return jsonify(name=USER_NAME)


@app.get("/api/due")
def due():
    rows = q("SELECT * FROM reminders WHERE fired=0 AND remind_at<=?", (datetime.now().strftime(FMT),))
    for r in rows:
        ex("UPDATE reminders SET fired=1 WHERE id=?", (r["id"],))
    return jsonify(rows)


@app.post("/api/chat")
def chat():
    return jsonify(reply=run_chat((request.get_json(force=True).get("message") or "").strip()))


@app.get("/api/<t>")
def list_items(t):
    if t not in TABLES:
        return jsonify(error="Unknown resource"), 404
    return jsonify(q(f"SELECT * FROM {t} ORDER BY {ORDER[t]}"))


@app.post("/api/<t>")
def create(t):
    if t not in TABLES:
        return jsonify(error="Unknown resource"), 404
    d = request.get_json(force=True)
    try:
        if t == "meetings":
            d["ends"] = d.get("ends") or (datetime.strptime(d["starts"], FMT) + timedelta(hours=1)).strftime(FMT)
            if d["ends"] <= d["starts"]:
                return jsonify(error="End time must be after the start time."), 400
            clash = clashes(d["starts"], d["ends"])
            if clash:
                return jsonify(error=f"Overlaps with “{clash[0]['title']}” at {clash[0]['starts'][11:]}."), 409
        cols = [c for c in TABLES[t] if c in d]
        new_id = ex(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [d[c] for c in cols])
    except (KeyError, ValueError, sqlite3.Error):
        return jsonify(error="Please fill in the required fields."), 400
    return jsonify(id=new_id), 201


@app.patch("/api/<t>/<int:i>")
def update(t, i):
    if t not in TABLES:
        return jsonify(error="Unknown resource"), 404
    d = request.get_json(force=True)
    cols = [c for c in TABLES[t] if c in d]
    if cols:
        ex(f"UPDATE {t} SET {','.join(c + '=?' for c in cols)} WHERE id=?", [d[c] for c in cols] + [i])
    return jsonify(ok=True)


@app.delete("/api/<t>/<int:i>")
def remove(t, i):
    if t not in TABLES:
        return jsonify(error="Unknown resource"), 404
    ex(f"DELETE FROM {t} WHERE id=?", (i,))
    return jsonify(ok=True)


with db() as _con:
    _con.executescript(SCHEMA)

if __name__ == "__main__":
    print("Nova is running at http://127.0.0.1:5000")
    app.run(debug=True)
