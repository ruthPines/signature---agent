"""
tools.py - הכלים של אג'נט החתימות.

כל כלי הוא פונקציה פשוטה שמחזירה מילון (dict).
שגיאות לא זורקות חריגה - הן חוזרות כ-{"error": "..."} כדי שהאג'נט יראה אותן ויחליט מה לעשות.

מצב העבודה (חתימות והודעות) נשמר בקובץ נפרד לכל טופס: state/form_<מספר>.json.
  - טפסים לא חולקים מצב, ואיפוס של טופס אחד לא נוגע באחרים.
  - כל קריאה וכתיבה מוגנות בנעילה (קובץ נעילה + נעילת threads), כך שאין race condition
    גם כשכמה תהליכים או threads עובדים במקביל.
  - הכתיבה אטומית (כותבים לקובץ זמני ואז מחליפים), כך שקריסה באמצע לא משאירה JSON חצוי.
"""

import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).parent
FORMS_DIR = BASE_DIR / "forms"
STATE_DIR = BASE_DIR / "state"
NOTIFICATIONS_LOG = STATE_DIR / "notifications.log"

LOCK_TIMEOUT = 5.0        # שניות להמתנה לנעילה לפני שמוותרים
LOCK_STALE_AFTER = 30.0   # נעילה ישנה מזה נחשבת תקועה (למשל אחרי קריסה) ומוסרת

# הדמיית התנהגות של בני אדם בהדגמה: (מספר טופס, מזהה חותם) -> "no_response"
# חותם כזה לא יחתום, כדי שנראה איך האג'נט מתמודד עם חתימה שלא מגיעה.
DEMO_BEHAVIOR = {
    ("002", "P04"): "no_response",
}

_THREAD_LOCK = threading.Lock()


# ---------- טעינת הגדרות (נטענות פעם אחת ונשמרות בזיכרון) ----------

def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_people_by_id():
    """כל אנשי הצוות לפי מזהה. נטען מהקובץ פעם אחת. אין לשנות את התוצאה."""
    return {p["id"]: p for p in _load_json(BASE_DIR / "signers.json")["people"]}


@lru_cache(maxsize=1)
def get_rules():
    """מדיניות החתימות. נטענת מהקובץ פעם אחת. אין לשנות את התוצאה."""
    return _load_json(BASE_DIR / "approval_rules.json")["rules"]


def clear_config_cache():
    """אם שינית את signers.json או approval_rules.json בזמן שהתוכנית רצה."""
    get_people_by_id.cache_clear()
    get_rules.cache_clear()


def _get_person(person_id):
    return get_people_by_id().get(person_id)


# ---------- מצב עבודה: קובץ לכל טופס, עם נעילה וכתיבה אטומית ----------

def _normalize_form_id(form_id):
    """מקבל '001', 'form_001' או 'form_001.txt' ומחזיר '001'."""
    return str(form_id).strip().lower().replace(".txt", "").replace("form_", "")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _state_path(fid):
    return STATE_DIR / f"form_{fid}.json"


@contextmanager
def _form_lock(fid):
    """נעילה בלעדית על המצב של טופס אחד. עובדת בין threads ובין תהליכים, ב-Windows וב-Linux."""
    STATE_DIR.mkdir(exist_ok=True)
    lock_path = STATE_DIR / f"form_{fid}.lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    with _THREAD_LOCK:
        while True:
            try:
                os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > LOCK_STALE_AFTER:
                        lock_path.unlink(missing_ok=True)  # נעילה תקועה
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"לא הצלחתי לנעול את המצב של טופס {fid}")
                time.sleep(0.02)
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)


def _load_form_state(fid):
    path = _state_path(fid)
    data = _load_json(path) if path.exists() else {}
    data.setdefault("signatures", {})
    data.setdefault("notifications", [])
    return data


def _write_json_atomic(path, data):
    """כותב לקובץ זמני ואז מחליף, כך שהקובץ המקורי אף פעם לא נשאר חצוי."""
    STATE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def reset_state(form_id=None):
    """מנקה מצב. בלי פרמטר - הכל. עם מספר טופס - רק הטופס הזה, ושאר הטפסים לא נפגעים."""
    if form_id is None:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        return
    fid = _normalize_form_id(form_id)
    _state_path(fid).unlink(missing_ok=True)
    (STATE_DIR / f"form_{fid}.lock").unlink(missing_ok=True)


# ---------- הכלים של האג'נט ----------

def read_form(form_id):
    """קורא את התוכן הגולמי של טופס."""
    fid = _normalize_form_id(form_id)
    path = FORMS_DIR / f"form_{fid}.txt"
    if not path.exists():
        return {"error": f"טופס {fid} לא נמצא"}
    try:
        return {"form_id": fid, "content": path.read_text(encoding="utf-8")}
    except OSError as e:
        return {"error": f"שגיאה בקריאת הטופס: {e}"}


def get_approval_rules():
    """מחזיר את מדיניות החתימות של הארגון ואת רשימת אנשי הצוות."""
    try:
        return {"rules": get_rules(), "people": list(get_people_by_id().values())}
    except (OSError, json.JSONDecodeError, KeyError) as e:
        return {"error": f"שגיאה בטעינת המדיניות: {e}"}


def request_signature(form_id, signer_id):
    """שולח בקשת חתימה לאדם מסוים על טופס מסוים."""
    fid = _normalize_form_id(form_id)
    if not (FORMS_DIR / f"form_{fid}.txt").exists():
        return {"error": f"טופס {fid} לא נמצא"}
    person = _get_person(signer_id)
    if person is None:
        return {"error": f"מזהה חותם לא קיים: {signer_id}"}

    try:
        with _form_lock(fid):
            data = _load_form_state(fid)
            if signer_id in data["signatures"]:
                return {"error": f"כבר נשלחה בקשה ל-{person['name']} על טופס {fid} "
                                 f"(סטטוס: {data['signatures'][signer_id]['status']})"}
            data["signatures"][signer_id] = {"status": "pending", "requested_at": _now(),
                                             "signed_at": None}
            _write_json_atomic(_state_path(fid), data)
    except (TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"error": f"שגיאה בשמירת מצב החתימות: {e}"}

    print(f"   [מערכת] נשלחה בקשת חתימה ל-{person['name']} ({person['role']}) על טופס {fid}")
    return {"ok": True, "message": f"בקשת חתימה נשלחה ל-{person['name']} ({person['role']})"}


def check_signature_status(form_id):
    """בודק מי חתם ומי עדיין ממתין. בהדגמה, חותמים מדומים 'חותמים' בבדיקה הראשונה."""
    fid = _normalize_form_id(form_id)
    try:
        with _form_lock(fid):
            data = _load_form_state(fid)
            if not data["signatures"]:
                return {"error": f"לא נשלחו בקשות חתימה על טופס {fid}"}

            # הדמיית בני אדם: מי שממתין ולא מוגדר כ"לא מגיב" חותם
            for signer_id, info in data["signatures"].items():
                if info["status"] == "pending" and DEMO_BEHAVIOR.get((fid, signer_id)) != "no_response":
                    info["status"] = "signed"
                    info["signed_at"] = _now()
            _write_json_atomic(_state_path(fid), data)
            records = dict(data["signatures"])
    except (TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"error": f"שגיאה בקריאת מצב החתימות: {e}"}

    signatures = []
    for signer_id, info in records.items():
        person = _get_person(signer_id)
        signatures.append({
            "signer_id": signer_id,
            "name": person["name"] if person else signer_id,
            "role": person["role"] if person else "?",
            "status": info["status"],
        })
    return {"form_id": fid, "signatures": signatures}


def send_notification(recipient_id, message, form_id=""):
    """שולח הודעה לאדם (בהדגמה: מודפסת למסך, נכתבת ליומן ונשמרת במצב הטופס)."""
    person = _get_person(recipient_id)
    if person is None:
        return {"error": f"מזהה נמען לא קיים: {recipient_id}"}
    fid = _normalize_form_id(form_id) if form_id else "general"
    line = f"[{_now()}] אל: {person['name']} <{person['email']}> | {message}"
    try:
        with _form_lock(fid):
            data = _load_form_state(fid)
            data["notifications"].append({
                "time": _now(), "form_id": "" if fid == "general" else fid,
                "recipient_id": recipient_id, "recipient_name": person["name"],
                "message": message,
            })
            _write_json_atomic(_state_path(fid), data)
            with open(NOTIFICATIONS_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"error": f"שגיאה בשליחת ההודעה: {e}"}
    print(f"   [הודעה] {line}")
    return {"ok": True, "message": f"הודעה נשלחה ל-{person['name']}"}


# ---------- פונקציות קריאה (לא כלים של האג'נט; משמשות לאימות ולדוח) ----------

def get_signature_records(form_id):
    """מחזיר {signer_id: {status, requested_at, signed_at}} עבור טופס."""
    fid = _normalize_form_id(form_id)
    with _form_lock(fid):
        return _load_form_state(fid)["signatures"]


def get_notifications(form_id):
    """מחזיר את רשימת ההודעות שנשלחו בנוגע לטופס."""
    fid = _normalize_form_id(form_id)
    with _form_lock(fid):
        return _load_form_state(fid)["notifications"]


# מילון שהלולאה תשתמש בו כדי להפעיל כלי לפי שמו
TOOL_FUNCTIONS = {
    "read_form": read_form,
    "get_approval_rules": get_approval_rules,
    "request_signature": request_signature,
    "check_signature_status": check_signature_status,
    "send_notification": send_notification,
}


if __name__ == "__main__":
    # בדיקה עצמית מהירה של הכלים (ללא מודל)
    reset_state("003")
    print("1. קריאת טופס 003:")
    print(read_form("003")["content"])
    print("2. טופס שלא קיים:", read_form("999"))
    print("3. בקשת חתימה:", request_signature("003", "P04"))
    print("4. בקשה כפולה:", request_signature("003", "P04"))
    print("5. חותם שלא קיים:", request_signature("003", "P99"))
    print("6. סטטוס:", check_signature_status("003"))
    print("7. הודעה:", send_notification("P03", "טופס 003 נחתם", "003"))
    reset_state("003")