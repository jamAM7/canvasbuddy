// POST /api/calendar/sync
// Subscribes to / refreshes external calendars (UTS iCal, etc.) and merges into `calendar_events`.
import { NextResponse } from "next/server";

export async function POST(request: Request) {
  // TODO: iCal/CalDAV fetch + merge
  return NextResponse.json({ status: "not implemented" }, { status: 501 });
}
