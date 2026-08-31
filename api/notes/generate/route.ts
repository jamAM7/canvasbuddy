// POST /api/notes/generate
// Body: { subjectId, weekId, sourceContent }
// Calls lib/ai to turn Canvas content into structured notes.
import { NextResponse } from "next/server";

export async function POST(request: Request) {
  // TODO: call lib/ai generateNotes(), persist to `notes`
  return NextResponse.json({ status: "not implemented" }, { status: 501 });
}
