import os
import sys
import json
import argparse
from pathlib import Path

import requests


# --- verified subject endpoints ---
AUTH_PATH = "Account/Authenticate"
TERMS_PATH = "SubjectApplication/Terms"
CURRICULUM_PATH = "SubjectApplication/Curriculum"
SCHEDULABLE_PATH = "SubjectApplication/SchedulableSubjects"
COURSES_PATH = "SubjectApplication/GetSubjectsCourses"
REGISTER_PATH = "SubjectApplication/SubjectSignin"
COURSECHANGE_PATH = "SubjectApplication/CourseChange"

# --- exam endpoints ---
EXAMS_LIST_PATH = "ExamRegistration/GetExamsList"
EXAM_REGISTER_PATH = "ExamRegistration/SaveExamRegistration"


# ---------------------------------------------------------------------------
# console output
# ---------------------------------------------------------------------------

VERBOSE = False


def out(msg=""):
    print(msg)


def hr(char="="):
    print(char * 60)


def debug(msg):
    if VERBOSE:
        print(f"  [debug] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def notify(webhook, message):
    """Best-effort success notification. Never raises."""
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": message}, timeout=10)
        debug(f"Discord notified: {message}")
    except Exception as e:
        debug(f"Discord notify failed (ignored): {e}")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class NeptunClient:
    def __init__(self, neptun_id, password, base_url=None, token_cache_path=".neptun-token.json"):
        self.neptun_id = neptun_id
        self.password = password
        self.base_url = base_url
        self.session = requests.Session()
        self.token = None
        self._token_cache_path = token_cache_path

    def _save_token(self):
        if self.token:
            Path(self._token_cache_path).write_text(json.dumps({"token": self.token}), encoding="utf-8")

    def _load_token(self):
        p = Path(self._token_cache_path)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                token = data.get("token")
                if token:
                    self.token = token
                    self.session.headers.update({
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    })
                    debug("Loaded cached token")
                    return True
            except Exception as e:
                debug(f"Failed to load cached token: {e}")
        return False

    def _force_refresh(self):
        """Delete cached token and re-authenticate against the server."""
        try:
            Path(self._token_cache_path).unlink(missing_ok=True)
        except Exception:
            pass
        self.session.headers.pop("Authorization", None)
        self.token = None
        self.authenticate()

    def _url(self, path):
        return f"{self.base_url}/{path}"

    def authenticate(self):
        if self._load_token():
            return True
        payload = {"userName": self.neptun_id, "password": self.password}
        resp = self.session.post(self._url(AUTH_PATH), json=payload,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
        if resp.status_code != 200:
            debug(f"Auth status {resp.status_code}: {resp.text}")
            return False
        try:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                self.token = data["data"].get("accessToken")
            elif isinstance(data, dict):
                self.token = data.get("accessToken") or data.get("access_token") or data.get("token")
            else:
                self.token = data
        except json.JSONDecodeError:
            self.token = resp.text.replace('"', "")
        if not self.token:
            return False
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._save_token()
        return True

    def _get(self, path, params=None):
        resp = self.session.get(self._url(path), params=params)
        if resp.status_code == 401:
            debug("Token expired, forcing re-auth...")
            self._force_refresh()
            resp = self.session.get(self._url(path), params=params)
        if resp.status_code == 401:
            debug("Still 401 after re-auth — credentials may have changed.")
            return None
        if resp.status_code != 200:
            debug(f"GET {path} status {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            debug(f"GET {path} non-JSON: {resp.text[:200]}")
            return None

    def _post(self, path, payload):
        resp = self.session.post(self._url(path), json=payload)
        if resp.status_code == 401:
            debug("Token expired, forcing re-auth...")
            self._force_refresh()
            resp = self.session.post(self._url(path), json=payload)
        if resp.status_code == 401:
            debug("Still 401 after re-auth — credentials may have changed.")
            return None
        debug(f"POST {path} -> {resp.status_code} {resp.text[:300]}")
        if resp.status_code not in (200, 201):
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text}

    # --- subjects ---
    def get_terms(self):
        return self._get(TERMS_PATH)

    def get_curricula(self, subject_type, term_value):
        return self._get(CURRICULUM_PATH, {"subjectType": subject_type, "termId": term_value})

    def get_schedulable_subjects(self, subject_type, term_value, curriculum_value):
        return self._get(SCHEDULABLE_PATH, {
            "request.curriculumTemplateId": curriculum_value,
            "request.hasCompletedSubjects": "false",
            "request.subjectType": subject_type,
            "request.termId": term_value,
            "request.hasRegisteredSubjects": "true",
            "request.hasScheduledSubjects": "false",
            "sortAndPage.firstRow": 0,
            "sortAndPage.lastRow": 1000,
            "sortAndPage.title": "asc",
        })

    def get_subject_courses(self, subject_row):
        params = {
            "subjectId": subject_row.get("id"),
            "termId": subject_row.get("termId"),
            "curriculumTemplateId": subject_row.get("curriculumTemplateId"),
        }
        if subject_row.get("curriculumTemplateLineId"):
            params["curriculumTemplateLineId"] = subject_row["curriculumTemplateLineId"]
        return self._get(COURSES_PATH, params)

    def register_subject(self, subject_row, course_ids):
        return self._post(REGISTER_PATH, {
            "courseIds": course_ids,
            "curriculumTemplateId": subject_row.get("curriculumTemplateId"),
            "curriculumTemplateLineId": subject_row.get("curriculumTemplateLineId"),
            "subjectId": subject_row.get("id"),
            "termId": subject_row.get("termId"),
        })

    def change_course(self, index_line_id, requested_course_id):
        return self._post(COURSECHANGE_PATH, {
            "indexLineId": index_line_id,
            "requestedCourseId": requested_course_id,
        })

    # --- exams ---
    def get_exams(self):
        return self._get(EXAMS_LIST_PATH,
                         {"sortAndPage.firstRow": 0, "sortAndPage.lastRow": 9999})

    def register_exam(self, exam):
        return self._post(EXAM_REGISTER_PATH, {"examId": exam.get("id")})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def rows(payload):
    if isinstance(payload, dict):
        return payload.get("data") or []
    if isinstance(payload, list):
        return payload
    return []


def seats_open(course):
    if course.get("isFull") is True:
        return False
    mx = course.get("maxLimit") or 0
    reg = course.get("registeredStudentsCount") or 0
    return reg < mx if mx else True


def seat_str(course):
    s = f"{course.get('registeredStudentsCount', 0)}/{course.get('maxLimit', '?')}"
    if course.get("isFull"):
        s += " FULL"
    if course.get("waitingStudentsCount"):
        s += f" +{course['waitingStudentsCount']} waiting"
    return s


def pick_lecture(courses, prefix):
    """EA lecture matching `prefix`, excluding longer-suffixed variants.
    'SzGH_EA' matches 'SzGH_EA' but NOT 'SzGH_EA_SLG'.
    """
    exact = [c for c in courses if (c.get("code") or "") == prefix]
    if exact:
        return exact[0]
    candidates = [c for c in courses
                  if (c.get("code") or "").startswith(prefix)
                  and not (c.get("code") or "")[len(prefix):].startswith("_")]
    if len(candidates) > 1:
        debug(f"Lecture prefix '{prefix}' matched {len(candidates)}; using {candidates[0].get('code')}")
    return candidates[0] if candidates else None


def find_course(courses, code):
    return next((c for c in courses if (c.get("code") or "") == code), None)


# ---------------------------------------------------------------------------
# subject handling
# ---------------------------------------------------------------------------

def build_subject_index(client, term_value):
    index = {}
    for st in (1, 2):
        for cur in rows(client.get_curricula(st, term_value)):
            for s in rows(client.get_schedulable_subjects(st, term_value, cur.get("value"))):
                code = s.get("code")
                if code and code not in index:
                    index[code] = s
    debug(f"Indexed {len(index)} schedulable subjects")
    return index


def process_subject(client, idx, total, cfg, index, webhook):
    code = cfg.get("neptun_code")
    lecture_prefix = cfg.get("lecture")
    prefs = cfg.get("courses", [])

    row = index.get(code)
    out()
    out(f"[{idx}/{total}] {row.get('title') if row else '?'} ({code})")

    if not row:
        out("      ! Not schedulable at this time. Skipping.")
        return
    courses = rows(client.get_subject_courses(row))
    if not courses:
        out("      ! No courses returned. Skipping.")
        return

    lecture = pick_lecture(courses, lecture_prefix) if lecture_prefix else None
    if lecture_prefix:
        if not lecture:
            out(f"      ! No lecture matching '{lecture_prefix}'. Skipping.")
            return
        out(f"      Lecture: {lecture.get('code')} [{lecture.get('type')}]  seats {seat_str(lecture)}")

    pref_courses = []
    out("      Course priority:")
    for rank, pref in enumerate(prefs, 1):
        c = find_course(courses, pref)
        pref_courses.append((pref, c))
        if c:
            tag = "   <- enrolled" if c.get("isSigned") else ""
            out(f"        {rank}. {pref:<16} seats {seat_str(c)}{tag}")
        else:
            out(f"        {rank}. {pref:<16} (not offered)")

    if row.get("isRegistered"):
        _switch_if_better(client, code, row, courses, pref_courses, webhook)
    else:
        _register_subject(client, code, row, lecture, pref_courses, webhook)


def _register_subject(client, code, row, lecture, pref_courses, webhook):
    out("      Status: NOT REGISTERED")
    if lecture is not None and not seats_open(lecture):
        out(f"      x Lecture {lecture.get('code')} is FULL.")
        return
    chosen = next((c for _, c in pref_courses if c and seats_open(c)), None)
    if not chosen:
        out("      x No priority course has open seats.")
        return

    course_ids = ([lecture.get("id")] if lecture is not None else []) + [chosen.get("id")]
    out(f"      -> Registering course {chosen.get('code')}")
    if client.register_subject(row, course_ids) is not None:
        out(f"      OK Registered into {chosen.get('code')}.")
        notify(webhook, f"✅ Registered **{row.get('title')}** ({code}) → course {chosen.get('code')}")
    else:
        out("      x Registration failed (registration period may be closed).")


def _switch_if_better(client, code, row, courses, pref_courses, webhook):
    signed_ids = {c.get("id") for c in courses if c.get("isSigned")}
    current = next((c for _, c in pref_courses if c and c.get("id") in signed_ids), None)
    out(f"      Status: REGISTERED (in {current.get('code') if current else '?'})")

    for pref, c in pref_courses:
        if not c:
            continue
        if current is not None and c.get("id") == current.get("id"):
            out(f"      OK Already in top available course ({pref}). Nothing to do.")
            return
        if seats_open(c):
            index_line_id = row.get("indexlineId")
            if not index_line_id:
                out("      x Missing indexlineId; cannot switch.")
                return
            out(f"      -> Switching to higher-priority {pref}")
            if client.change_course(index_line_id, c.get("id")) is not None:
                out(f"      OK Switched to {pref}.")
                notify(webhook, f"🔄 Switched **{row.get('title')}** ({code}) → course {pref}")
            else:
                out("      x Switch failed. See --verbose.")
            return
        out(f"      .. {pref} full, checking next")
    out("      OK No higher-priority course open. Staying put.")


# ---------------------------------------------------------------------------
# exam handling
# ---------------------------------------------------------------------------

def _exam_full(exam):
    strength = exam.get("strength", 0)
    max_strength = exam.get("maxStrength", 0)
    if exam.get("isFull") is True:
        return True
    return strength >= max_strength if max_strength else False


def _exam_date(exam):
    return (exam.get("fromDate") or "").split("T")[0]


def _exam_seat_str(exam):
    return f"{exam.get('strength', 0)}/{exam.get('maxStrength', '?')}"


def process_exam(client, idx, total, cfg, exams_by_subject, webhook):
    code = cfg.get("neptun_code") or cfg.get("subject")
    wanted_dates = cfg.get("dates", [])

    subject = exams_by_subject.get(code)
    out()
    out(f"[{idx}/{total}] Exam: {subject.get('subjectName') if subject else '?'} ({code})")

    if not subject:
        out("      ! No exams listed for this subject. Skipping.")
        return

    exam_list = subject.get("examList", [])
    if not exam_list:
        out("      ! Subject has no exams. Skipping.")
        return

    # Already registered to one of this subject's exams?
    already = next((e for e in exam_list if e.get("isRegistered") or e.get("isSigned")), None)
    if already:
        out(f"      OK Already registered for exam on {_exam_date(already)}. Nothing to do.")
        return

    # Build candidate list: preferred dates in order, else all exams by date.
    if wanted_dates:
        candidates = [e for d in wanted_dates for e in exam_list if _exam_date(e) == d]
    else:
        candidates = sorted(exam_list, key=_exam_date)

    out("      Candidates:")
    for e in candidates:
        out(f"        {_exam_date(e):<12} seats {_exam_seat_str(e)}{' FULL' if _exam_full(e) else ''}")

    target = next((e for e in candidates if not _exam_full(e)), None)
    if not target:
        out("      x No open exam among your choices.")
        return

    out(f"      -> Registering for exam on {_exam_date(target)}")
    if client.register_exam(target) is not None:
        out(f"      OK Registered for exam on {_exam_date(target)}.")
        notify(webhook, f"✅ Registered exam **{subject.get('subjectName')}** ({code}) → {_exam_date(target)}")
    else:
        out("      x Exam registration failed. See --verbose.")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_term(client):
    terms = rows(client.get_terms())
    for t in terms:
        if t.get("isActualTerm"):
            return t.get("value"), t.get("text")
    return (terms[0].get("value"), terms[0].get("text")) if terms else (None, None)


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="Neptun subject + exam automation")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--verbose", action="store_true", help="Print debug detail to stderr")
    parser.add_argument("--token-cache", default=".neptun-token.json", help="Path to token cache file")
    args = parser.parse_args()
    VERBOSE = args.verbose

    load_env(args.env)
    neptun_id = os.environ.get("NEPTUNID")
    password = os.environ.get("NEPTUN_PASSWORD")
    webhook = os.environ.get("DISCORD_WEBHOOK")

    if not neptun_id or not password:
        raise SystemExit("Set NEPTUNID and NEPTUN_PASSWORD in .env")

    config = load_config(args.config)
    base_url = config.get("base_url")
    subjects = config.get("subjects", [])
    exams = config.get("exams", [])

    client = NeptunClient(neptun_id, password, base_url=base_url, token_cache_path=args.token_cache)

    hr()
    out(f" User: {neptun_id}")
    if not client.authenticate():
        out(" x Authentication failed. Check .env credentials.")
        raise SystemExit(1)

    # --- subjects ---
    if subjects:
        term_value, term_text = resolve_term(client)
        out(f" Term: {term_text}")
        hr()
        out(" SUBJECTS")
        index = build_subject_index(client, term_value) if term_value else {}
        for i, cfg in enumerate(subjects, 1):
            try:
                process_subject(client, i, len(subjects), cfg, index, webhook)
            except Exception as e:
                debug(f"subject {cfg.get('neptun_code')} error: {e}")

    # --- exams ---
    if exams:
        out()
        hr()
        out(" EXAMS")
        exams_by_subject = {s.get("subjectCode"): s for s in rows(client.get_exams())}
        for i, cfg in enumerate(exams, 1):
            try:
                process_exam(client, i, len(exams), cfg, exams_by_subject, webhook)
            except Exception as e:
                debug(f"exam {cfg.get('neptun_code')} error: {e}")

    out()
    hr()
    out(" Done.")
    hr()


if __name__ == "__main__":
    main()
