# canvasbuddy

A student app that syncs Canvas into one place: AI-generated notes as calendar

## Overview

StudyFlow pulls course content and deadlines from Canvas, turns them into organised per-subject/per-week notes and surfaces everything on a home dashboard.

## Core features

| Feature | Summary |
|---|---|
| **Notes** | Canvas content → AI-generated notes, organised per-subject / per-week, editable in a word-editor-style view |
| **Calendar** | Subscribes to UTS + external calendars; pulls assessments from Canvas; three object types — classes, assessment tasks, self/AI tasks; assessments auto-break into subtasks |
| **Dashboard** | Notifications, "due this week", quick links into notes and quizzes |
| **Settings** | Canvas connection, calendar subscriptions, preferences |
| **Sign up** | Single-role (student) auth for MVP |

## Tech stack

| Layer | Choice |
|---|---|
| Framework | **Next.js 14** (App Router, TypeScript) |
| Styling | Tailwind CSS |
| Database + Auth | **Supabase** (Postgres, Auth, Row Level Security, Storage) |
| Hosting | Vercel (app) + Supabase (managed Postgres) |
| AI | Pluggable via `lib/ai` — used for note generation |
| Canvas integration | Canvas LMS REST API via `lib/canvas` |

## Repo structure

```
.
├── app/                        # Next.js App Router
│   ├── (auth)/sign-up/         # Sign up page
│   ├── dashboard/              # Home dashboard
│   ├── notes/                  # Notes: subject list -> week detail
│   │   └── [subjectId]/[weekId]/
│   ├── calendar/               # Calendar view
│   ├── quiz/                   # Quiz list -> quiz detail
│   │   └── [quizId]/
│   ├── settings/                
│   └── api/                    # Backend logic (Route Handlers)
│       ├── canvas/sync/        # Pull courses/assignments from Canvas
│       ├── notes/generate/     # Canvas content -> AI notes
│       ├── quiz/generate/      # Notes -> AI quiz
│       └── calendar/sync/      # External calendar subscriptions
├── components/                 # Shared UI, grouped by feature
├── lib/
│   ├── supabase/                # Browser + server Supabase clients
│   ├── canvas/                  # Canvas API wrapper
│   └── ai/                      # AI provider wrapper (notes/quiz generation)
├── types/                       # Shared TypeScript types
├── supabase/
│   ├── migrations/               # SQL schema, source of truth for the DB
│   └── seed.sql
└── docs/
    ├── architecture.md
    ├── er-diagram.md
    ├── data-flow-canvas-sync.md
    └── mvp-scope.md
```

## Getting started

### Prerequisites
- Node.js 20+
- A [Supabase]() account
- A Canvas API token (Canvas → Account → Settings → New Access Token)

### Install

```bash
npm install
cp .env.example .env.local
```

### Environment variables

| Variable | Where to get it |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project → Settings → API |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase project → Settings → API |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase project → Settings → API (server-only, never expose client-side) |
| `CANVAS_BASE_URL` | Your institution's Canvas URL, e.g. `https://canvas.uts.edu.au` |
| `CANVAS_API_TOKEN` | Canvas → Account → Settings → New Access Token |
| `AI_API_KEY` | Your AI provider's API key |



