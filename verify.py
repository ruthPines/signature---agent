"""
verify.py - שכבת אימות דטרמיניסטית.

המודל יכול לטעות, ולכן כשהוא מצהיר "הושלם" הקוד בודק בעצמו (בלי מודל) שזה נכון:
  1. סוג הטופס שהוצהר תואם לשם הטופס.
  2. הקוד קורא את עובדות הטופס (סכום, לינה, סוג ציוד, ימי היעדרות) ומעריך בעצמו
     את התנאים הנוספים שבמדיניות, כדי לדעת מי חייב לחתום ומי לא.
  3. כל מי שחייב לחתום - חתם. אף אחד שלא נדרש - לא התבקש.
  4. ההודעות שהמדיניות מחייבת נשלחו.

אם עובדה לא ניתנת לחילוץ מהטופס (למשל חסרה שורת "לינה"), התנאי שתלוי בה נחשב לא-ידוע:
הקוד לא דורש את החותם הנוסף ולא פוסל אותו, והבדיקה נשארת בידי המודל.
"""

import operator
import re

from tools import (get_people_by_id, get_rules, get_notifications, get_signature_records,
                   read_form, _normalize_form_id)

ESCALATION_CONTACT = "P03"  # מנהלת אדמיניסטרטיבית — תמיד מקבלת הודעה בהסלמה, לא נחשבת עודפת

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}

COMPUTER_KEYWORDS = ("מחשב", "לפטופ", "טאבלט", "נתב", "ציוד תקשורת", "רשת", "שרת")


def extract_facts(text):
    """מחלץ מהטופס עובדות מובנות. עובדה שלא נמצאה מקבלת None."""
    facts = {"amount": None, "overnight": None, "equipment_category": None, "absence_days": None}

    m = re.search(r"(?:עלות כוללת|סכום כולל|סכום)\s*:\s*(\d[\d,\.]*)", text)
    if m:
        try:
            facts["amount"] = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            pass

    m = re.search(r"לינה\s*:\s*(כן|לא)", text)
    if m:
        facts["overnight"] = (m.group(1) == "כן")

    m = re.search(r"פריט\s*:\s*(.+)", text)
    if m:
        facts["equipment_category"] = ("computers_or_communication"
                                       if any(k in m.group(1) for k in COMPUTER_KEYWORDS) else "other")

    m = re.search(r"(\d+)\s*ימים", text) or re.search(r"ימים\s*:\s*(\d+)", text)
    if m:
        facts["absence_days"] = int(m.group(1))

    return facts


def evaluate_requirements(rule, facts):
    """
    מחזיר (תפקידים שחובה שיחתמו, תפקידים לא-ידועים, תנאים שהתקיימו).
    תפקיד לא-ידוע = תלוי בתנאי שאי אפשר להעריך מהטופס.
    """
    required = set(rule["required_roles"])
    unknown, applied = set(), []
    for cond in rule.get("extra_conditions", []):
        role = cond["also_requires"]
        value = facts.get(cond.get("field"))
        if value is None:
            unknown.add(role)
        elif _OPS[cond["op"]](value, cond["value"]):
            required.add(role)
            applied.append(cond["if"])
    return required, unknown - required, applied


def check_wrong_signers(form_id):
    """
    בודק אם נשלחו בקשות חתימה לאנשים שאינם רלוונטיים לטופס.
    מחזיר רשימת אזהרות (מחרוזות). רשימה ריקה = הכל תקין.
    נקרא גם בהסלמה, לא רק בסיום מוצלח.
    """
    fid = _normalize_form_id(form_id)
    records = get_signature_records(fid)
    if not records:
        return []

    people = get_people_by_id()
    form = read_form(fid)
    if "error" in form:
        return []

    # כל התפקידים שמופיעים בכלשהו מהכללים (required + extra_conditions)
    all_known_roles = set()
    for rule in get_rules():
        all_known_roles.update(rule["required_roles"])
        for cond in rule.get("extra_conditions", []):
            all_known_roles.add(cond["also_requires"])

    warnings = []
    for sid in records:
        person = people.get(sid)
        if person and person["role"] not in all_known_roles:
            warnings.append(
                f"{person['name']} ({person['role']}) אינו מוכר כחותם אפשרי בשום כלל במדיניות"
            )
    return warnings


def verify_completion(form_id, form_type, required_signers):
    """מחזיר רשימת בעיות. רשימה ריקה = האימות עבר."""
    fid = _normalize_form_id(form_id)
    required_signers = required_signers or []
    people = get_people_by_id()

    rule = next((r for r in get_rules() if r["form_type"] == form_type), None)
    if rule is None:
        known = ", ".join(r["form_type"] for r in get_rules())
        return [f"סוג הטופס '{form_type}' לא קיים במדיניות. הסוגים הקיימים: {known}"]

    form = read_form(fid)
    if "error" in form:
        return [f"לא ניתן לקרוא את הטופס לאימות: {form['error']}"]
    text = form["content"]
    issues = []

    # 1. סוג הטופס שהוצהר תואם לשם הטופס
    name_match = re.search(r"שם הטופס\s*:\s*(.+)", text)
    keywords = rule.get("keywords", [])
    if name_match and keywords and not any(k in name_match.group(1) for k in keywords):
        issues.append(f"סוג הטופס שהוצהר ('{form_type}') לא תואם לשם הטופס "
                      f"('{name_match.group(1).strip()}')")

    # 2. מי חייב לחתום, לפי עובדות הטופס והתנאים שבמדיניות
    facts = extract_facts(text)
    required_roles, unknown_roles, _ = evaluate_requirements(rule, facts)

    records = get_signature_records(fid)
    if not records:
        issues.append("לא נשלחה אף בקשת חתימה על הטופס")

    signed_roles = {people[s]["role"] for s, r in records.items()
                    if r["status"] == "signed" and s in people}
    requested_roles = {people[s]["role"] for s in records if s in people}

    for role in sorted(required_roles - signed_roles):
        if role in requested_roles:
            continue  # נשלחה בקשה אך טרם חתם - יטופל למטה
        issues.append(f"התפקיד '{role}' נדרש לפי המדיניות ועובדות הטופס, אך לא נשלחה לו בקשה")

    # 3. אסור לבקש חתימה מתפקיד שלא נדרש; וכל בקשה חייבת להסתיים בחתימה
    allowed_roles = required_roles | unknown_roles
    for sid, record in records.items():
        person = people.get(sid)
        if person and person["role"] not in allowed_roles:
            issues.append(f"נשלחה בקשה ל-{person['name']} ({person['role']}), "
                          f"תפקיד שאינו נדרש לטופס הזה לפי המדיניות ועובדות הטופס")
        if record["status"] != "signed":
            issues.append(f"{person['name'] if person else sid} טרם חתם")

    # הצהרת המודל עצמה חייבת להיות עקבית
    for sid in required_signers:
        if sid not in people:
            issues.append(f"מזהה חותם לא קיים בהצהרה: {sid}")
        elif sid not in records:
            issues.append(f"{people[sid]['name']} הוצהר כחותם נדרש אך לא נשלחה לו בקשה")

    # 4. הודעות חובה לאחר חתימה
    notified = {n["recipient_id"] for n in get_notifications(fid)}
    for role in rule.get("notify_after_signed", []):
        for person in people.values():
            if person["role"] == role and person["id"] not in notified:
                issues.append(f"לא נשלחה הודעה על הטופס החתום ל-{person['name']} ({role})")

    # 5. הודעות עודפות — נשלחה הודעה למי שהמדיניות לא מחייבת
    required_notify_ids = {
        person["id"]
        for role in rule.get("notify_after_signed", [])
        for person in people.values()
        if person["role"] == role
    }
    # P03 תמיד מקבלת הודעה בהסלמה — לא נחשב עודף
    for n in get_notifications(fid):
        rid = n["recipient_id"]
        if rid not in required_notify_ids and rid != ESCALATION_CONTACT:
            person = people.get(rid)
            name = person["name"] if person else rid
            issues.append(f"נשלחה הודעה ל-{name} אך המדיניות לא מחייבת זאת לסוג טופס זה")
            break  # מספיק אזהרה אחת

    return issues