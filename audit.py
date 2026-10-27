"""
audit.py - דוח ביקורת. כל ריצה על טופס נשמרת בקובץ JSON בתיקיית reports/.
"""

import json
from datetime import datetime

from tools import BASE_DIR, _normalize_form_id, get_notifications, get_signature_records

REPORTS_DIR = BASE_DIR / "reports"


def write_audit_report(form_id, outcome, steps, started_at):
    """כותב דוח ומחזיר את הנתיב שלו (או None אם הכתיבה נכשלה)."""
    fid = _normalize_form_id(form_id)
    report = {
        "form_id": fid,
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "outcome": outcome,
        "signatures": get_signature_records(fid),
        "notifications": get_notifications(fid),
        "steps": steps,
    }
    try:
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / f"audit_{fid}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return str(path.relative_to(BASE_DIR))
    except OSError:
        return None