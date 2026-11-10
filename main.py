import os
import sys
import json
import time
import argparse
from datetime import datetime
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
# Exam/course IDs from GetExamsList are bound to the login session (IdExpiredError otherwise),
# so always sign up with IDs fetched using the same token.
EXAM_REGISTER_PATH = "ExamRegistration/SignUpForExam"


# ---------------------------------------------------------------------------
# console output
# ---------------------------------------------------------------------------

VERBOSE = False
LOG_FILE = None


def _log_line(level, msg):
    if LOG_FILE is None:
        return
    text = str(msg)
    timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    LOG_FILE.write(f"[{timestamp}] [{level}] {text}\n")
    LOG_FILE.flush()


def out(msg=""):
    print(msg)
    _log_line("OUT", msg)


def hr(char="="):
    print(char * 60)


def debug(msg):
    if VERBOSE:
        print(f"  [debug] {msg}", file=sys.stderr)
        _log_line("DEBUG", msg)


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
        resp = self.session.post(self._url(EXAM_REGISTER_PATH), json={
            "examId": exam.get("examId") or exam.get("id"),
            "courseId": exam.get("courseId"),
        })
        if resp.status_code in (200, 201):
            return resp.json() if resp.text else {}
        reason = resp.text[:300]
        try:
            notes = resp.json().get("notification") or []
            if notes:
                reason = "; ".join(n.get("description") or "" for n in notes)
        except Exception:
            pass
        if resp.status_code == 401:
            # token expired: drop it so the next attempt logs in and refetches (session-bound) IDs
            Path(self._token_cache_path).unlink(missing_ok=True)
            self.session.headers.pop("Authorization", None)
            self.token = None
        out(f"      Neptun said ({resp.status_code}): {reason}")
        return None


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
    """EA lecture, e-learning, or exam course matching `prefix`, excluding longer-suffixed variants.
    'SzGH_EA' matches 'SzGH_EA' but NOT 'SzGH_EA_SLG'.
    """
    exact = [c for c in courses if (c.get("code") or "") == prefix]
    if exact:
        return exact[0]
    candidates = [c for c in courses
                  if (c.get("code") or "").startswith(prefix)
                  and not (c.get("code") or "")[len(prefix):].startswith("_")]
    if candidates:
        if len(candidates) > 1:
            debug(f"Lecture prefix '{prefix}' matched {len(candidates)}; using {candidates[0].get('code')}")
        return candidates[0]
    fallback = [c for c in courses if (c.get("code") or "").startswith(prefix)]
    if fallback:
        if len(fallback) > 1:
            debug(f"Lecture fallback prefix '{prefix}' matched {len(fallback)}; using {fallback[0].get('code')}")
        return fallback[0]
    return None


def find_course(courses, code):
    return next((c for c in courses if (c.get("code") or "") == code), None)


def match_course(course, target):
    """Matches course either by course code string or numeric/string ID."""
    target_str = str(target).strip()
    c_code = str(course.get("code") or "").strip()
    c_id = str(course.get("id") or "").strip()
    return c_code == target_str or c_id == target_str


def open_seats_count(course):
    """Returns number of available seats if limits are specified, else None."""
    mx = course.get("maxLimit")
    reg = course.get("registeredStudentsCount") or 0
    if isinstance(mx, int) and mx > 0:
        return max(0, mx - reg)
    return None



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
    """Returns True when nothing more needs to be done for this subject."""
    code = cfg.get("neptun_code")
    lecture_prefix = cfg.get("lecture")
    prefs = cfg.get("courses", [])

    row = index.get(code)
    out()
    out(f"[{idx}/{total}] {row.get('title') if row else '?'} ({code})")

    if not row:
        out("      ! Not schedulable at this time. Skipping.")
        return False
    courses = rows(client.get_subject_courses(row))
    if not courses:
        out("      ! No courses returned. Skipping.")
        return False

    if not lecture_prefix and not prefs:
        out("      ! Neither lecture nor courses specified in config. Skipping.")
        return False

    lecture = pick_lecture(courses, lecture_prefix) if lecture_prefix else None
    if lecture_prefix:
        if not lecture:
            out(f"      ! No lecture matching '{lecture_prefix}'. Skipping.")
            return False
        out(f"      Lecture: {lecture.get('code')} [{lecture.get('type')}]  seats {seat_str(lecture)}")

    pref_courses = []
    if prefs:
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
        return _switch_if_better(client, code, row, courses, pref_courses, webhook)
    else:
        return _register_subject(client, code, row, lecture, pref_courses, webhook)


def _register_subject(client, code, row, lecture, pref_courses, webhook):
    """Returns True if registered successfully, False if seats unavailable or period closed."""
    out("      Status: NOT REGISTERED")
    course_ids = []
    registered_names = []

    if lecture is not None:
        if not seats_open(lecture):
            out(f"      x Lecture/Course {lecture.get('code')} is FULL.")
            return False
        course_ids.append(lecture.get("id"))
        registered_names.append(lecture.get("code"))

    if pref_courses:
        chosen = next((c for _, c in pref_courses if c and seats_open(c)), None)
        if not chosen:
            out("      x No priority course has open seats.")
            return False
        course_ids.append(chosen.get("id"))
        registered_names.append(chosen.get("code"))
    elif lecture is None:
        out("      x No lecture or courses to register.")
        return False

    target_desc = " + ".join(registered_names)
    out(f"      -> Registering {target_desc}")
    if client.register_subject(row, course_ids) is not None:
        out(f"      OK Registered into {target_desc}.")
        notify(webhook, f"✅ Registered **{row.get('title')}** ({code}) → {target_desc}")
        return True
    else:
        out("      x Registration failed (registration period may be closed).")
        return False


def _switch_if_better(client, code, row, courses, pref_courses, webhook):
    """Returns True when already in the best possible course (nothing left to do)."""
    signed_courses = [c for c in courses if c.get("isSigned")]
    signed_ids = {c.get("id") for c in signed_courses}
    signed_str = ", ".join(c.get("code", "?") for c in signed_courses) if signed_courses else "?"
    out(f"      Status: REGISTERED (in {signed_str})")

    if not pref_courses:
        out("      OK Already registered. Nothing to do.")
        return True

    current = next((c for _, c in pref_courses if c and c.get("id") in signed_ids), None)

    for pref, c in pref_courses:
        if not c:
            continue
        if current is not None and c.get("id") == current.get("id"):
            out(f"      OK Already in top available course ({pref}). Nothing to do.")
            return True
        if seats_open(c):
            index_line_id = row.get("indexlineId")
            if not index_line_id:
                out("      x Missing indexlineId; cannot switch.")
                return True  # registered, just can't improve
            out(f"      -> Switching to higher-priority {pref}")
            if client.change_course(index_line_id, c.get("id")) is not None:
                out(f"      OK Switched to {pref}.")
                notify(webhook, f"🔄 Switched **{row.get('title')}** ({code}) → course {pref}")
            else:
                out("      x Switch failed. See --verbose.")
            return True  # registered regardless of switch outcome
        out(f"      .. {pref} full, checking next")
    out("      OK No higher-priority course open. Staying put.")
    return True  # registered in best available


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
    """Returns True when registered (or already was), False if not yet possible."""
    code = cfg.get("neptun_code") or cfg.get("subject")
    wanted_dates = cfg.get("dates", [])

    subject = exams_by_subject.get(code)
    out()
    out(f"[{idx}/{total}] Exam: {subject.get('subjectName') if subject else '?'} ({code})")

    if not subject:
        out("      ! No exams listed for this subject. Skipping.")
        return False

    exam_list = subject.get("examList", [])
    if not exam_list:
        out("      ! Subject has no exams. Skipping.")
        return False

    # Already registered to one of this subject's exams?
    already = next((e for e in exam_list if e.get("examSignin") or e.get("examSignInId") or e.get("isRegistered") or e.get("isSigned")), None)
    if already:
        out(f"      OK Already registered for exam on {_exam_date(already)}. Nothing to do.")
        return True

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
        return False

    out(f"      -> Registering for exam on {_exam_date(target)}")
    if client.register_exam(target) is not None:
        out(f"      OK Registered for exam on {_exam_date(target)}.")
        notify(webhook, f"✅ Registered exam **{subject.get('subjectName')}** ({code}) → {_exam_date(target)}")
        return True
    else:
        out("      x Exam registration failed. See --verbose.")
        return False


# ---------------------------------------------------------------------------
# course monitoring
# ---------------------------------------------------------------------------

def parse_check_interval(cfg):
    """Parse check interval from config in seconds, defaulting to 300 (5 minutes)."""
    if "check_interval_minutes" in cfg:
        try:
            return max(1, int(float(cfg["check_interval_minutes"]) * 60))
        except (ValueError, TypeError):
            pass
    if "interval_minutes" in cfg:
        try:
            return max(1, int(float(cfg["interval_minutes"]) * 60))
        except (ValueError, TypeError):
            pass
    for key in ("check_interval", "interval", "check_interval_seconds", "loop_delay"):
        if key in cfg:
            try:
                return max(1, int(cfg[key]))
            except (ValueError, TypeError):
                pass
    return 300


def check_monitored_subjects(client, monitor_cfg, index):
    """Check configured subjects and courses for open seats.

    Returns a list of dicts for courses that have open seats:
      [
        {
          "subject_title": ...,
          "subject_code": ...,
          "course_code": ...,
          "course_id": ...,
          "seat_str": ...,
          "open_count": ...,
        }
      ]
    """
    open_courses = []
    subjects_cfg = monitor_cfg.get("subjects")

    if subjects_cfg is not None:
        for s_cfg in subjects_cfg:
            code = s_cfg.get("neptun_code")
            if not code:
                continue
            row = index.get(code)
            if not row:
                debug(f"Subject {code} not schedulable right now.")
                continue

            subject_title = row.get("title") or code
            courses = rows(client.get_subject_courses(row))
            if not courses:
                debug(f"No courses returned for {code}")
                continue

            targets = s_cfg.get("courses") or s_cfg.get("course_ids")
            lecture_prefix = s_cfg.get("lecture")
            checked_course_ids = set()

            if targets:
                for target in targets:
                    matched = [c for c in courses if match_course(c, target)]
                    for c in matched:
                        c_id = c.get("id")
                        if c_id in checked_course_ids:
                            continue
                        checked_course_ids.add(c_id)
                        if seats_open(c):
                            open_courses.append({
                                "subject_title": subject_title,
                                "subject_code": code,
                                "course_code": c.get("code") or "?",
                                "course_id": c_id,
                                "seat_str": seat_str(c),
                                "open_count": open_seats_count(c),
                            })
            else:
                if not lecture_prefix:
                    for c in courses:
                        c_id = c.get("id")
                        if c_id in checked_course_ids:
                            continue
                        checked_course_ids.add(c_id)
                        if seats_open(c):
                            open_courses.append({
                                "subject_title": subject_title,
                                "subject_code": code,
                                "course_code": c.get("code") or "?",
                                "course_id": c_id,
                                "seat_str": seat_str(c),
                                "open_count": open_seats_count(c),
                            })

            if lecture_prefix:
                lec = pick_lecture(courses, lecture_prefix)
                if lec:
                    l_id = lec.get("id")
                    if l_id not in checked_course_ids and seats_open(lec):
                        checked_course_ids.add(l_id)
                        open_courses.append({
                            "subject_title": subject_title,
                            "subject_code": code,
                            "course_code": lec.get("code") or "?",
                            "course_id": l_id,
                            "seat_str": seat_str(lec),
                            "open_count": open_seats_count(lec),
                        })
    else:
        top_courses = monitor_cfg.get("courses") or monitor_cfg.get("course_ids") or []
        if top_courses:
            for s_code, row in index.items():
                subject_title = row.get("title") or s_code
                courses = rows(client.get_subject_courses(row))
                for target in top_courses:
                    for c in courses:
                        if match_course(c, target) and seats_open(c):
                            open_courses.append({
                                "subject_title": subject_title,
                                "subject_code": s_code,
                                "course_code": c.get("code") or "?",
                                "course_id": c.get("id"),
                                "seat_str": seat_str(c),
                                "open_count": open_seats_count(c),
                            })

    return open_courses


def run_monitor_loop(client, neptun_id, monitor_config_path, webhook):
    """Continuously monitors courses and alerts on Discord when open seats exist.
    Never exits unless interrupted by the user (runs forever)."""
    hr()
    out(f" Neptun Course Monitor")
    out(f" User: {neptun_id}")
    out(f" Config: {monitor_config_path}")
    if webhook:
        out(" Discord notifications: ENABLED")
    else:
        out(" Discord notifications: DISABLED (no DISCORD_WEBHOOK in .env)")
    hr()

    try:
        if not client.authenticate():
            out(" x Authentication failed. Check .env credentials and base_url.")
            raise SystemExit(1)
    except requests.exceptions.RequestException as e:
        out(f" x Connection error during authentication: {e}")
        raise SystemExit(1)

    check_num = 0
    last_notified_state = None

    while True:
        check_num += 1
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Dynamically reload config in case user updated courses or interval while running
        try:
            cfg = load_config(monitor_config_path)
        except Exception as e:
            debug(f"Failed to reload {monitor_config_path}: {e}")
            cfg = {}

        interval = parse_check_interval(cfg)
        notify_on_change = bool(cfg.get("notify_on_change_only", False))

        try:
            term_value, term_text = resolve_term(client)
            if not term_value:
                out(f"[{now_str}] Check #{check_num}: Failed to resolve active term.")
            else:
                index = build_subject_index(client, term_value)
                open_courses = check_monitored_subjects(client, cfg, index)

                if open_courses:
                    out()
                    out(f"[{now_str}] Check #{check_num}: OPEN SPACES FOUND ({len(open_courses)} course(s))!")
                    lines = ["📢 **Neptun: Open Course Space Detected!**"]
                    current_state = set()

                    for c in open_courses:
                        open_cnt = f" ({c['open_count']} seats open)" if c['open_count'] is not None else ""
                        out(f"    * {c['subject_title']} ({c['subject_code']}) - Course {c['course_code']}: {c['seat_str']}{open_cnt}")
                        lines.append(f"• **{c['subject_title']}** (`{c['subject_code']}`) → Course **`{c['course_code']}`**: {c['seat_str']}{open_cnt}")
                        current_state.add((c['subject_code'], str(c['course_id']), c['seat_str']))

                    should_notify = True
                    if notify_on_change and current_state == last_notified_state:
                        should_notify = False
                        debug("Seats unchanged and notify_on_change_only=true; skipping Discord notification.")

                    if should_notify:
                        msg = "\n".join(lines)
                        notify(webhook, msg)
                        last_notified_state = current_state
                else:
                    out(f"[{now_str}] Check #{check_num}: All monitored courses are full. No open spaces.")
                    last_notified_state = set()

        except requests.exceptions.RequestException as e:
            out(f"[{now_str}] Check #{check_num}: Network/API error: {e}")
        except Exception as e:
            out(f"[{now_str}] Check #{check_num}: Unexpected error: {e}")

        out(f"      Sleeping {interval}s until next check (press Ctrl+C to stop)...")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            out()
            out(" Monitoring interrupted by user. Exiting.")
            break


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path, config):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def parse_registration_start(value):
    """Parse config registration start datetime.

    Accepted examples:
      - 2026-09-01 17:59:00
      - 2026-09-01T17:59:00
      - 2026-09-01T17:59:00+02:00
      - 2026-09-01T15:59:00Z
    """
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("registration_start must be a string")
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(
            "Invalid registration_start format. Use ISO datetime like "
            "'2026-09-01 17:59:00' or '2026-09-01T17:59:00+02:00'."
        ) from e


def wait_until_registration_start(registration_start):
    """Block until registration start; refresh current time on a single line."""
    if registration_start is None:
        return

    last_len = 0
    while True:
        now = datetime.now(registration_start.tzinfo) if registration_start.tzinfo else datetime.now()
        if now >= registration_start:
            break

        line = (
            "Waiting for registration start... "
            f"Now: {now.isoformat(sep=' ', timespec='seconds')} | "
            f"Start: {registration_start.isoformat(sep=' ', timespec='seconds')}"
        )
        pad = " " * max(0, last_len - len(line))
        sys.stdout.write("\r" + line + pad)
        sys.stdout.flush()
        last_len = len(line)
        time.sleep(1)

    if last_len:
        sys.stdout.write("\r" + (" " * last_len) + "\r")
        sys.stdout.flush()
    out("Registration start reached. Continuing.")


def resolve_term(client):
    terms = rows(client.get_terms())
    for t in terms:
        if t.get("isActualTerm"):
            return t.get("value"), t.get("text")
    return (terms[0].get("value"), terms[0].get("text")) if terms else (None, None)


def run_once(client, neptun_id, subjects, exams, webhook):
    """Authenticate and attempt all subject + exam registrations. Returns True if
    every configured item is already registered (nothing left to do)."""
    hr()
    out(f" User: {neptun_id}")
    if not client.authenticate():
        out(" x Authentication failed. Check .env credentials.")
        raise SystemExit(1)

    all_done = True
    subjects_done = True

    # --- subjects ---
    if subjects:
        term_value, term_text = resolve_term(client)
        out(f" Term: {term_text}")
        hr()
        out(" SUBJECTS")
        index = build_subject_index(client, term_value) if term_value else {}
        for i, cfg in enumerate(subjects, 1):
            try:
                done = process_subject(client, i, len(subjects), cfg, index, webhook)
                if not done:
                    all_done = False
                    subjects_done = False
            except Exception as e:
                debug(f"subject {cfg.get('neptun_code')} error: {e}")
                all_done = False
                subjects_done = False

    # --- exams ---
    if exams:
        out()
        hr()
        out(" EXAMS")
        exams_by_subject = {s.get("subjectCode"): s for s in rows(client.get_exams())}
        for i, cfg in enumerate(exams, 1):
            try:
                done = process_exam(client, i, len(exams), cfg, exams_by_subject, webhook)
                if not done:
                    all_done = False
            except Exception as e:
                debug(f"exam {cfg.get('neptun_code')} error: {e}")
                all_done = False

    out()
    hr()
    out(" Done.")
    hr()

    return all_done, subjects_done


def main():
    global VERBOSE, LOG_FILE
    parser = argparse.ArgumentParser(description="Neptun subject + exam automation")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--verbose", action="store_true", help="Print debug detail to stderr")
    parser.add_argument("--log", action="store_true", help="Write output to log.txt")
    parser.add_argument("--token-cache", default=".neptun-token.json", help="Path to token cache file")
    parser.add_argument(
        "--monitor",
        nargs="?",
        const="monitor.json",
        default=None,
        help="Run continuous course monitoring using monitor.json (or specify a custom path)",
    )

    argv = sys.argv[1:]
    if argv and argv[0] == "monitor":
        argv[0] = "--monitor"
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    if args.log:
        try:
            LOG_FILE = open("log.txt", "a", encoding="utf-8")
            _log_line("INFO", "--- run started ---")
        except OSError as e:
            raise SystemExit(f"Failed to open log.txt: {e}")

    try:
        load_env(args.env)
        neptun_id = os.environ.get("NEPTUNID")
        password = os.environ.get("NEPTUN_PASSWORD")
        webhook = os.environ.get("DISCORD_WEBHOOK")

        if not neptun_id or not password:
            raise SystemExit("Set NEPTUNID and NEPTUN_PASSWORD in .env")

        if args.monitor is not None:
            monitor_path = args.monitor
            if not os.path.exists(monitor_path):
                raise SystemExit(
                    f"Monitor config file not found: {monitor_path}\n"
                    f"Please create '{monitor_path}' (see monitor.example.json for an example)."
                )

            monitor_cfg = load_config(monitor_path)
            base_url = monitor_cfg.get("base_url")
            if not base_url and os.path.exists(args.config):
                try:
                    base_url = load_config(args.config).get("base_url")
                except Exception:
                    pass
            if not base_url:
                base_url = os.environ.get("NEPTUN_BASE_URL")
            if not base_url:
                raise SystemExit("base_url not found in monitor.json, config.json, or NEPTUN_BASE_URL")

            client = NeptunClient(neptun_id, password, base_url=base_url, token_cache_path=args.token_cache)
            run_monitor_loop(client, neptun_id, monitor_path, webhook)
            return

        config = load_config(args.config)
        base_url = config.get("base_url")
        subjects = config.get("subjects", [])
        exams = config.get("exams", [])
        loop_delay = int(config.get("loop_delay", 30))
        loop_tries = int(config.get("loop_tries", 1))
        # optional: poll faster for the first fast_minutes after registration_start
        fast_delay = int(config.get("fast_delay", loop_delay))
        fast_minutes = float(config.get("fast_minutes", 0))
        try:
            loop_until = parse_registration_start(config.get("loop_until"))
        except ValueError as e:
            raise SystemExit(f"loop_until: {e}")
        completed = bool(config.get("completed", False))

        if completed:
            out("completed=true in config, skipping")
            return

        try:
            registration_start = parse_registration_start(config.get("registration_start"))
        except ValueError as e:
            raise SystemExit(str(e))

        if loop_tries < 1:
            raise SystemExit("loop_tries must be >= 1")

        wait_until_registration_start(registration_start)

        client = NeptunClient(neptun_id, password, base_url=base_url, token_cache_path=args.token_cache)

        for attempt in range(1, loop_tries + 1):
            if loop_tries > 1:
                out()
                hr("=")
                out(f" Attempt #{attempt}/{loop_tries}")
                hr("=")

            out()
            try:
                all_done, subjects_done = run_once(client, neptun_id, subjects, exams, webhook)
            except SystemExit:
                raise
            except Exception as e:
                out(f" ! Unexpected error: {e}")
                all_done = False
                subjects_done = False

            if subjects and subjects_done and not completed:
                config["completed"] = True
                save_config(args.config, config)
                out(" All configured subjects are registered. Set completed=true in config.")
                completed = True

            if all_done:
                out(" All items registered. Exiting.")
                break

            now = datetime.now().astimezone()
            if loop_until is not None and now >= loop_until:
                out(f" Reached loop_until ({loop_until.isoformat()}). Exiting.")
                break
            if attempt < loop_tries:
                in_fast = (registration_start is not None and fast_minutes > 0 and
                           (now - registration_start.astimezone()).total_seconds() < fast_minutes * 60)
                delay = fast_delay if in_fast else loop_delay
                out(f" Waiting {delay}s before next attempt...")
                try:
                    time.sleep(delay)
                except KeyboardInterrupt:
                    out()
                    out(" Interrupted. Exiting.")
                    break
            else:
                out(" Reached configured loop_tries limit. Exiting.")
    finally:
        if LOG_FILE is not None:
            _log_line("INFO", "--- run ended ---")
            LOG_FILE.close()
            LOG_FILE = None


if __name__ == "__main__":
    main()
