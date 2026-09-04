# Fetches one course and assembles it into a single json doc
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .client import CanvasClient, CanvasError, CanvasForbidden
from .content import extract_links, file_to_text, html_to_markdown

CODE_RE = re.compile(r"\b(\d{5})\b")


def slugify(value: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:limit] or "course"


def subject_code(course: dict) -> str | None:
    for field in ("course_code", "name", "sis_course_id"):
        match = CODE_RE.search(str(course.get(field) or ""))
        if match:
            return match.group(1)
    return None


def clean_name(course: dict) -> str:
    """'41052 Advanced Algorithms' -> 'Advanced Algorithms'."""
    name = (course.get("name") or "").strip()
    return re.sub(r"^\d{5}[\s:_-]*", "", name).strip() or name


def to_local(stamp: str | None, tz: ZoneInfo) -> str | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).isoformat()


def compute_weights(course: dict, groups: list[dict]) -> str:
    
    def counts(assignment: dict) -> bool:
        return (
            not assignment.get("omit_from_final_grade")
            and assignment.get("grading_type") != "not_graded"
            and (assignment.get("points_possible") or 0) > 0
        )

    if course.get("apply_assignment_group_weights"):
        for group in groups:
            assignments = [a for a in group.get("assignments", []) if counts(a)]
            total = sum(a.get("points_possible") or 0 for a in assignments)
            group_weight = group.get("group_weight") or 0
            for assignment in group.get("assignments", []):
                if counts(assignment) and total:
                    share = (assignment["points_possible"] / total) * group_weight
                    assignment["weight_pct"] = round(share, 2)
                else:
                    assignment["weight_pct"] = None
        return "group_weights"

    everything = [a for group in groups for a in group.get("assignments", []) if counts(a)]
    total = sum(a.get("points_possible") or 0 for a in everything)
    for group in groups:
        for assignment in group.get("assignments", []):
            if counts(assignment) and total:
                assignment["weight_pct"] = round(assignment["points_possible"] / total * 100, 2)
            else:
                assignment["weight_pct"] = None
    return "points_proportional"


class SubjectBuilder:
    def __init__(
        self,
        client: CanvasClient,
        course: dict,
        out_dir: Path,
        tz: str = "Australia/Sydney",
        download: bool = False,
        extract: bool = False,
        ocr: bool = False,
    ):
        self.client = client
        self.course = course
        self.course_id = str(course["id"])
        self.out_dir = out_dir
        self.tz = ZoneInfo(tz)
        self.download = download
        self.extract = extract
        self.ocr = ocr
        self.coverage: dict[str, dict] = {}
        self.web_url = f"{client.base_url}/courses/{self.course_id}"
        self._file_cache: dict[str, dict] = {}
        self._sha_index: dict[str, str] = {}

    # ------------------------------------------------------------------ utils

    def _safe(self, name: str, fn, default):
        """Run one endpoint. A hidden tab must not kill the whole subject."""
        try:
            value = fn()
        except CanvasForbidden as exc:
            self.coverage[name] = {"status": "forbidden", "note": "hidden or restricted tab"}
            return default
        except CanvasError as exc:
            # Canvas says this when staff switch a tab off. That's a fact about
            # the subject, not a failure to read it, and an LLM reading the
            # coverage block needs to be able to tell those apart.
            disabled = "has been disabled for this course" in (exc.message or "")
            self.coverage[name] = {
                "status": "disabled" if disabled else "error",
                "note": ("tab switched off by teaching staff" if disabled
                         else exc.message[:200]),
            }
            return default
        count = len(value) if hasattr(value, "__len__") else 1
        self.coverage[name] = {"status": "ok", "count": count}
        return value

    def _record_assessment_content(self, assessments: list[dict]) -> None:
        """Roll the per-assessment provenance up into the coverage block.

        Endpoint-level coverage can't catch this: /assignments returns 200 with
        every brief empty, which looks identical to a subject that simply has
        short briefs. This is the same "did we fail, or is there nothing?"
        question one level down.
        """
        total = len(assessments)
        if not total:
            return
        briefs = sum(1 for a in assessments if a["content_status"]["brief"] == "present")
        rubrics = sum(1 for a in assessments if a["content_status"]["rubric"] == "present")
        # Only placeholders with nothing in them are worth pointing elsewhere for.
        placeholders = sum(1 for a in assessments
                           if a["content_status"]["gradebook_placeholder"]
                           and a["content_status"]["brief"] == "absent_in_canvas")
        sources = sorted({s for a in assessments for s in a["content_status"]["likely_source"]})

        note = f"{briefs}/{total} assessments carry a description in Canvas"
        if rubrics:
            note += f"; {rubrics} carry a rubric"
        else:
            note += ("; none carry a rubric, and course-level rubrics are teacher-only "
                     "via the API, so one may exist that this token cannot read")
        if placeholders:
            note += (f"; {placeholders} of those "
                     f"{'is a gradebook placeholder' if placeholders == 1 else 'are gradebook placeholders'} "
                     "(submission_types=none), so the task is described elsewhere")
        if sources:
            note += f". Look in: {', '.join(sources)}"

        self.coverage["assessment_content"] = {
            "status": "ok" if briefs == total else ("partial" if briefs else "absent"),
            "count": total,
            "with_brief": briefs,
            "with_rubric": rubrics,
            "note": note + ".",
        }

    def _course_path(self, suffix: str) -> str:
        return f"/api/v1/courses/{self.course_id}/{suffix}"

    def _markdown(self, html: str | None) -> str:
        return html_to_markdown(html, base_url=self.client.base_url)

    def _links(self, html: str | None) -> list[dict]:
        return extract_links(html, base_url=self.client.base_url)

    # ------------------------------------------------------------------ files

    def _handle_file(self, file_obj: dict) -> dict:
        """Metadata for a Canvas file, plus local copy and text if requested."""
        file_id = str(file_obj.get("id"))
        if file_id in self._file_cache:
            return self._file_cache[file_id]

        record = {
            "id": file_id,
            "name": file_obj.get("display_name") or file_obj.get("filename"),
            "content_type": file_obj.get("content-type") or file_obj.get("content_type"),
            "size_bytes": file_obj.get("size"),
            "updated_at": file_obj.get("updated_at"),
            "url": f"{self.web_url}/files/{file_id}",
        }

        if self.download and file_obj.get("url"):
            dest = self.out_dir / "files" / self.slug / f"{file_id}_{record['name']}"
            try:
                # Canvas gives the size up front, so a local copy that already
                # matches it needs no second trip. Re-runs would otherwise pull
                # the whole course library again every time.
                expected = file_obj.get("size")
                if not (dest.exists() and expected and dest.stat().st_size == expected):
                    # The `url` field is pre-signed and expires, so fetch it now.
                    self.client.download(file_obj["url"], dest)
                record["local_path"] = str(dest.relative_to(self.out_dir))
                record["sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
                # Staff re-upload the same document as "(1)", "(2)-1" and so on.
                # Identical bytes need their text extracted and stored only once.
                first = self._sha_index.get(record["sha256"])
                if first and first != file_id:
                    record["duplicate_of"] = first
                    if self.extract:
                        record["extracted"] = {
                            "status": "duplicate",
                            "note": f"byte-identical to file {first}; its text is stored there",
                        }
                else:
                    self._sha_index[record["sha256"]] = file_id
                    if self.extract:
                        record["extracted"] = file_to_text(dest, ocr=self.ocr)
            except Exception as exc:
                record["download_error"] = str(exc)[:200]

        self._file_cache[file_id] = record
        return record

    def _attachments_for(self, html: str | None) -> list[dict]:
        """Resolve /files/<id> links inside a brief into real file records."""
        attachments = []
        for link in self._links(html):
            match = re.search(r"/files/(\d+)", link["url"])
            if not match:
                continue
            file_id = match.group(1)
            if file_id in self._file_cache:
                attachments.append(self._file_cache[file_id])
                continue
            try:
                file_obj, _ = self.client.get(f"/api/v1/files/{file_id}")
            except CanvasError:
                continue
            attachments.append(self._handle_file(file_obj))
        return attachments

    # ------------------------------------------------------------------ build

    def build(self) -> dict:
        course = self.course
        code = subject_code(course)
        name = clean_name(course)
        self.slug = f"{code}-{slugify(name)}" if code else slugify(course.get("name", ""))

        detail = self._safe(
            "course",
            lambda: self.client.get(
                f"/api/v1/courses/{self.course_id}",
                {"include[]": ["syllabus_body", "term", "teachers", "public_description"]},
            )[0],
            course,
        )
        course = {**course, **(detail or {})}

        groups = self._safe(
            "assignment_groups",
            lambda: self.client.get_list(
                self._course_path("assignment_groups"), {"include[]": ["assignments"]}
            ),
            [],
        )
        grading_rule = compute_weights(course, groups)

        # The nested copies inside assignment_groups omit some fields, so pull the
        # full objects too. all_dates carries section overrides, which is where the
        # real due date lives when the base assignment has none.
        details = self._safe(
            "assignment_details",
            lambda: {
                str(a["id"]): a
                for a in self.client.get_list(
                    self._course_path("assignments"),
                    {"include[]": ["submission", "all_dates", "overrides"]},
                )
            },
            {},
        )

        assessments = self._assessments(groups, details)
        self._record_assessment_content(assessments)
        quizzes = self._safe("quizzes", self._quizzes, [])

        # Modules first: when the Pages index is disabled, module items are the
        # only way to discover which pages exist.
        modules = self._safe("modules", self._modules, [])
        page_slugs = [
            item["page_url"]
            for module in modules
            for item in module["items"]
            if item.get("page_url")
        ]
        pages = self._pages(page_slugs)
        # Module items stay pointers. The body lives once, in pages[], keyed by
        # the item's page_url — storing it in both places doubled every page.
        self._link_pages(modules, {p["url_slug"]: p for p in pages if p.get("url_slug")})

        files = self._safe(
            "files",
            lambda: [self._handle_file(f) for f in self.client.get_list(self._course_path("files"))],
            [],
        )
        announcements = self._safe("announcements", self._announcements, [])
        discussions = self._safe("discussions", self._discussions, [])
        tabs = self._safe(
            "tabs",
            lambda: [
                {"label": t.get("label"), "url": t.get("full_url"), "hidden": bool(t.get("hidden"))}
                for t in self.client.get_list(self._course_path("tabs"))
            ],
            [],
        )
        tools = self._safe(
            "external_tools",
            lambda: [
                {"name": t.get("name"), "url": t.get("url"), "domain": t.get("domain")}
                for t in self.client.get_list(self._course_path("external_tools"))
            ],
            [],
        )
        if not tools and tabs:
            # Students usually can't list external_tools, but the tab list exposes
            # the same launches (Ed, Reading List, the subject outline LTI).
            tools = [
                {"name": tab["label"], "url": tab["url"], "domain": None, "source": "tabs"}
                for tab in tabs
                if "/external_tools/" in (tab.get("url") or "")
            ]
            if tools:
                self.coverage["external_tools"] = {
                    "status": "partial",
                    "count": len(tools),
                    "note": "derived from course tabs; launches need a browser session",
                }

        syllabus_html = course.get("syllabus_body")
        term = course.get("term") or {}

        document = {
            "subject": {
                "code": code,
                "name": name,
                "full_name": course.get("name"),
                "course_code": course.get("course_code"),
                "course_id": self.course_id,
                "session": term.get("name"),
                "start_at": course.get("start_at"),
                "end_at": course.get("end_at"),
                "url": self.web_url,
                "teachers": [t.get("display_name") for t in course.get("teachers") or []],
                "syllabus": self._markdown(syllabus_html),
                "syllabus_links": self._links(syllabus_html),
            },
            "assessments": assessments,
            "quizzes": quizzes,
            "modules": modules,
            "pages": pages,
            "announcements": announcements,
            "discussions": discussions,
            "files": files,
            "external_tools": tools,
            "tabs": tabs,
            "links": self._all_links(assessments, quizzes, modules, pages, announcements, syllabus_html),
            "_meta": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "canvas_host": self.client.base_url,
                "timezone": str(self.tz),
                "grading_rule": grading_rule,
                "files_downloaded": self.download,
                "text_extracted": self.extract,
            "ocr": self.ocr,
                "coverage": self.coverage,
            },
        }
        document["overview_markdown"] = self._overview(document)
        return document

    # ------------------------------------------------------------- components

    def _assessments(self, groups: list[dict], details: dict) -> list[dict]:
        out = []
        for group in groups:
            for assignment in group.get("assignments", []):
                detail = details.get(str(assignment.get("id"))) or {}
                # Prefer whichever copy actually carries a description.
                description = assignment.get("description") or detail.get("description")
                submission = detail.get("submission") or {}
                all_dates = detail.get("all_dates") or []
                due_at = assignment.get("due_at") or detail.get("due_at") or next(
                    (d["due_at"] for d in all_dates if d.get("due_at")), None
                )
                out.append(
                    {
                        "id": str(assignment.get("id")),
                        "name": assignment.get("name"),
                        "group": group.get("name"),
                        "group_weight_pct": group.get("group_weight"),
                        "weight_pct": assignment.get("weight_pct"),
                        "points_possible": assignment.get("points_possible"),
                        "due_at": due_at,
                        "due_at_local": to_local(due_at, self.tz),
                        "all_dates": [
                            {
                                "title": d.get("title"),
                                "base": d.get("base"),
                                "due_at": d.get("due_at"),
                                "due_at_local": to_local(d.get("due_at"), self.tz),
                            }
                            for d in all_dates
                        ],
                        "unlock_at": assignment.get("unlock_at") or detail.get("unlock_at"),
                        "lock_at": assignment.get("lock_at") or detail.get("lock_at"),
                        "submission_types": assignment.get("submission_types") or [],
                        "is_group_work": bool(assignment.get("group_category_id")),
                        "allowed_extensions": assignment.get("allowed_extensions") or [],
                        "grading_type": assignment.get("grading_type"),
                        "published": assignment.get("published"),
                        "brief": self._markdown(description),
                        "rubric": self._rubric(assignment),
                        "content_status": self._content_status(
                            assignment, description, self._rubric(assignment)),
                        "links": self._links(description),
                        "attachments": self._attachments_for(description),
                        "my_submission": {
                            "workflow_state": submission.get("workflow_state"),
                            "submitted_at": submission.get("submitted_at"),
                            "score": submission.get("score"),
                            "grade": submission.get("grade"),
                            "late": submission.get("late"),
                        }
                        if submission
                        else None,
                        "url": assignment.get("html_url"),
                    }
                )
        out.sort(key=lambda a: (a["due_at"] or "9999", a["name"] or ""))
        return out

    @staticmethod
    def _content_status(assignment: dict, description: str | None, rubric: list) -> dict:
        """Say why a brief or rubric is empty, rather than leaving a bare "".

        An empty string is ambiguous on its own — Canvas may genuinely hold
        nothing, or the real brief may sit behind an LTI this token can't reach.
        A model reading the file needs that distinction before it answers
        "what's the rubric for this?" with "there isn't one".
        """
        name = assignment.get("name") or ""
        # submission_types == ["none"] means the row exists only to carry a mark;
        # the actual task is described somewhere Canvas isn't.
        placeholder = "none" in (assignment.get("submission_types") or [])
        elsewhere = []
        if re.match(r"^\s*\[\s*ed\s*\]", name, re.I):
            elsewhere.append("Ed (the assignment name is prefixed [ed])")
        if placeholder and not description:
            elsewhere.append("the subject outline / Subject Information LTI")
        return {
            "brief": "present" if description else "absent_in_canvas",
            "rubric": "present" if rubric else "absent_in_canvas",
            "gradebook_placeholder": placeholder,
            "likely_source": elsewhere,
        }

    @staticmethod
    def _rubric(assignment: dict) -> list[dict]:
        rubric = assignment.get("rubric") or []
        return [
            {
                "criterion": c.get("description"),
                "points": c.get("points"),
                "detail": c.get("long_description"),
                "ratings": [
                    {"label": r.get("description"), "points": r.get("points"),
                     "detail": r.get("long_description")}
                    for r in c.get("ratings") or []
                ],
            }
            for c in rubric
        ]

    def _page_record(self, stub: dict, body: str | None) -> dict:
        slug = stub.get("url")
        return {
            "title": stub.get("title"),
            "url_slug": slug,
            "updated_at": stub.get("updated_at"),
            "published": stub.get("published"),
            "content": self._markdown(body),
            "links": self._links(body),
            # Same treatment as an assessment brief: a page that says "read the
            # attached PDF" is useless without the PDF. When the Files tab is
            # hidden these are the only route to slides and readings.
            "attachments": self._attachments_for(body),
            "url": stub.get("html_url") or f"{self.web_url}/pages/{slug}",
        }

    def _fetch_page(self, slug: str) -> dict | None:
        try:
            full, _ = self.client.get(self._course_path(f"pages/{quote(str(slug))}"))
        except CanvasError:
            return None
        return self._page_record(full, full.get("body"))

    def _pages(self, fallback_slugs: list[str]) -> list[dict]:
        """Every page body, with a fallback for a disabled Pages index.

        Staff can switch off the Pages tab while leaving individual pages
        readable. Module items still name them, so the content is recoverable
        one slug at a time. Sets its own coverage entry rather than going
        through _safe, because "partial" is a real outcome here.
        """
        try:
            stubs = self.client.get_list(self._course_path("pages"))
        except CanvasError as exc:
            pages = [p for p in (self._fetch_page(s) for s in fallback_slugs) if p]
            self.coverage["pages"] = {
                "status": "partial" if pages else "forbidden",
                "count": len(pages),
                "note": f"index unavailable ({exc.message[:80]}); "
                        f"recovered {len(pages)}/{len(fallback_slugs)} via module items",
            }
            return pages

        pages = []
        for stub in stubs:
            slug = stub.get("url")
            body = None
            if slug:
                try:
                    full, _ = self.client.get(self._course_path(f"pages/{quote(str(slug))}"))
                    body = full.get("body")
                except CanvasError:
                    body = None
            pages.append(self._page_record(stub, body))
        self.coverage["pages"] = {"status": "ok", "count": len(pages)}
        return pages

    @staticmethod
    def _link_pages(modules: list[dict], pages_by_url: dict) -> None:
        """Point each module item at its page instead of copying the body in.

        `resolved` says whether the pointer actually lands in pages[]: an item
        can name a page that was never retrieved, and a dangling pointer should
        be visible rather than look like an empty page.
        """
        for module in modules:
            for item in module["items"]:
                slug = item.get("page_url")
                if slug:
                    item["resolved"] = slug in pages_by_url

    def _quizzes(self) -> list[dict]:
        out = []
        for quiz in self.client.get_list(self._course_path("quizzes")):
            description = quiz.get("description")
            out.append(
                {
                    "id": str(quiz.get("id")),
                    "title": quiz.get("title"),
                    "quiz_type": quiz.get("quiz_type"),
                    "points_possible": quiz.get("points_possible"),
                    "question_count": quiz.get("question_count"),
                    "time_limit_minutes": quiz.get("time_limit"),
                    "allowed_attempts": quiz.get("allowed_attempts"),
                    "shuffle_answers": quiz.get("shuffle_answers"),
                    "due_at": quiz.get("due_at"),
                    "due_at_local": to_local(quiz.get("due_at"), self.tz),
                    "unlock_at": quiz.get("unlock_at"),
                    "lock_at": quiz.get("lock_at"),
                    "description": self._markdown(description),
                    "links": self._links(description),
                    "url": quiz.get("html_url"),
                }
            )
        return out

    def _modules(self) -> list[dict]:
        modules = []
        for module in self.client.get_list(self._course_path("modules"), {"include[]": ["items"]}):
            items = []
            for item in module.get("items") or []:
                record = {
                    "title": item.get("title"),
                    "type": item.get("type"),
                    "indent": item.get("indent"),
                    "url": item.get("html_url") or item.get("external_url"),
                }
                if item.get("type") == "Page":
                    # Canvas omits page_url on some instances; its slug is the
                    # slugified title, which is what the direct fetch needs.
                    record["page_url"] = item.get("page_url") or slugify(item.get("title") or "")
                elif item.get("type") == "ExternalUrl":
                    record["external_url"] = item.get("external_url")
                elif item.get("type") in ("Assignment", "Quiz", "File", "Discussion"):
                    record["content_id"] = str(item.get("content_id"))
                items.append(record)
            modules.append(
                {
                    "name": module.get("name"),
                    "position": module.get("position"),
                    "unlock_at": module.get("unlock_at"),
                    "published": module.get("published"),
                    "items": items,
                }
            )
        modules.sort(key=lambda m: m.get("position") or 0)
        return modules

    def _announcements(self) -> list[dict]:
        topics = self.client.get_list(
            self._course_path("discussion_topics"), {"only_announcements": "true"}
        )
        return [
            {
                "title": t.get("title"),
                "posted_at": t.get("posted_at"),
                "posted_at_local": to_local(t.get("posted_at"), self.tz),
                "author": (t.get("author") or {}).get("display_name"),
                "content": self._markdown(t.get("message")),
                "links": self._links(t.get("message")),
                "url": t.get("html_url"),
            }
            for t in topics
        ]

    def _discussions(self) -> list[dict]:
        topics = self.client.get_list(self._course_path("discussion_topics"))
        return [
            {
                "title": t.get("title"),
                "posted_at": t.get("posted_at"),
                "reply_count": t.get("discussion_subentry_count"),
                "content": self._markdown(t.get("message")),
                "url": t.get("html_url"),
            }
            for t in topics
            if not t.get("is_announcement")
        ]

    def _all_links(self, assessments, quizzes, modules, pages, announcements, syllabus_html) -> list[dict]:
        """One deduped index of every link found anywhere in the subject."""
        seen: dict[str, dict] = {}

        def add(links, source):
            for link in links or []:
                url = link["url"]
                if url not in seen:
                    seen[url] = {**link, "sources": [source]}
                elif source not in seen[url]["sources"]:
                    seen[url]["sources"].append(source)

        add(self._links(syllabus_html), "syllabus")
        for item in assessments:
            add(item["links"], f"assessment:{item['name']}")
        for quiz in quizzes:
            add(quiz["links"], f"quiz:{quiz['title']}")
        for page in pages:
            add(page["links"], f"page:{page['title']}")
        for module in modules:
            for item in module["items"]:
                add(item.get("links"), f"module:{module['name']}")
        for note in announcements:
            add(note["links"], f"announcement:{note['title']}")
        return list(seen.values())

    # --------------------------------------------------------------- overview

    def _overview(self, document: dict) -> str:
        subject = document["subject"]
        header = f"# {subject['code'] or ''} {subject['name']}".strip()
        lines = [header]
        if subject.get("session"):
            lines.append(f"_{subject['session']}_")
        lines.append(f"\nCanvas: {subject['url']}")
        if subject.get("teachers"):
            lines.append(f"Staff: {', '.join(t for t in subject['teachers'] if t)}")

        lines.append("\n## Assessment schedule\n")
        if document["assessments"]:
            lines.append("| Assessment | Weight | Due (local) | Submission |")
            lines.append("| --- | --- | --- | --- |")
            for item in document["assessments"]:
                weight = f"{item['weight_pct']}%" if item["weight_pct"] is not None else "—"
                due = (item["due_at_local"] or "—")[:16].replace("T", " ")
                kind = ", ".join(item["submission_types"]) or "—"
                lines.append(f"| {item['name']} | {weight} | {due} | {kind} |")
            total = sum(i["weight_pct"] or 0 for i in document["assessments"])
            lines.append(f"\nWeights total {round(total, 1)}% "
                         f"(rule: {document['_meta']['grading_rule']}).")
            content = document["_meta"]["coverage"].get("assessment_content") or {}
            if content.get("status") not in (None, "ok"):
                lines.append(f"\n> **Briefs and criteria:** {content['note']}")
        else:
            lines.append("_No assessments found via the API._")

        if document.get("quizzes"):
            lines.append("\n## Quizzes\n")
            for quiz in document["quizzes"]:
                bits = [f"**{quiz['title']}**"]
                if quiz.get("question_count"):
                    bits.append(f"{quiz['question_count']} questions")
                if quiz.get("time_limit_minutes"):
                    bits.append(f"{quiz['time_limit_minutes']} min")
                if quiz.get("due_at_local"):
                    bits.append(f"due {quiz['due_at_local'][:16].replace('T', ' ')}")
                lines.append("- " + " — ".join(bits))

        if document["modules"]:
            lines.append("\n## Module structure\n")
            for module in document["modules"]:
                lines.append(f"- **{module['name']}** — {len(module['items'])} items")

        gaps = [f"{k} ({v.get('status')})" for k, v in document["_meta"]["coverage"].items()
                if v.get("status") != "ok"]
        if gaps:
            lines.append("\n## Not retrieved\n")
            lines.append("These sections could not be read, so their absence below "
                         "does not mean the subject has none: " + ", ".join(gaps) + ".")

        return "\n".join(lines)
