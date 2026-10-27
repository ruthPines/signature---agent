"""
agent.py - אג'נט החתימות של תיכון אופק.

הרצה:  python agent.py 001           טופס אחד
       python agent.py --all         כל הטפסים, עם טבלת סיכום
       python agent.py 002 --keep-state   (בלי לאפס את מצב החתימות הקודם)

שלושת חלקי האג'נט:
  מוח   - מודל השפה (OpenAI) שמחליט מה הצעד הבא
  כלים  - הפונקציות ב-tools.py
  לולאה - run_agent: מודל בוחר כלי -> מריצים -> מחזירים תוצאה -> חוזרים, עד finish

שכבות בטיחות סביב המוח:
  verify.py - כשהמודל אומר "הושלם", הקוד בודק בעצמו שזה נכון
  audit.py  - כל ריצה מתועדת בדוח ב-reports/
"""

import json
import os
import sys
import time
from datetime import datetime

from audit import write_audit_report
from tools import (FORMS_DIR, TOOL_FUNCTIONS, _normalize_form_id, get_notifications,
                   reset_state, send_notification)
from verify import verify_completion, check_wrong_signers

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
MAX_STEPS = 15              # מגבלת בטיחות: אחרי כמה צעדים עוצרים בכל מקרה
MAX_FINISH_REJECTIONS = 2   # כמה פעמים נותנים למודל לתקן אחרי שהאימות דחה את הסיום
REQUEST_TIMEOUT = 30.0      # שניות לכל בקשה ל-OpenAI, כדי שלא נתקע לנצח
MAX_RUN_SECONDS = 180       # מגבלת זמן כוללת לטיפול בטופס אחד
ESCALATION_CONTACT = "P03"  # מי מקבל הודעה כשהאג'נט מסלים (מנהלת אדמיניסטרטיבית)

SYSTEM_PROMPT = """אתה אג'נט שמנהל תהליך חתימות על טפסים בתיכון.
המטרה שלך: לדאוג שטופס שהגיע יישלח לכל מי שצריך לחתום עליו לפי מדיניות בית הספר,
ושמי שצריך לקבל את הטופס החתום יקבל על כך הודעה.

אתה מחליט בעצמך באיזה סדר להשתמש בכלים. דרך עבודה מומלצת:
1. קרא את הטופס והבן מה סוגו ומה פרטיו (סכום, לינה, סוג ציוד וכו').
2. קבל את מדיניות החתימות, זהה לאיזה form_type היא שייכת, והחל אותה על הטופס, כולל התנאים הנוספים.
3. שלח בקשת חתימה לכל מי שנדרש. אל תבקש חתימה ממי שאינו נדרש.
4. בדוק את סטטוס החתימות והחלט מה לעשות בהתאם לתוצאה.
5. כשכל החתימות הנדרשות התקבלו, שלח הודעה אך ורק למי שמופיע ברשימת notify_after_signed של הכלל המתאים.
   אם הרשימה ריקה — אל תשלח שום הודעה. אל תשלח הודעות יזומות שלא נדרשות במדיניות.
   בהודעה ציין איזה טופס נחתם ועל ידי מי. תמיד מלא את form_id בשליחת הודעה.
6. סיים בקריאה לכלי finish.

שים לב — שני תפקידים שונים לחלוטין, אל תבלבל ביניהם:
- "מנהלת בית הספר" (P01, רונית כהן) — חותמת על טפסים כשנדרש לפי המדיניות (רכש יקר, טיול עם לינה, היעדרות ארוכה)
- "מנהלת אדמיניסטרטיבית" (P03, מיכל אברמוב) — מקבלת הודעה לאחר שהטופס נחתם, או בהסלמה. היא לעולם אינה חותמת על טפסים, אלא אם המדיניות מציינת במפורש את תפקידה כנדרש.

כללים מחייבים:
- לעולם אל תמציא חותם, תפקיד, חוק או פרט שאין בנתונים. השתמש רק במזהי האנשים שקיבלת.
- אם הטופס לא ברור, חסר בו מידע בסיסי, או שאין במדיניות חוק שמתאים לסוגו - אל תנחש.
  שלח הודעה למנהלת האדמיניסטרטיבית (P03) שמסבירה מה הבעיה ובאיזה טופס מדובר,
  ואז קרא ל-finish עם status="escalated" והסבר מה חסר.
- אם חותם נדרש לא חתם: שלח לו הודעת תזכורת אחת (send_notification), ובדוק שוב את הסטטוס.
  אם עדיין לא חתם, שלח הודעה למנהלת האדמיניסטרטיבית (P03) ואז קרא ל-finish עם
  status="escalated" וציין מי לא חתם.
- סיים עם status="completed" רק אם כל החתימות הנדרשות התקבלו וההודעות נשלחו.
  בסיום מוצלח חובה למלא ב-finish את form_type (בדיוק כפי שהוא כתוב במדיניות)
  ואת required_signers (מזהי כל מי שהיה נדרש לחתום). הקוד בודק את ההצהרה שלך מול הטופס והמדיניות,
  כולל התנאים הנוספים (סכום, לינה, סוג ציוד, ימי היעדרות),
  ואם היא לא נכונה - הסיום יידחה ותתבקש לתקן.
- בכל קריאה לכלי מלא את השדה reason: משפט קצר שמסביר למה בחרת בצעד הזה.
"""

# הגדרות הכלים עבור המודל (function calling): (שם, תיאור, פרמטרים, פרמטרים חובה או None=כולם)
_TOOL_SPECS = [
    ("read_form", "קורא את התוכן של טופס.",
     {"form_id": {"type": "string", "description": "מספר הטופס, למשל 001"}}, None),
    ("get_approval_rules", "מחזיר את מדיניות החתימות של בית הספר ואת רשימת אנשי הצוות עם המזהים שלהם.",
     {}, None),
    ("request_signature", "שולח בקשת חתימה לאדם מסוים על טופס.",
     {"form_id": {"type": "string"},
      "signer_id": {"type": "string", "description": "מזהה האדם, למשל P04"}}, None),
    ("check_signature_status", "בודק מי חתם על טופס ומי עדיין ממתין.",
     {"form_id": {"type": "string"}}, None),
    ("send_notification", "שולח הודעה לאדם (תזכורת, הודעה שהטופס נחתם, או דיווח על בעיה).",
     {"recipient_id": {"type": "string", "description": "מזהה הנמען, למשל P03"},
      "message": {"type": "string"},
      "form_id": {"type": "string", "description": "מספר הטופס שההודעה נוגעת אליו"}}, None),
    ("finish", "מסיים את העבודה על הטופס. חובה לקרוא לו בסוף.",
     {"status": {"type": "string", "enum": ["completed", "escalated"],
                 "description": "completed = הכל הושלם; escalated = לא ניתן להשלים, נדרש גורם אנושי"},
      "summary": {"type": "string", "description": "סיכום קצר של מה נעשה ומה המצב"},
      "form_type": {"type": "string",
                    "description": "סוג הטופס בדיוק כפי שהוא מופיע בשדה form_type במדיניות. חובה כש-status=completed"},
      "required_signers": {"type": "array", "items": {"type": "string"},
                           "description": "מזהי כל מי שהיה נדרש לחתום על הטופס. חובה כש-status=completed"}},
     ["status", "summary"]),
]


def _build_tools():
    tools = []
    for name, description, props, required in _TOOL_SPECS:
        properties = dict(props)
        properties["reason"] = {"type": "string", "description": "למה בחרת בצעד הזה"}
        required_keys = list(properties.keys()) if required is None else required + ["reason"]
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": properties,
                               "required": required_keys},
            },
        })
    return tools


TOOLS = _build_tools()


def _execute_tool(name, args):
    """מריץ כלי לפי שם. כל תקלה חוזרת כ-error ולא מפילה את התוכנית."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return {"error": f"כלי לא קיים: {name}"}
    try:
        return func(**args)
    except TypeError as e:
        return {"error": f"פרמטרים לא תקינים לכלי {name}: {e}"}
    except Exception as e:  # רשת ביטחון אחרונה
        return {"error": f"הכלי {name} נכשל: {e}"}


def _record(steps, step, tool, args, reason, result, by="agent"):
    steps.append({"step": step, "time": datetime.now().strftime("%H:%M:%S"), "by": by,
                  "tool": tool, "args": args, "reason": reason, "result": result})


def _ensure_escalation_notice(form_id, summary, steps, step):
    """רשת ביטחון: בהסלמה, מנהלת אדמיניסטרטיבית חייבת לקבל הודעה. אם המודל לא שלח, הקוד שולח."""
    if any(n["recipient_id"] == ESCALATION_CONTACT for n in get_notifications(form_id)):
        return
    message = f"טופס {form_id} דורש טיפול אנושי: {summary}"
    print(f"\n[מערכת] המודל לא דיווח על ההסלמה, שולח דיווח אוטומטי")
    result = send_notification(ESCALATION_CONTACT, message, form_id)
    _record(steps, step, "send_notification",
            {"recipient_id": ESCALATION_CONTACT, "message": message, "form_id": form_id},
            "דיווח אוטומטי על הסלמה", result, by="system")



def run_agent(form_id, client, max_steps=MAX_STEPS):
    """מריץ את האג'נט על טופס אחד. מחזיר מילון עם status, summary, verified, steps, report."""
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps = []
    rejections = 0
    run_started = time.monotonic()

    def end(outcome, last_step):
        outcome["steps"] = last_step
        outcome["report"] = write_audit_report(form_id, outcome, steps, started_at)
        return outcome

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"הגיע טופס חדש מספר {form_id}. "
                                    f"דאג שהוא יגיע לאנשים הנכונים לחתימה, "
                                    f"ושמי שצריך יקבל הודעה כשהכל נחתם."},
    ]

    for step in range(1, max_steps + 1):
        if time.monotonic() - run_started > MAX_RUN_SECONDS:
            return end({"status": "failed", "verified": False,
                        "summary": f"האג'נט חרג ממגבלת הזמן ({MAX_RUN_SECONDS} שניות) ונעצר"}, step - 1)

        # --- המוח מחליט ---
        try:
            response = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto",
            )
        except Exception as e:
            return end({"status": "failed", "verified": False,
                        "summary": f"שגיאת תקשורת עם המודל: {type(e).__name__}: {e}"}, step)

        msg = response.choices[0].message

        # המודל ענה בטקסט בלי לבחור כלי - מזכירים לו איך מסיימים
        if not msg.tool_calls:
            print(f"\n[צעד {step}] המודל ענה בטקסט ללא כלי: {msg.content}")
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user",
                             "content": "המשך בעזרת הכלים. כשתסיים, קרא ל-finish."})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        # --- מפעילים את הכלים שנבחרו ---
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None

            if args is None:
                result = {"error": "הארגומנטים אינם JSON תקין"}
                print(f"\n[צעד {step}] {name} - ארגומנטים פגומים")
                _record(steps, step, name, None, "", result)
            else:
                reason = args.pop("reason", "")
                print(f"\n[צעד {step}] {name}({json.dumps(args, ensure_ascii=False)})")
                if reason:
                    print(f"   נימוק: {reason}")

                if name == "finish":
                    status = args.get("status", "unknown")
                    summary = args.get("summary", "")

                    # --- הסלמה: מוודאים שמישהו אנושי יודע, ובודקים חותמים לא נכונים ---
                    if status != "completed":
                        wrong = check_wrong_signers(form_id)
                        outcome = {"accepted": True}
                        if wrong:
                            print(f"   [אזהרה] חותמים לא נכונים בהסלמה: {wrong}")
                            outcome["warnings"] = wrong
                        _record(steps, step, name, args, reason, outcome)
                        _ensure_escalation_notice(_normalize_form_id(form_id), summary, steps, step)
                        return end({"status": status, "verified": None, "summary": summary,
                                    "warnings": wrong or None}, step)

                    # --- סיום מוצלח: הקוד בודק את הצהרת המודל ---
                    try:
                        issues = verify_completion(form_id, args.get("form_type", ""),
                                                   args.get("required_signers"))
                    except Exception as e:
                        issues = [f"האימות נכשל בגלל תקלה: {type(e).__name__}: {e}"]
                    if not issues:
                        print("   [אימות] עבר בהצלחה")
                        _record(steps, step, name, args, reason, {"accepted": True, "verification": "passed"})
                        return end({"status": "completed", "verified": True, "summary": summary}, step)

                    rejections += 1
                    print(f"   [אימות] נדחה ({rejections}/{MAX_FINISH_REJECTIONS + 1}): {issues}")
                    result = {"error": "הסיום נדחה: האימות מצא אי-התאמות. תקן אותן ונסה שוב, "
                                       "או סיים עם status=escalated אם אי אפשר להשלים.",
                              "issues": issues}
                    _record(steps, step, name, args, reason, result, by="verification")
                    if rejections > MAX_FINISH_REJECTIONS:
                        return end({"status": "unverified", "verified": False,
                                    "summary": f"המודל דיווח על הצלחה אך האימות נכשל: {issues}"}, step)
                else:
                    result = _execute_tool(name, args)
                    _record(steps, step, name, args, reason, result)

            # --- בחינת התוצאה: מחזירים למודל, והוא מחליט מה הלאה ---
            print(f"   תוצאה: {json.dumps(result, ensure_ascii=False)[:300]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # --- תנאי סיום: חרגנו ממספר הצעדים ---
    return end({"status": "failed", "verified": False,
                "summary": f"האג'נט לא סיים תוך {max_steps} צעדים ונעצר מטעמי בטיחות"}, max_steps)


def run_all(client, form_ids):
    """מריץ את האג'נט על כל הטפסים ומדפיס טבלת סיכום."""
    results = []
    for fid in form_ids:
        print(f"\n{'=' * 50}\n=== מתחיל טיפול בטופס {fid} ===")
        results.append((fid, run_agent(fid, client)))

    print("\n" + "=" * 50)
    print("סיכום כל הטפסים")
    print("=" * 50)
    for fid, o in results:
        verified = {True: "אומת", False: "לא אומת", None: "-"}[o.get("verified")]
        print(f"טופס {fid} | {o['status']} | {verified} | {o['steps']} צעדים | {o['summary'][:70]}")
    return results


def _list_form_ids():
    return sorted(p.stem.replace("form_", "") for p in FORMS_DIR.glob("form_*.txt"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run_everything = "--all" in sys.argv
    if not args and not run_everything:
        print("שימוש: python agent.py <מספר טופס> | --all   [--keep-state]")
        sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        print("שגיאה: משתנה הסביבה OPENAI_API_KEY לא מוגדר.")
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(timeout=REQUEST_TIMEOUT, max_retries=2)  # המפתח נקרא ממשתנה הסביבה

    if "--keep-state" not in sys.argv:
        reset_state(None if run_everything else args[0])  # טופס בודד: מאפסים רק אותו

    if run_everything:
        form_ids = _list_form_ids()
        if not form_ids:
            print("לא נמצאו טפסים בתיקיית forms")
            sys.exit(1)
        run_all(client, form_ids)
        return

    form_id = args[0]
    print(f"=== מתחיל טיפול בטופס {form_id} ===")
    outcome = run_agent(form_id, client)
    print("\n" + "=" * 40)
    print(f"סטטוס סופי: {outcome['status']}")
    print(f"סיכום: {outcome['summary']}")
    if outcome.get("report"):
        print(f"דוח ביקורת: {outcome['report']}")


if __name__ == "__main__":
    main()