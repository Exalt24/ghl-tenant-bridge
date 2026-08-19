/**
 * supabase.ts -- client factories, split by trust level.
 *
 * The split is the point. There are two clients and mixing them up is how a
 * multi-tenant app leaks:
 *
 *   browserClient()  anon key, RLS APPLIES. Safe to ship to a phone.
 *   serviceClient()  service-role key, RLS IS BYPASSED ENTIRELY. Server only.
 *
 * The service-role key must never reach anything client-reachable, so this module
 * throws if the service client is constructed outside a server context rather than
 * relying on a convention nobody enforces. NEXT_PUBLIC_ prefixed vars are compiled
 * into the browser bundle by definition, which is why the service key deliberately
 * does NOT carry that prefix.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(
      `${name} is not set. Copy .env.example to .env.local and fill it in.`
    );
  }
  return value;
}

/**
 * Anon-key client. RLS applies, so this is the client whose behaviour the negative
 * tests care about: an anonymous caller must see zero rows.
 */
export function browserClient(): SupabaseClient {
  return createClient(
    required("NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL),
    required("NEXT_PUBLIC_SUPABASE_ANON_KEY", process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY),
    {
      auth: {
        // The queue survives page reloads, so the session has to as well, otherwise a
        // flush after a reload has no identity and every write is rejected by RLS.
        persistSession: true,
        autoRefreshToken: true,
      },
    }
  );
}

/**
 * Service-role client. BYPASSES RLS, so every tenant decision it makes must be
 * explicit in code. Used only by the webhook receiver, which resolves a GHL
 * location_id to an org_id itself before writing anything.
 */
export function serviceClient(): SupabaseClient {
  if (typeof window !== "undefined") {
    throw new Error(
      "serviceClient() was called in a browser context. The service-role key bypasses " +
        "RLS and must never be shipped to a client."
    );
  }
  return createClient(
    required("NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL),
    required("SUPABASE_SERVICE_ROLE_KEY", process.env.SUPABASE_SERVICE_ROLE_KEY),
    { auth: { persistSession: false, autoRefreshToken: false } }
  );
}

/** True when the env is complete enough to talk to Supabase at all. */
export function supabaseConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  );
}
