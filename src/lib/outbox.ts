/**
 * outbox.ts -- durable offline write queue for field capture.
 *
 * THE PROBLEM
 * -----------
 * A roof inspector stands on a roof with no signal, takes 40 photos and fills in a
 * form. All of it has to survive, in order, and reach the server exactly once when
 * signal returns. Losing a photo is worse than any UI defect here, because the
 * inspector has already left the property.
 *
 * THE CONSTRAINT THAT DECIDES THE ARCHITECTURE
 * --------------------------------------------
 * The Background Sync API is NOT SUPPORTED IN ANY VERSION OF SAFARI OR iOS SAFARI,
 * and its spec status is unofficial. iOS also offers no Periodic Background Sync and
 * no Background Fetch. So on the platform most field crews actually carry, NOTHING
 * runs after the app is closed. There is no background flush to build.
 *
 * Everything below follows from that: the flush lives in the PAGE, is driven by
 * visibility and focus rather than a service-worker event, and the UX has to be
 * honest that uploads happen while the app is open.
 *
 * FIVE RULES, EACH FROM A DOCUMENTED FAILURE IN A SHIPPED COMPETITOR
 * -----------------------------------------------------------------
 * 1. WRITE LOCALLY BEFORE ANY NETWORK CALL, and delete only after a confirmed 2xx.
 *    In every field app surveyed the queue is the only copy of a photo, and users
 *    losing pending work is the single most common complaint. A flush interrupted
 *    between upload and delete must be safe to repeat, which is what rule 2 buys.
 * 2. THE IDEMPOTENCY KEY IS MINTED AT CAPTURE, NOT AT FLUSH. A retry has to carry
 *    the same key as the original or the retry becomes a duplicate. UUIDv7 is used
 *    so keys sort by creation time and the queue drains in capture order for free.
 * 3. ONE FLUSH AT A TIME. Triggers overlap constantly (app becomes visible AND the
 *    online event fires AND a timer ticks), and two concurrent flushes double-submit.
 *    A single-flight guard is not optional.
 * 4. NEVER SHOW A COUNT THAT CAN DISAGREE WITH THE QUEUE. The worst documented
 *    failure in this category is a header count that says 67 photos while only 51
 *    exist, because the user then cannot tell whether to re-shoot. pendingCount()
 *    reads the same storage the flush reads, so the two cannot drift.
 * 5. REFRESH THE SESSION ONCE, AT THE TOP OF A FLUSH. Supabase access tokens last
 *    an hour and refresh tokens are single-use with a short reuse window, so two
 *    contexts refreshing at once can revoke the whole session and sign the
 *    inspector out on a roof. Hence a single `beforeFlush` hook rather than a
 *    refresh inside the per-item loop.
 *
 * WHAT THIS FILE DELIBERATELY DOES NOT DO
 * ---------------------------------------
 * It does not touch the DOM, register event listeners, or import a storage engine.
 * Storage and transport are injected. That is what makes the state machine testable
 * in node with no browser, while the browser-specific parts (OPFS, IndexedDB) are
 * separate adapters exercised under Playwright. A queue whose logic can only be
 * tested by driving a real browser tends not to get tested.
 */

// -------------------------------------------------------------------- ids

/**
 * UUIDv7: 48-bit big-endian unix millisecond timestamp, then 74 bits of randomness,
 * with the version and variant bits set. Lexicographic order matches creation order,
 * which is why the queue needs no separate sequence column.
 *
 * Layout per the RFC:
 *   bytes 0-5   unix_ts_ms
 *   byte  6     version (0111) in the high nibble
 *   byte  8     variant (10) in the two high bits
 */
export function uuidv7(nowMs: number, randomBytes: Uint8Array): string {
  if (randomBytes.length < 10) {
    throw new Error("uuidv7 needs at least 10 random bytes");
  }
  const b = new Uint8Array(16);
  // 48-bit timestamp, most significant byte first.
  b[0] = (nowMs / 2 ** 40) & 0xff;
  b[1] = (nowMs / 2 ** 32) & 0xff;
  b[2] = (nowMs / 2 ** 24) & 0xff;
  b[3] = (nowMs / 2 ** 16) & 0xff;
  b[4] = (nowMs / 2 ** 8) & 0xff;
  b[5] = nowMs & 0xff;
  for (let i = 0; i < 10; i++) b[6 + i] = randomBytes[i];
  b[6] = (b[6] & 0x0f) | 0x70; // version 7
  b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return (
    hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" +
    hex.slice(16, 20) + "-" + hex.slice(20)
  );
}

// -------------------------------------------------------------------- types

export type ItemState = "pending" | "inflight" | "failed";

export interface OutboxItem {
  /** UUIDv7, minted at capture. This IS the idempotency key end to end. */
  id: string;
  kind: "photo" | "form";
  /** Row payload. For a photo this is metadata only; bytes live under bytesRef. */
  payload: Record<string, unknown>;
  /** Key into binary storage (OPFS path or IndexedDB key). Null for form-only items. */
  bytesRef: string | null;
  capturedAt: number;
  attempts: number;
  state: ItemState;
  lastError: string | null;
  /** Earliest time a retry may run, for backoff. */
  nextAttemptAt: number;
}

export interface OutboxStorage {
  put(item: OutboxItem): Promise<void>;
  /** All items, ascending by id, which is capture order because ids are UUIDv7. */
  list(): Promise<OutboxItem[]>;
  delete(id: string): Promise<void>;
  readBytes(ref: string): Promise<Uint8Array>;
  writeBytes(ref: string, bytes: Uint8Array): Promise<void>;
  deleteBytes(ref: string): Promise<void>;
}

export interface OutboxTransport {
  /** Upload bytes to a deterministic destination derived from the item id, so a
   *  repeat upload overwrites rather than duplicating. Must be idempotent. */
  uploadBytes(item: OutboxItem, bytes: Uint8Array): Promise<void>;
  /** Upsert the row keyed on item.id. The server side needs a UNIQUE INDEX on that
   *  column, not merely a constraint, because PostgREST resolves on_conflict
   *  against an index. */
  upsertRow(item: OutboxItem): Promise<void>;
}

export interface OutboxOptions {
  storage: OutboxStorage;
  transport: OutboxTransport;
  now: () => number;
  random: (n: number) => Uint8Array;
  /** Called ONCE per flush, before any item. The place to refresh auth. */
  beforeFlush?: () => Promise<void>;
  /** Attempts before an item is parked as `failed` and surfaced to the user. */
  maxAttempts?: number;
  /** Base backoff in ms; grows exponentially with jitter. */
  backoffBaseMs?: number;
  onChange?: () => void;
}

export interface FlushResult {
  sent: number;
  failed: number;
  skipped: number;
  /** True when another flush was already running and this call did nothing. */
  alreadyRunning: boolean;
}

// -------------------------------------------------------------------- outbox

export class Outbox {
  private o: Required<Pick<OutboxOptions, "maxAttempts" | "backoffBaseMs">> & OutboxOptions;
  private flushing = false;

  constructor(opts: OutboxOptions) {
    this.o = { maxAttempts: 5, backoffBaseMs: 1000, ...opts };
  }

  /**
   * Queue an item. Writes to storage BEFORE returning and never touches the
   * network, so capture is always instant and never blocked by connectivity.
   *
   * Returns the id, which is the idempotency key the server will dedupe on.
   */
  async enqueue(
    kind: OutboxItem["kind"],
    payload: Record<string, unknown>,
    bytes?: Uint8Array
  ): Promise<string> {
    const now = this.o.now();
    const id = uuidv7(now, this.o.random(10));
    const bytesRef = bytes ? `outbox/${id}` : null;

    // Bytes first. If this throws on a full disk, no queue row is created, so the
    // caller learns the photo was not saved instead of finding a row pointing at
    // bytes that do not exist.
    if (bytes && bytesRef) await this.o.storage.writeBytes(bytesRef, bytes);

    await this.o.storage.put({
      id,
      kind,
      payload,
      bytesRef,
      capturedAt: now,
      attempts: 0,
      state: "pending",
      lastError: null,
      nextAttemptAt: 0,
    });
    this.o.onChange?.();
    return id;
  }

  /** Everything still queued, capture order. */
  async list(): Promise<OutboxItem[]> {
    return this.o.storage.list();
  }

  /**
   * Count of items not yet delivered.
   *
   * Reads the same storage flush() reads, on purpose. A count computed from a
   * separate counter is how a UI ends up claiming 67 photos when 51 exist, which
   * then makes users re-shoot work they already captured.
   */
  async pendingCount(): Promise<number> {
    return (await this.o.storage.list()).length;
  }

  /** Items parked after exhausting retries. These need a human. */
  async failedItems(): Promise<OutboxItem[]> {
    return (await this.o.storage.list()).filter((i) => i.state === "failed");
  }

  /**
   * Attempt delivery of everything due.
   *
   * Single-flight: overlapping triggers are normal (visible + online + timer all
   * fire together), and two concurrent flushes would double-submit.
   *
   * Sequential, not parallel. A field connection is the bottleneck and ordering is
   * meaningful, so concurrency buys little and risks reordering.
   */
  async flush(): Promise<FlushResult> {
    if (this.flushing) {
      return { sent: 0, failed: 0, skipped: 0, alreadyRunning: true };
    }
    this.flushing = true;
    const result: FlushResult = { sent: 0, failed: 0, skipped: 0, alreadyRunning: false };

    try {
      // Once, before any item. Refreshing inside the loop is what revokes a
      // Supabase session when two refreshes land inside the reuse window.
      if (this.o.beforeFlush) await this.o.beforeFlush();

      const now = this.o.now();
      for (const item of await this.o.storage.list()) {
        if (item.state === "failed") {
          result.skipped++;
          continue;
        }
        if (item.nextAttemptAt > now) {
          result.skipped++;
          continue;
        }

        try {
          if (item.bytesRef) {
            const bytes = await this.o.storage.readBytes(item.bytesRef);
            await this.o.transport.uploadBytes(item, bytes);
          }
          await this.o.transport.upsertRow(item);

          // Only now is it safe to drop the local copy. Deleting any earlier means
          // a crash between the two steps loses the photo permanently.
          if (item.bytesRef) await this.o.storage.deleteBytes(item.bytesRef);
          await this.o.storage.delete(item.id);
          result.sent++;
        } catch (e) {
          const attempts = item.attempts + 1;
          const exhausted = attempts >= this.o.maxAttempts;
          // Exponential backoff with jitter, so a whole queue that failed together
          // does not retry in lockstep the moment signal returns.
          const delay =
            this.o.backoffBaseMs * 2 ** (attempts - 1) * (0.5 + this.jitter());
          await this.o.storage.put({
            ...item,
            attempts,
            state: exhausted ? "failed" : "pending",
            lastError: e instanceof Error ? e.message : String(e),
            nextAttemptAt: this.o.now() + Math.round(delay),
          });
          result.failed++;
          // Keep going. One bad item must not block the rest of the queue, which is
          // the difference between losing one photo and losing the day's work.
        }
      }
    } finally {
      this.flushing = false;
      this.o.onChange?.();
    }
    return result;
  }

  /** Clear the failed flag so a user-driven Retry can pick an item up again. */
  async retryFailed(): Promise<number> {
    const items = (await this.o.storage.list()).filter((i) => i.state === "failed");
    for (const i of items) {
      await this.o.storage.put({ ...i, state: "pending", attempts: 0, nextAttemptAt: 0 });
    }
    this.o.onChange?.();
    return items.length;
  }

  private jitter(): number {
    // One random byte scaled to [0,1). Uses the injected source so tests are
    // deterministic; Math.random would make backoff untestable.
    return this.o.random(1)[0] / 256;
  }
}

// -------------------------------------------------------------------- triggers

/**
 * The events worth flushing on, in priority order, and why these and not others.
 *
 * `visibilitychange` to hidden and `pagehide` are the documented reliable signals
 * that a page is going away. `unload` is explicitly unreliable on mobile and does
 * not fire when a tab is closed from the switcher, and `beforeunload` needs prior
 * user interaction, so neither is listed.
 *
 * `online` is included as a HINT only. navigator.onLine reports true on a LAN with
 * no internet, so it must never gate a feature; it just means "worth trying now".
 *
 * There is deliberately no service-worker trigger. On iOS nothing runs after close.
 */
export const FLUSH_TRIGGERS = [
  "app-mount",
  "visibilitychange:visible",
  "visibilitychange:hidden",
  "pagehide",
  "online",
  "timer",
  "manual-retry",
] as const;

export type FlushTrigger = (typeof FLUSH_TRIGGERS)[number];

/**
 * Ask for storage persistence and report honestly whether it was granted.
 *
 * Why this matters more than it looks: on iOS, an origin with no user interaction
 * for seven days of browser use has its script-created storage DELETED, and that
 * eviction takes IndexedDB, OPFS and Cache API together, per origin. Persistence
 * exempts an origin from that. Safari and Chrome decide without prompting, and
 * WebKit's stated heuristics include whether the site is installed to the home
 * screen, so prompting the user to install genuinely improves the odds.
 *
 * The boolean is returned rather than swallowed because a false here is worth
 * surfacing: it means the queue can be wiped while the inspector is off for a week.
 */
export async function requestPersistentStorage(
  nav: { storage?: { persist?: () => Promise<boolean>; persisted?: () => Promise<boolean> } }
): Promise<{ supported: boolean; persisted: boolean }> {
  const s = nav.storage;
  if (!s || typeof s.persist !== "function") return { supported: false, persisted: false };
  if (typeof s.persisted === "function" && (await s.persisted())) {
    return { supported: true, persisted: true };
  }
  return { supported: true, persisted: await s.persist() };
}
