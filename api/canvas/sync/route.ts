// POST /api/canvas/sync
// Pulls courses/assignments from Canvas and upserts into `subjects` + `calendar_events`.
import { NextResponse } from "next/server";

export async function POST(request: Request) {
  // TODO: fetch from lib/canvas, write via lib/supabase/server
  return NextResponse.json({ status: "not implemented" }, { status: 501 });
}
