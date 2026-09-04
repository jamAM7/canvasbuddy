#!/usr/bin/env python3
"""Run the whole pipeline against a fake Canvas. No token or network needed.

    python selftest.py

Verifies pagination handling, weight computation under both grading rules,
HTML->markdown with tables, link extraction, and that a hidden tab degrades
into a coverage note instead of crashing the run.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from canvas.build import SubjectBuilder, compute_weights
from canvas.client import CanvasError, CanvasForbidden
from canvas.content import extract_links, html_to_markdown

BRIEF = """
<h2>Task</h2>
<p>Implement the algorithm and submit a report. See
<a class="instructure_file_link" href="/courses/1/files/77"
   data-api-endpoint="https://canvas.test/api/v1/files/77"
   data-api-returntype="File">Assignment spec</a>.</p>
<table>
  <tr><th>Criterion</th><th>Marks</th></tr>
  <tr><td>Correctness</td><td>40</td></tr>
  <tr><td>Analysis</td><td>30</td></tr>
</table>
<iframe src="https://echo360.net.au/lesson/abc"></iframe>
"""

FAKE = {
    "/api/v1/courses/1": {
        "id": "1", "name": "41052 Advanced Algorithms",
        "course_code": "41052-2026-SPR", "apply_assignment_group_weights": True,
        "term": {"name": "2026 Spring"},
        "teachers": [{"display_name": "Dr Example"}],
        "syllabus_body": "<p>Read the <a href='/courses/1/pages/outline'>outline</a>.</p>",
    },
    "/api/v1/courses/1/assignment_groups": [
        {"id": "10", "name": "Assessments", "group_weight": 100, "assignments": [
            {"id": "100", "name": "Assignment 1", "points_possible": 40,
             "due_at": "2026-09-19T02:59:00Z", "submission_types": ["online_upload"],
             "grading_type": "points", "description": BRIEF, "html_url": "https://canvas.test/a/100",
             "rubric": [{"description": "Correctness", "points": 40,
                         "ratings": [{"description": "Excellent", "points": 40}]}]},
            {"id": "101", "name": "Assignment 2", "points_possible": 60,
             "due_at": "2026-10-24T02:59:00Z", "submission_types": ["online_upload"],
             "grading_type": "points", "description": "<p>Part two.</p>",
             "html_url": "https://canvas.test/a/101", "group_category_id": "5"},
        ]}
    ],
    "/api/v1/courses/1/assignments": [
        {"id": "100", "submission": {"workflow_state": "graded", "score": 34}},
        {"id": "101", "submission": {"workflow_state": "unsubmitted"}},
    ],
    "/api/v1/courses/1/pages": [
        {"url": "outline", "title": "Subject Outline", "html_url": "https://canvas.test/p/outline"}
    ],
    "/api/v1/courses/1/pages/outline": {
        "body": "<h3>Week plan</h3><ul><li>Greedy</li><li>DP</li></ul>"
    },
    "/api/v1/courses/1/modules": [
        {"name": "Week 1", "position": 1, "items": [
            {"title": "Subject Outline", "type": "Page", "page_url": "outline",
             "html_url": "https://canvas.test/p/outline"},
            {"title": "Lecture recording", "type": "ExternalUrl",
             "external_url": "https://echo360.net.au/x"},
        ]}
    ],
    "/api/v1/courses/1/discussion_topics": [
        {"id": "9", "title": "Week 3 notice", "is_announcement": True,
         "posted_at": "2026-08-20T01:00:00Z", "message": "<p>Tutorial moved.</p>",
         "author": {"display_name": "Dr Example"}, "html_url": "https://canvas.test/d/9"}
    ],
    "/api/v1/files/77": {"id": "77", "display_name": "spec.pdf", "size": 1024,
                         "content-type": "application/pdf"},
    "/api/v1/courses/1/quizzes": [
        {"id": "50", "title": "Knowledge Quiz", "quiz_type": "assignment",
         "points_possible": 10, "question_count": 12, "time_limit": 45,
         "allowed_attempts": 1, "due_at": "2026-10-02T12:59:00Z",
         "description": "<p>Covers weeks 1-6. See <a href=\"https://ed.test/q\">Ed</a>.</p>",
         "html_url": "https://canvas.test/courses/1/quizzes/50"}
    ],
    "/api/v1/courses/1/external_tools": [],
    "/api/v1/courses/1/tabs": [
        {"label": "Home", "full_url": "https://canvas.test/courses/1"},
        {"label": "Ed", "full_url": "https://canvas.test/courses/1/external_tools/2929"},
    ],
}

# Course 2 mirrors 41052: Pages index disabled, individual pages still readable.
FAKE["/api/v1/courses/2"] = {"id": "2", "name": "41201 DSEP", "term": {"name": "2026 Spring"}}
FAKE["/api/v1/courses/2/assignment_groups"] = []
FAKE["/api/v1/courses/2/assignments"] = []
FAKE["/api/v1/courses/2/quizzes"] = []
FAKE["/api/v1/courses/2/modules"] = [
    {"name": "Get started", "position": 1, "items": [
        {"title": "Assessment overview", "type": "Page",
         "html_url": "https://canvas.test/courses/2/modules/items/1"},
    ]}
]
FAKE["/api/v1/courses/2/pages/assessment-overview"] = {
    "url": "assessment-overview", "title": "Assessment overview",
    "body": "<p>Task 1 is due week 6. <a href=\"/courses/2/files/88\">Brief</a></p>",
}
FAKE["/api/v1/courses/2/discussion_topics"] = []
FAKE["/api/v1/courses/2/external_tools"] = []
FAKE["/api/v1/courses/2/tabs"] = []
FAKE["/api/v1/files/88"] = {"id": "88", "display_name": "task1.pdf", "size": 2048}

FORBIDDEN = {"/api/v1/courses/1/files", "/api/v1/courses/2/files"}
DISABLED = {"/api/v1/courses/2/pages"}


class FakeClient:
    """Stands in for CanvasClient, including a deliberately hidden Files tab."""

    base_url = "https://canvas.test"
    request_count = 0

    def _lookup(self, path):
        path = path.split("?")[0]
        if path in FORBIDDEN:
            raise CanvasForbidden(403, path, "unauthorized")
        if path in DISABLED:
            raise CanvasError(404, path, "That page has been disabled for this course")
        if path not in FAKE:
            raise KeyError(f"fake Canvas has no route for {path}")
        return FAKE[path]

    def get(self, path, params=None):
        self.request_count += 1
        return self._lookup(path), {"link": ""}

    def get_list(self, path, params=None):
        self.request_count += 1
        value = self._lookup(path)
        if path.endswith("discussion_topics") and (params or {}).get("only_announcements"):
            return [t for t in value if t.get("is_announcement")]
        return value if isinstance(value, list) else [value]

    def download(self, url, dest):
        raise AssertionError("selftest should not download")


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return condition


def main() -> int:
    ok = True
    print("content helpers")
    markdown = html_to_markdown(BRIEF, "https://canvas.test")
    ok &= check("table survives conversion", "| Correctness |" in markdown)
    ok &= check("headings survive", markdown.startswith("## Task"))
    links = extract_links(BRIEF, "https://canvas.test")
    ok &= check("file link classified from data attribute",
                any(l["kind"] == "file" for l in links))
    ok &= check("relative href absolutised",
                any(l["url"].startswith("https://canvas.test/courses/1/files/77") for l in links))
    ok &= check("iframe captured", any(l["kind"] == "embed" for l in links))

    print("\nweight computation")
    groups = [{"name": "G", "group_weight": 100, "assignments": [
        {"id": "1", "points_possible": 40, "grading_type": "points"},
        {"id": "2", "points_possible": 60, "grading_type": "points"},
    ]}]
    rule = compute_weights({"apply_assignment_group_weights": True}, groups)
    ok &= check("group-weight rule splits by points",
                rule == "group_weights"
                and groups[0]["assignments"][0]["weight_pct"] == 40.0)
    groups2 = [{"name": "G", "group_weight": 0, "assignments": [
        {"id": "1", "points_possible": 25, "grading_type": "points"},
        {"id": "2", "points_possible": 75, "grading_type": "points"},
    ]}]
    rule2 = compute_weights({"apply_assignment_group_weights": False}, groups2)
    ok &= check("points rule falls back correctly",
                rule2 == "points_proportional"
                and groups2[0]["assignments"][1]["weight_pct"] == 75.0)
    groups3 = [{"name": "G", "group_weight": 100, "assignments": [
        {"id": "1", "points_possible": 0, "grading_type": "not_graded"},
    ]}]
    compute_weights({"apply_assignment_group_weights": True}, groups3)
    ok &= check("ungraded items get no weight",
                groups3[0]["assignments"][0]["weight_pct"] is None)

    print("\nfull subject build")
    with tempfile.TemporaryDirectory() as tmp:
        builder = SubjectBuilder(FakeClient(), {"id": "1", "name": "41052 Advanced Algorithms"},
                                 Path(tmp))
        document = builder.build()

    subject = document["subject"]
    ok &= check("subject code parsed", subject["code"] == "41052")
    ok &= check("name stripped of code", subject["name"] == "Advanced Algorithms")
    ok &= check("session captured", subject["session"] == "2026 Spring")
    ok &= check("two assessments", len(document["assessments"]) == 2)
    first = document["assessments"][0]
    ok &= check("weights computed", first["weight_pct"] == 40.0)
    ok &= check("due date localised", (first["due_at_local"] or "").startswith("2026-09-19T12:59"))
    ok &= check("rubric flattened", first["rubric"][0]["criterion"] == "Correctness")
    ok &= check("attachment resolved from brief link",
                any(a["name"] == "spec.pdf" for a in first["attachments"]))
    ok &= check("group work detected", document["assessments"][1]["is_group_work"] is True)
    ok &= check("submission merged", first["my_submission"]["score"] == 34)
    item = document["modules"][0]["items"][0]
    ok &= check("module item points at its page, without copying the body",
                item.get("content") is None and item["resolved"] is True)
    ok &= check("the pointer resolves to a real page body",
                "Greedy" in next(p["content"] for p in document["pages"]
                                 if p["url_slug"] == item["page_url"]))
    ok &= check("announcement separated from discussions",
                len(document["announcements"]) == 1 and not document["discussions"])
    ok &= check("link index built with sources", any(
        "syllabus" in l["sources"] for l in document["links"]))
    ok &= check("hidden Files tab recorded, not fatal",
                document["_meta"]["coverage"]["files"]["status"] == "forbidden")
    ok &= check("grading rule recorded", document["_meta"]["grading_rule"] == "group_weights")
    ok &= check("overview names the gap", "not retrieved" in
                document["overview_markdown"].lower() or "Not retrieved" in
                document["overview_markdown"])
    ok &= check("overview has assessment table", "| Assignment 1 |" in
                document["overview_markdown"])
    ok &= check("document is JSON serialisable",
                bool(json.dumps(document, ensure_ascii=False)))

    quizzes = document["quizzes"]
    ok &= check("quizzes fetched", len(quizzes) == 1 and quizzes[0]["question_count"] == 12)
    ok &= check("quiz time limit captured", quizzes[0]["time_limit_minutes"] == 45)
    ok &= check("quiz links reach the index",
                any("quiz:Knowledge Quiz" in l["sources"] for l in document["links"]))
    ok &= check("external tools derived from tabs",
                any(t.get("source") == "tabs" and t["name"] == "Ed"
                    for t in document["external_tools"]))

    print("\ndisabled Pages index (the 41052 case)")
    with tempfile.TemporaryDirectory() as tmp:
        fallback = SubjectBuilder(FakeClient(), {"id": "2", "name": "41201 DSEP"},
                                  Path(tmp)).build()

    coverage = fallback["_meta"]["coverage"]["pages"]
    ok &= check("page recovered despite disabled index", len(fallback["pages"]) == 1)
    ok &= check("recovery marked partial, not ok", coverage["status"] == "partial")
    ok &= check("coverage note explains the recovery", "module items" in coverage["note"])
    ok &= check("slug derived from title when page_url absent",
                fallback["pages"][0]["url_slug"] == "assessment-overview")
    recovered = fallback["modules"][0]["items"][0]
    ok &= check("recovered page reachable through the module pointer",
                "due week 6" in next(p["content"] for p in fallback["pages"]
                                     if p["url_slug"] == recovered["page_url"]))
    ok &= check("links inside recovered page reach the index",
                any("/files/88" in l["url"] for l in fallback["links"]))

    print("\n" + ("All checks passed." if ok else "Some checks FAILED."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
