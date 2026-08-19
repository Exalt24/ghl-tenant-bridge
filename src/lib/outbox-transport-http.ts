/**
 * outbox-transport-http.ts -- a plain HTTP transport for the offline queue.
 *
 * Two jobs, both honest:
 *
 * 1. It is what the capture page uses when Supabase is not configured, so the queue
 *    can actually DRAIN. Without a transport the demo can only show photos piling up,
 *    which is half the feature.
 * 2. It is the reference implementation of the OutboxTransport contract for anyone
 *    whose backend is not Supabase. The Supabase version derives rowsWritten from a
 *    PostgREST `.select()`; this one derives it from the response body. Same rule
 *    either way: a 2xx is not proof, the COUNT is.
 *
 * The idempotency key travels as a header rather than only inside the body, so a
 * server can dedupe before it parses anything, and so a proxy log shows which record
 * a retry belongs to.
 */

import type { OutboxItem, OutboxTransport, UploadAck, WriteAck } from "./outbox";

export interface HttpTransportOptions {
  /** Endpoint receiving the bytes. Must echo bytesStored and rowsWritten. */
  endpoint: string;
  /** Extra headers, e.g. an auth token. */
  headers?: Record<string, string>;
  timeoutMs?: number;
}

interface Ack {
  bytesStored?: number;
  rowsWritten?: number;
}

export function createHttpOutboxTransport(opts: HttpTransportOptions): OutboxTransport {
  const send = async (item: OutboxItem, body: BodyInit, kind: string): Promise<Ack> => {
    const res = await fetch(opts.endpoint, {
      method: "POST",
      headers: {
        // The idempotency key, minted at capture. A retry carries the same one.
        "x-client-uuid": item.id,
        "x-outbox-kind": kind,
        "content-type": "application/octet-stream",
        ...(opts.headers ?? {}),
      },
      body,
      signal: AbortSignal.timeout(opts.timeoutMs ?? 30_000),
    });
    if (!res.ok) throw new Error(`${kind} -> HTTP ${res.status}`);
    // A response that is not JSON is a failure, not an empty success. Treating an
    // unparseable body as "probably fine" is how a queue deletes a record that a proxy
    // swallowed.
    let ack: Ack;
    try {
      ack = (await res.json()) as Ack;
    } catch {
      throw new Error(`${kind} -> response was not JSON, cannot confirm the write`);
    }
    return ack;
  };

  return {
    async uploadBytes(item, bytes): Promise<UploadAck> {
      // A fresh copy: a Uint8Array can be a view over a larger buffer, and sending the
      // backing buffer would upload unrelated bytes.
      const ack = await send(item, bytes.slice().buffer as ArrayBuffer, "bytes");
      if (typeof ack.bytesStored !== "number") {
        throw new Error("upload response did not report bytesStored");
      }
      return { bytesStored: ack.bytesStored };
    },

    async upsertRow(item): Promise<WriteAck> {
      const ack = await send(item, JSON.stringify(item.payload), "row");
      // Missing count means unproven, which the outbox treats as a failure and keeps
      // the local copy. Defaulting to 1 here would silently defeat the phantom guard.
      return { rowsWritten: typeof ack.rowsWritten === "number" ? ack.rowsWritten : 0 };
    },
  };
}
