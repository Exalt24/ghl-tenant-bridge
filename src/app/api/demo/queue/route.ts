/**
 * POST /api/demo/queue -- the local demo sink for the offline queue.
 *
 * WHY THIS EXISTS, stated plainly so nobody mistakes it for product code.
 *
 * The capture page needs somewhere to actually SEND to, otherwise the queue fills and
 * never drains, and a demo of "offline capture" that cannot show the drain is showing
 * half the feature. Supabase credentials are optional in this repo, so without this
 * route the only honest demo is photos piling up forever.
 *
 * What makes this a real test rather than a prop: the full path executes. The outbox
 * reads bytes back out of OPFS or IndexedDB, POSTs them over HTTP, waits for an
 * acknowledgement, and only deletes the local copy once the response proves a row was
 * written. That is the same contract src/lib/outbox-transport-supabase.ts implements
 * against PostgREST, so the phantom guard is genuinely exercised here.
 *
 * It is deliberately NOT persistent. It counts what it received and throws the bytes
 * away, because storing a stranger's demo uploads on disk is a liability with no
 * upside. In production the Supabase transport is the real one.
 *
 * SAFETY: this route is a no-op sink and is refused entirely when NODE_ENV is
 * production unless DEMO_SINK is explicitly set, so it cannot become an accidental
 * open upload endpoint on a deployed host.
 */

import { NextResponse } from "next/server";

export const runtime = "nodejs";

/** Rolling in-memory tally, so the demo can prove receipt without keeping anything. */
let received = 0;
let bytesReceived = 0;

function demoEnabled(): boolean {
  if (process.env.DEMO_SINK === "1") return true;
  return process.env.NODE_ENV !== "production";
}

export async function POST(req: Request) {
  if (!demoEnabled()) {
    return NextResponse.json(
      { error: "demo sink disabled outside development" },
      { status: 404 }
    );
  }

  const clientUuid = req.headers.get("x-client-uuid") ?? "";
  if (!clientUuid) {
    // The idempotency key is mandatory. Accepting a write without one would let the
    // demo pass while the property that matters, exactly-once delivery, went untested.
    return NextResponse.json(
      { error: "x-client-uuid is required; it is the idempotency key" },
      { status: 400 }
    );
  }

  const body = await req.arrayBuffer();
  received += 1;
  bytesReceived += body.byteLength;

  // Shaped exactly like the Supabase transport's acknowledgement, because the outbox
  // treats rowsWritten < 1 as a FAILURE and keeps the local copy. Returning 0 here is
  // how you would simulate an RLS rejection.
  return NextResponse.json({
    clientUuid,
    bytesStored: body.byteLength,
    rowsWritten: 1,
    receivedSoFar: received,
    bytesSoFar: bytesReceived,
  });
}

export async function GET() {
  if (!demoEnabled()) {
    return NextResponse.json({ error: "demo sink disabled" }, { status: 404 });
  }
  return NextResponse.json({ received, bytesReceived });
}
