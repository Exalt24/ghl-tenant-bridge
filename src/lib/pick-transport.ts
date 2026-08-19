/**
 * pick-transport.ts -- choose the real Supabase transport when it can actually work,
 * and the local demo sink otherwise.
 *
 * WHY A CHOOSER RATHER THAN JUST HARD-WIRING SUPABASE
 *   The Supabase transport needs THREE things at once: env config, a signed-in session,
 *   and that user being a member of the org whose id goes on the row. is_org_member()
 *   resolves auth.uid() against org_members, so a signed-out visitor cannot write at
 *   all. Hard-wiring it would make the public capture page fail at the first photo with
 *   a policy error that reads like a client bug.
 *
 *   So the page uses Supabase the moment a real session exists, and the demo sink when
 *   it does not. Both satisfy the same OutboxTransport contract, including the
 *   proof-of-write guard, so the queue behaves identically either way.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO
 *   It never reads a password from env. NEXT_PUBLIC_* is shipped to the browser, so a
 *   demo credential there would be public. Sign-in is a user action.
 */
import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import type { OutboxTransport } from "./outbox";
import { createHttpOutboxTransport } from "./outbox-transport-http";
import {
  createSupabaseOutboxTransport,
  makeBeforeFlush,
} from "./outbox-transport-supabase";

export const FIELD_PHOTO_BUCKET = "field-photos";
export const FIELD_CAPTURE_TABLE = "field_captures";

export interface PickedTransport {
  transport: OutboxTransport;
  beforeFlush?: () => Promise<void>;
  /** Which backend is live, for honest on-screen labelling. */
  backend: "supabase" | "demo-sink";
  /** The org rows should be written to, or null when using the demo sink. */
  orgId: string | null;
  client: SupabaseClient | null;
}

/**
 * A browser Supabase client, or null when the project is not configured.
 *
 * MEMOISED, and that is not a micro-optimisation. The first version constructed a new
 * client on every call, including from render, and the browser console filled with
 * "Multiple GoTrueClient instances detected in the same browser context". Each instance
 * owns its own auth state and refresh timer against the SAME storage key, so two of
 * them can race on a single-use refresh token and revoke the session, which is exactly
 * the failure makeBeforeFlush() exists to avoid. One client per context, always.
 */
let cached: SupabaseClient | null | undefined;

export function browserSupabase(): SupabaseClient | null {
  if (cached !== undefined) return cached;
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  // A placeholder left in .env.example must not count as configured, otherwise the app
  // reports "supabase" and then fails every write.
  if (!url || !key || !url.startsWith("https://") || key.length < 40) {
    cached = null;
    return cached;
  }
  cached = createClient(url, key, {
    auth: { persistSession: true, autoRefreshToken: true },
  });
  return cached;
}

/**
 * Decide the transport.
 *
 * Resolving the org from org_members rather than trusting a value from the page is the
 * same rule the webhook path follows: the tenant comes from our own row, never from
 * the caller. A page-supplied org_id would be a cross-tenant write waiting to happen.
 */
export async function pickTransport(): Promise<PickedTransport> {
  const demo: PickedTransport = {
    transport: createHttpOutboxTransport({ endpoint: "/api/demo/queue" }),
    backend: "demo-sink",
    orgId: null,
    client: null,
  };

  const client = browserSupabase();
  if (!client) return demo;

  const { data } = await client.auth.getSession();
  if (!data.session) return { ...demo, client };

  const { data: rows, error } = await client
    .from("org_members")
    .select("org_id")
    .limit(1);
  const orgId = !error && rows?.length ? (rows[0].org_id as string) : null;

  // Signed in but a member of nothing: every write would be refused by WITH CHECK, so
  // the sink is the honest choice rather than a queue that can never drain.
  if (!orgId) return { ...demo, client };

  return {
    transport: createSupabaseOutboxTransport({
      client,
      bucket: FIELD_PHOTO_BUCKET,
      table: FIELD_CAPTURE_TABLE,
    }),
    beforeFlush: makeBeforeFlush(client),
    backend: "supabase",
    orgId,
    client,
  };
}
