/**
 * outbox-transport-supabase.ts -- the real transport behind the offline queue.
 *
 * This is where the phantom guard in outbox.ts stops being theoretical. The
 * OutboxTransport contract demands proof that a write happened, and this file is
 * the implementation that has to actually supply it.
 *
 * MEASURED 2026-08-20 on Postgres 17 with `force row level security`
 * (tests/rls_phantom_proof.sql, re-runnable):
 *   * INSERT violating a WITH CHECK clause RAISES 42501. Loud, easy.
 *   * UPDATE or DELETE filtered out by a USING clause DOES NOT RAISE. It reports
 *     `UPDATE 0` and returns success, and the identical statement against a visible
 *     row reports `UPDATE 1`. The count is the only difference.
 *   * `UPDATE ... RETURNING id` on a hidden row returns ZERO ROWS.
 *
 * That last line is the whole design of this file. `.select()` after the upsert is
 * not decoration and must never be "optimised away": it is the only thing that
 * distinguishes a write that happened from one RLS silently discarded. Without it a
 * field photo gets deleted from the phone having never reached the server, and the
 * queue reads as fully synced forever.
 *
 * TWO MORE SUPABASE SPECIFICS ENCODED HERE
 * ----------------------------------------
 * 1. `onConflict` needs a UNIQUE INDEX, not merely a unique constraint, because
 *    PostgREST resolves the conflict target against an index.
 * 2. Signed upload URLs live about two hours, so they are minted at flush time and
 *    never before going offline. This transport uses the authenticated client
 *    directly rather than pre-minting anything.
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import type { OutboxItem, OutboxTransport, UploadAck, WriteAck } from "./outbox";

export interface SupabaseTransportOptions {
  client: SupabaseClient;
  /** Storage bucket for field photos. Must exist; creation is an ops task. */
  bucket: string;
  /** Table receiving queued rows. Needs a UNIQUE INDEX on client_uuid. */
  table: string;
}

/**
 * Deterministic object path derived from the item id.
 *
 * Deterministic on purpose: re-uploading after an interrupted flush overwrites the
 * same object instead of creating a second copy, which is what makes the byte
 * upload idempotent without any server-side dedup.
 */
export function objectPathFor(item: OutboxItem): string {
  const org = typeof item.payload.org_id === "string" ? item.payload.org_id : "unknown-org";
  return `${org}/${item.id}.jpg`;
}

export function createSupabaseOutboxTransport(
  opts: SupabaseTransportOptions
): OutboxTransport {
  const { client, bucket, table } = opts;

  return {
    async uploadBytes(item: OutboxItem, bytes: Uint8Array): Promise<UploadAck> {
      const path = objectPathFor(item);
      // upsert:true so a retry overwrites. Without it a second attempt fails with a
      // duplicate-object error and the item would retry forever on a write that
      // already succeeded.
      const { error } = await client.storage.from(bucket).upload(path, bytes, {
        contentType: "image/jpeg",
        upsert: true,
      });
      if (error) throw new Error(`storage upload failed: ${error.message}`);

      // Storage does not report a byte count on upload, so it is read back. That is
      // one extra round trip per photo and it is worth it: it is the only way to
      // catch a truncated object, and the alternative is trusting a 200 and deleting
      // the local copy of a half-written photo.
      const { data: listed, error: listErr } = await client.storage
        .from(bucket)
        .list(path.split("/")[0], { search: `${item.id}.jpg`, limit: 1 });
      if (listErr) throw new Error(`storage verify failed: ${listErr.message}`);

      const size = listed?.[0]?.metadata?.size;
      if (typeof size !== "number") {
        throw new Error("storage verify returned no size, cannot confirm the upload");
      }
      return { bytesStored: size };
    },

    async upsertRow(item: OutboxItem): Promise<WriteAck> {
      const row = {
        client_uuid: item.id,
        kind: item.kind,
        captured_at: new Date(item.capturedAt).toISOString(),
        object_path: item.bytesRef ? objectPathFor(item) : null,
        ...item.payload,
      };

      // `.select("client_uuid")` is the phantom guard. An RLS-rejected UPDATE returns
      // success with zero rows, so the returned array length is the ONLY evidence a
      // row was written. Removing this select would make every rejected write look
      // like a success and silently delete the local copy.
      const { data, error } = await client
        .from(table)
        .upsert(row, { onConflict: "client_uuid" })
        .select("client_uuid");

      if (error) throw new Error(`upsert failed: ${error.message}`);

      // Zero rows returned means the write did not land. Reporting the count rather
      // than throwing here keeps the decision in one place: outbox.flush() treats
      // zero as a failure, retains the bytes, and records why.
      return { rowsWritten: Array.isArray(data) ? data.length : 0 };
    },
  };
}

/**
 * Refresh the session ONCE before a flush.
 *
 * Passed as outbox `beforeFlush`. Supabase access tokens last an hour and refresh
 * tokens are single-use with a short reuse interval, so two contexts refreshing
 * inside that window can revoke the whole session. A field inspector signed out on a
 * roof cannot recover, so this is deliberately the only refresh in the flush path.
 *
 * It also swallows a network failure on purpose: being offline is the normal case
 * here, and treating "could not reach the auth server" as fatal would abort a flush
 * that was about to discover it has no connection anyway. A genuinely expired
 * session surfaces as a per-item failure instead, which retains the bytes.
 */
export function makeBeforeFlush(client: SupabaseClient): () => Promise<void> {
  return async () => {
    try {
      const { data } = await client.auth.getSession();
      const expiresAt = data.session?.expires_at;
      if (!expiresAt) return;
      const secondsLeft = expiresAt - Math.floor(Date.now() / 1000);
      // Only refresh when it is actually close to expiry. Refreshing on every flush
      // burns single-use refresh tokens for no reason and widens the reuse-race
      // window that revokes sessions.
      if (secondsLeft < 120) await client.auth.refreshSession();
    } catch {
      // Offline is expected. Let the per-item failures carry the signal.
    }
  };
}
