# SIS — Canvas subject scraper

Pulls every subject the token owner is enrolled in and writes one self-contained
JSON document per subject, shaped for use as LLM context.

## Setup

```bash
cd SIS
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — put your Canvas host and a freshly generated token in it
```

`.env` is gitignored. Keep it that way.

## Run

```bash
python scrape.py                        # active subjects, links only
python scrape.py --download --extract   # also pull files and read their text
python scrape.py --include-concluded    # past sessions too
python scrape.py --session 'Autumn 2026'  # a specific session
python scrape.py --all-sessions         # every session you're enrolled in
python scrape.py --courses 41052 41201  # only these subject codes
python scrape.py --ocr                  # also OCR scanned PDFs (macOS)
python scrape.py --drop-boilerplate     # remove UTS template pages
python scrape.py --refresh              # ignore the local response cache
```

By default only the **current session** is scraped, and courses with no
subject code (induction modules, academic integrity, org sites) are skipped.
The chosen session is printed so it isn't silent — override with `--session`,
or turn both filters off with `--all-sessions` and `--include-non-subjects`.
Naming subjects explicitly with `--courses` bypasses the filtering entirely.

Output lands in `out/`:

```
out/
  index.json                       # every subject, coverage gaps, and the session choice
  41052-advanced-algorithms.json   # one file per subject
  files/41052-advanced-algorithms/ # only with --download
```

Raw API responses are cached in `.cache/`, so re-running is fast and doesn't
re-hammer Canvas. Delete the folder or pass `--refresh` to force a fresh pull.
Downloaded files are skipped when a local copy already matches the size Canvas
reports, so a re-run with `--download --extract` costs seconds rather than
pulling the whole course library again.

## What each subject file contains

| Key | What's in it |
| --- | --- |
| `subject` | code, name, session, teachers, syllabus as markdown |
| `assessments` | weight, due dates (incl. section overrides), full brief, rubric, links, attachments, your submission, and `content_status` saying why a brief or rubric is empty |
| `quizzes` | question count, time limit, attempts, description |
| `modules` | week structure; items point at their page via `page_url` + `resolved` |
| `pages` | every page body as markdown, with its attachments resolved and extracted |
| `announcements` / `discussions` | posts as markdown |
| `files` | metadata, plus local path and extracted text with `--extract` |
| `external_tools` | Echo360, Turnitin and friends |
| `links` | one deduped index of every link found anywhere, with its source |
| `overview_markdown` | generated summary — assessment table, module list, gaps |
| `_meta` | fetch time, grading rule used, coverage |

All text fields are markdown inside JSON strings, so headings and tables
survive. Marking criteria are nearly always tables.

## Keeping the JSON worth its tokens

Four things stop the files filling with text that costs context and returns
nothing:

**Pages are stored once.** Module items carry `page_url` and `resolved` rather
than a second copy of the body — inlining doubled every page. Resolve a pointer
by looking its slug up in `pages[]`; `resolved: false` means the item names a
page that wasn't retrieved.

**Identical files are extracted once.** Staff re-upload the same document as
`(1)`, `(2)-1` and so on. Files with matching `sha256` get `duplicate_of`
pointing at the first copy, and only that copy carries the text.

**Template pages are flagged.** UTS ships the same filler into every subject
shell. They're detected by body, not title — URLs stripped first, since Canvas
rewrites them per course — because titles lie: `Assessment overview` repeats
across subjects too, and in 41052 it is the only place the weights and dates
exist. Matching pages get `boilerplate: true`, so you can filter at prompt time.
`--drop-boilerplate` removes them instead, and any module item that named one
then reads `resolved: false`.

**Scanned PDFs can be OCR'd.** `--ocr` renders pages that have no text layer and
runs them through the macOS Vision recogniser — pip-only, no Homebrew. Recovered
text is marked `"extractor": "macos-vision-ocr"`. Off by default because it is
slow and macOS-only.

Spreadsheets are dumped in full, deliberately: every row stays queryable from
the JSON rather than needing the file opened.

## Which session gets scraped

By default the scraper picks the term whose date window contains right now, and
records the evidence in `index.json` under `session_selection`:

```json
{"name": "Spring 2026 (City campus)", "basis": "term_dates",
 "start_at": "2026-07-19T14:00:00+00:00", "end_at": "2027-01-04T13:00:00+00:00",
 "as_of": "2026-09-03T11:26:51+00:00", "verified": true}
```

`basis` says how the choice was made, which matters because the three cases are
not equally trustworthy:

- **`term_dates`** — a term's window contains now. `verified: true`. The normal case.
- **`name_majority`** — no term published any dates, so the term shared by most of
  your subjects wins. `verified: false`, and the run prints a warning, because a
  corpus built from a guess shouldn't look like one built from a check.
- **`explicit`** — you passed `--session`. Honoured either way, but still checked
  against the clock and marked unverified if today falls outside it.

If the terms *do* have dates and none of them contain today, you're between
sessions. The run stops with a non-zero exit rather than quietly scraping the
term that happens to be nearest — `--session` or `--all-sessions` overrides it.

## Two things that stop the output being quietly wrong

**Grading rule.** Canvas has two ways of turning points into a final mark. If
`apply_assignment_group_weights` is on, group weights are authoritative and
assignments split their group's weight by points. If it's off, weight is just
points over the course total. The rule actually used is recorded in
`_meta.grading_rule`.

**Disabled Pages index.** Staff can switch off the Pages tab while leaving
individual pages readable. When the index 403s, the scraper falls back to the
page slugs named by module items and fetches them one at a time. Coverage is
marked `partial` rather than `ok`, with a note saying how many were recovered.

**Empty briefs and rubrics.** Some subjects use Canvas assignments as bare
gradebook columns (`submission_types: ["none"]`, no description, no rubric) and
keep the real brief in Ed or the subject outline LTI. `/assignments` still
returns 200, so endpoint-level coverage can't see it — an empty `brief` looks
the same whether Canvas held nothing or we failed to read it. Every assessment
therefore carries a `content_status`:

```json
"content_status": {
  "brief": "absent_in_canvas", "rubric": "absent_in_canvas",
  "gradebook_placeholder": true,
  "likely_source": ["Ed (the assignment name is prefixed [ed])",
                    "the subject outline / Subject Information LTI"]
}
```

rolled up into `_meta.coverage.assessment_content` and stated in prose at the
top of `overview_markdown`. Note that `/courses/:id/rubrics` is teacher-only —
a student token gets 403 — so a rubric attached at course level rather than to
the assignment is unreadable, and the note says so rather than implying there
is none.

**Coverage.** If a lecturer hides the Files tab, that endpoint 403s. Rather than
dying or silently emitting an empty list, `_meta.coverage` records what was
attempted and what came back, so "this subject has no readings" stays
distinguishable from "we couldn't read them". `overview_markdown` names the
gaps in prose too. Statuses are `ok`, `partial` (recovered by a fallback, with a
note saying how), `forbidden` (403 — hidden or restricted), `disabled` (staff
switched the tab off, which is a fact about the subject rather than a failure to
read it) and `error` (anything genuinely unexpected, with the message).

## Limits

- Text extraction is blind to diagrams, figures and equations rendered as
  images. On lecture slides that can be most of the meaning — treat the
  extracted text as a searchable index, not a replacement for the file.
- Scanned PDFs have no text layer. They're flagged as such unless you pass
  `--ocr`, which is macOS-only and adds a minute or so per scanned file.
- LTI tools (Ed, Turnitin, the UTS subject outline) sit behind a browser
  session. Their launch URLs are captured; their content is not reachable
  with an API token, so briefs that live in Ed stay out of the JSON.
- Personal access tokens can't be shared. If this ever backs a multi-user app,
  you need an OAuth2 developer key from the Canvas admins instead.

## Tests

```bash
python selftest.py
```

Runs the HTML conversion, weight computation and a full subject build against a
mock Canvas. No network, no token needed.
