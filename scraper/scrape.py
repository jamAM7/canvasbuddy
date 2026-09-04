#!/usr/bin/env python3
"""Scrape every subject the token owner is enrolled in into per-subject JSON.

    python scrape.py                        # active enrolments, links only
    python scrape.py --download --extract   # also pull files and read their text
    python scrape.py --include-concluded    # past sessions too
    python scrape.py --courses 41052 41201  # only these subject codes
    python scrape.py --refresh              # ignore the local cache

Credentials come from .env or the environment. Nothing is ever written to the
output that contains your token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from canvas import CanvasClient, CanvasError, SubjectBuilder, subject_code
from canvas.build import clean_name, slugify

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Minimal .env reader so there's no python-dotenv dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(HERE / "out"), help="output directory")
    parser.add_argument("--cache", default=str(HERE / ".cache"), help="response cache directory")
    parser.add_argument("--refresh", action="store_true", help="bypass the cache")
    parser.add_argument("--download", action="store_true", help="download course files")
    parser.add_argument("--extract", action="store_true",
                        help="extract text from downloaded files (implies --download)")
    parser.add_argument("--include-concluded", action="store_true",
                        help="include finished sessions")
    parser.add_argument("--session", default=None,
                        help="term to keep, matched loosely (e.g. 'Spring 2026'). "
                             "Defaults to the current session.")
    parser.add_argument("--all-sessions", action="store_true",
                        help="every session you're enrolled in, not just the current one")
    parser.add_argument("--ocr", action="store_true",
                        help="OCR scanned PDFs that have no text layer (macOS only)")
    parser.add_argument("--drop-boilerplate", action="store_true",
                        help="remove UTS template pages instead of just flagging them")
    parser.add_argument("--include-non-subjects", action="store_true",
                        help="keep courses with no subject code (induction modules, "
                             "academic integrity, org sites)")
    parser.add_argument("--courses", nargs="*", default=None,
                        help="only these subject codes or course IDs")
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def _dt(stamp: str | None):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def _term_window(courses: list[dict], name: str):
    """The widest start/end across every course carrying this term name."""
    needle = name.lower()
    starts, ends = [], []
    for course in courses:
        term = course.get("term") or {}
        if needle not in (term.get("name") or "").lower():
            continue
        start, end = _dt(term.get("start_at")), _dt(term.get("end_at"))
        if start:
            starts.append(start)
        if end:
            ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None)


def session_record(courses: list[dict], name: str, now: datetime, basis: str) -> dict:
    """A session choice plus the evidence for it, so the corpus can be audited."""
    start, end = _term_window(courses, name)
    return {
        "name": name,
        "basis": basis,
        "start_at": start.isoformat() if start else None,
        "end_at": end.isoformat() if end else None,
        "as_of": now.isoformat(),
        "verified": bool(start and start <= now and (not end or now <= end)),
    }


def pick_session(courses: list[dict]) -> dict | None:
    """Work out which term is the current one, and record how we knew.

    A term whose date window contains now is the only answer the wall clock can
    confirm. UTS sometimes leaves those dates null, so fall back to the term
    name shared by most of your subjects — but mark it unverified, because a
    corpus built from a guess shouldn't look like one built from a check. If
    the dates are there and none of them contain now, we're between sessions
    and the honest answer is to say so rather than pick the nearest term.
    """
    now = datetime.now(timezone.utc)
    live, dated = [], []
    for course in courses:
        term = course.get("term") or {}
        name, start, end = term.get("name"), _dt(term.get("start_at")), _dt(term.get("end_at"))
        if not name:
            continue
        if start or end:
            dated.append((name, end))
        if start and start <= now and (not end or now <= end):
            live.append(name)

    if live:
        return session_record(courses, Counter(live).most_common(1)[0][0], now, "term_dates")

    if dated:
        # Dates exist and none cover today — between sessions, not a guess to make.
        latest = max(dated, key=lambda pair: (pair[1] is not None, pair[1]))
        return session_record(courses, latest[0], now, "between_sessions")

    named = [(c.get("term") or {}).get("name") for c in courses if subject_code(c)]
    named = [n for n in named if n]
    if not named:
        return None
    return session_record(courses, Counter(named).most_common(1)[0][0], now, "name_majority")


def describe_session(selection: dict, tz: ZoneInfo) -> str:
    def day(stamp):
        moment = _dt(stamp)
        return moment.astimezone(tz).date().isoformat() if moment else "?"

    now = _dt(selection["as_of"]).astimezone(tz)
    return (f"term runs {day(selection['start_at'])} \u2192 {day(selection['end_at'])}, "
            f"current as of {now:%Y-%m-%d %H:%M %Z}")


_URL_RE = re.compile(r"https?://\S+")


def _fingerprint(content: str | None) -> str | None:
    """Identity of a page body, ignoring what differs between copies of a template.

    Canvas rewrites image and file URLs per course (different ids, different
    verifier tokens), so two copies of the same UTS template page are never
    byte-identical. Strip URLs and collapse whitespace, then hash what's left.
    """
    if not content:
        return None
    stripped = re.sub(r"\s+", " ", _URL_RE.sub("", content)).strip()
    if len(stripped) < 40:
        return None
    return hashlib.sha1(stripped.encode("utf-8")).hexdigest()


def mark_boilerplate(built: list[tuple[str, dict]], drop: bool) -> dict:
    """Flag pages whose body repeats across two or more subjects.

    UTS ships template pages into every subject shell. Title alone can't
    identify them — "Assessment overview" repeats too, and in 41052 it is the
    only place the weights and dates exist — so identity is decided on the body.
    """
    owners: dict[str, set] = {}
    for _, doc in built:
        code = doc["subject"]["code"] or doc["subject"]["name"]
        for page in doc["pages"]:
            fp = _fingerprint(page.get("content"))
            if fp:
                owners.setdefault(fp, set()).add(code)
    shared = {fp for fp, codes in owners.items() if len(codes) > 1}

    stats = {"flagged": 0, "dropped": 0, "titles": set()}
    for _, doc in built:
        kept, here = [], 0
        for page in doc["pages"]:
            if _fingerprint(page.get("content")) in shared:
                stats["flagged"] += 1
                stats["titles"].add(page["title"])
                here += 1
                if drop:
                    stats["dropped"] += 1
                    continue
                page["boilerplate"] = True
            kept.append(page)
        doc["pages"] = kept

        # Dropping pages can strand a module pointer; say so rather than let it
        # look like an empty page.
        slugs = {p.get("url_slug") for p in kept}
        for module in doc["modules"]:
            for item in module["items"]:
                if item.get("page_url"):
                    item["resolved"] = item["page_url"] in slugs
        doc["_meta"]["boilerplate"] = {
            "policy": "dropped" if drop else "flagged",
            "count": here,
            "note": ("Pages whose body repeats across subjects are UTS template "
                     "filler. " + ("They were removed from pages[]; module items "
                                   "that named them now have resolved=false."
                                   if drop else
                                   "They are marked boilerplate=true and can be "
                                   "filtered at prompt time.")),
        }
    return stats


def list_courses(client: CanvasClient, include_concluded: bool) -> list[dict]:
    params = {"include[]": ["term"], "enrollment_state": "active"}
    courses = client.get_list("/api/v1/courses", params)
    if include_concluded:
        seen = {str(c["id"]) for c in courses}
        done = client.get_list("/api/v1/courses",
                               {"include[]": ["term"], "enrollment_state": "completed"})
        courses += [c for c in done if str(c["id"]) not in seen]
    # Courses you can't read come back as stubs with an access_restricted flag.
    return [c for c in courses if c.get("id") and not c.get("access_restricted_by_date")]


def wanted(course: dict, filters: list[str] | None) -> bool:
    if not filters:
        return True
    code = subject_code(course) or ""
    return code in filters or str(course["id"]) in filters


def main() -> int:
    args = parse_args()
    load_env(HERE / ".env")

    base_url = os.environ.get("CANVAS_BASE_URL", "").strip()
    token = os.environ.get("CANVAS_TOKEN", "").strip()
    if not base_url or not token:
        print("Missing CANVAS_BASE_URL or CANVAS_TOKEN. Copy .env.example to .env "
              "and fill it in.", file=sys.stderr)
        return 1

    download = args.download or args.extract
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(args.timezone)
    selection = None

    client = CanvasClient(base_url, token, cache_dir=args.cache,
                          refresh=args.refresh, verbose=not args.quiet)

    try:
        courses = list_courses(client, args.include_concluded)
    except CanvasError as exc:
        print(f"Could not list courses: {exc}", file=sys.stderr)
        print("A 401 here means the token is wrong, expired, or from a different "
              "Canvas host.", file=sys.stderr)
        return 1

    courses = [c for c in courses if wanted(c, args.courses)]

    # Explicit --courses means you already said what you want; don't second-guess it.
    if not args.courses and not args.all_sessions:
        if not args.include_non_subjects:
            dropped = [c for c in courses if not subject_code(c)]
            courses = [c for c in courses if subject_code(c)]
            if dropped:
                print(f"Skipping {len(dropped)} non-subject course(s): "
                      f"{', '.join((c.get('name') or '?')[:40] for c in dropped[:4])}"
                      f"{' …' if len(dropped) > 4 else ''}")

        now = datetime.now(timezone.utc)
        selection = (session_record(courses, args.session, now, "explicit")
                     if args.session else pick_session(courses))

        if selection and selection["basis"] == "between_sessions":
            print(f"No session is running as of {now.astimezone(tz):%Y-%m-%d %H:%M %Z}. "
                  f"The nearest is {selection['name']!r} "
                  f"({describe_session(selection, tz)}).\n"
                  f"Pass --session '{selection['name']}' to scrape it anyway, or "
                  f"--all-sessions for everything.", file=sys.stderr)
            return 1

        if selection:
            needle = selection["name"].lower()
            kept = [c for c in courses
                    if needle in ((c.get("term") or {}).get("name") or "").lower()]
            if kept:
                skipped = len(courses) - len(kept)
                courses = kept
                if not selection["verified"]:
                    reason = ("no term dates published" if selection["basis"] == "name_majority"
                              else "today falls outside its window")
                    print(f"WARNING: session not date-verified — {reason}.", file=sys.stderr)
                print(f"Session: {selection['name']} — {describe_session(selection, tz)}"
                      + (f"; {skipped} subject(s) from other sessions skipped"
                         if skipped else ""))
            elif args.session:
                print(f"No subjects matched session {args.session!r}.", file=sys.stderr)
                return 1

    if not courses:
        print("No matching courses.", file=sys.stderr)
        return 1

    print(f"\n{len(courses)} subject(s) to scrape\n")
    index = []
    built: list[tuple[str, dict]] = []

    for number, course in enumerate(courses, 1):
        label = course.get("name") or course["id"]
        print(f"[{number}/{len(courses)}] {label}")
        builder = SubjectBuilder(client, course, out_dir, tz=args.timezone,
                                 download=download, extract=args.extract,
                                 ocr=args.ocr)
        try:
            document = builder.build()
        except CanvasError as exc:
            print(f"  failed: {exc}\n")
            continue
        # builder.slug is only set once build() has seen the course.
        built.append((f"{builder.slug}.json", document))

    # Template pages can only be spotted by comparing subjects against each
    # other, so this waits until every subject is built.
    stats = mark_boilerplate(built, drop=args.drop_boilerplate)
    if stats["flagged"]:
        verb = "dropped" if args.drop_boilerplate else "flagged"
        print(f"\nBoilerplate: {stats['flagged']} page(s) {verb} across subjects "
              f"({', '.join(sorted(stats['titles'])[:4])}"
              f"{' …' if len(stats['titles']) > 4 else ''})\n")

    for filename, document in built:
        (out_dir / filename).write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        gaps = [k for k, v in document["_meta"]["coverage"].items() if v["status"] != "ok"]
        print(f"{document['subject']['code'] or '?'}: "
              f"{len(document['assessments'])} assessments, "
              f"{len(document['modules'])} modules, "
              f"{len(document['pages'])} pages, "
              f"{len(document['links'])} links -> {filename}")
        if gaps:
            print(f"    not retrieved: {', '.join(gaps)}")

        index.append({
            "code": document["subject"]["code"],
            "name": document["subject"]["name"],
            "session": document["subject"]["session"],
            "course_id": document["subject"]["course_id"],
            "file": filename,
            "assessment_count": len(document["assessments"]),
            "coverage_gaps": gaps,
        })

    (out_dir / "index.json").write_text(
        json.dumps({"subjects": index, "canvas_host": base_url,
                    "session_selection": selection}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Done. {client.request_count} API requests. Output in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
