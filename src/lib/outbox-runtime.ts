/**
 * outbox-runtime.ts -- registers the flush triggers.
 *
 * outbox.ts exports FLUSH_TRIGGERS as documentation. Without this file that list is
 * a comment: nothing ever calls flush() and the queue fills up forever. This is the
 * wiring, and it is separate from the queue so the state machine stays testable in
 * node with no DOM.
 *
 * WHY THESE EVENTS AND NOT THE OBVIOUS ONES
 * ----------------------------------------
 * There is no service-worker trigger, deliberately. Background Sync is supported in
 * NO version of Safari or iOS Safari, and iOS has no Periodic Background Sync or
 * Background Fetch either, so on the phones field crews carry nothing runs once the
 * app closes. A service-worker flush would look correct in Chrome and silently never
 * fire on an iPhone, which is worse than not having one.
 *
 * `unload` is absent because it is unreliable on mobile and does not fire when a tab
 * is closed from the switcher. `beforeunload` needs prior user interaction in Chrome.
 * The documented reliable pair is `visibilitychange` to hidden plus `pagehide`, so
 * those are what get used.
 *
 * `online` is a HINT, not a gate. navigator.onLine reports true on a LAN with no
 * internet, so it triggers an attempt and never blocks one.
 *
 * The periodic timer only runs while items remain, because a timer that ticks over
 * an empty queue is pure battery cost on a device already being asked to hold a
 * camera open.
 */

import type { FlushTrigger, Outbox } from "./outbox";

export interface RuntimeOptions {
  outbox: Outbox;
  /** Periodic retry interval while the queue is non-empty. */
  pollMs?: number;
  /** Notified after every attempt so a UI can re-read the pending count. */
  onFlush?: (trigger: FlushTrigger, result: Awaited<ReturnType<Outbox["flush"]>>) => void;
  /** Injected for tests; defaults to the real window. */
  target?: Pick<Window, "addEventListener" | "removeEventListener">;
  doc?: Pick<Document, "addEventListener" | "removeEventListener" | "visibilityState">;
}

export interface RuntimeHandle {
  /** Flush now, e.g. from a Retry button. */
  flushNow: (trigger?: FlushTrigger) => Promise<void>;
  /** Remove every listener and stop the timer. */
  stop: () => void;
  /** Triggers actually registered, so a test can assert the wiring exists. */
  registered: FlushTrigger[];
}

/**
 * Attach the queue to the page lifecycle. Returns a handle; call stop() on unmount.
 *
 * Safe to call once per page. The queue's own single-flight guard means overlapping
 * triggers cannot double-submit, which is why every listener here can fire freely
 * without coordination.
 */
export function startOutboxRuntime(opts: RuntimeOptions): RuntimeHandle {
  const { outbox } = opts;
  const pollMs = opts.pollMs ?? 30_000;
  const target = opts.target ?? (typeof window !== "undefined" ? window : undefined);
  const doc = opts.doc ?? (typeof document !== "undefined" ? document : undefined);

  const registered: FlushTrigger[] = [];
  let timer: ReturnType<typeof setInterval> | null = null;
  let stopped = false;

  const run = async (trigger: FlushTrigger) => {
    if (stopped) return;
    const result = await outbox.flush();
    opts.onFlush?.(trigger, result);
  };

  const onVisibility = () => {
    if (!doc) return;
    // Both directions matter. Becoming visible is the main chance to drain on iOS,
    // where nothing ran while the app was away. Going hidden is the last chance to
    // push anything captured seconds ago before the OS suspends the page.
    void run(doc.visibilityState === "visible"
      ? "visibilitychange:visible"
      : "visibilitychange:hidden");
  };
  const onPageHide = () => void run("pagehide");
  const onOnline = () => void run("online");

  if (doc) {
    doc.addEventListener("visibilitychange", onVisibility);
    registered.push("visibilitychange:visible", "visibilitychange:hidden");
  }
  if (target) {
    target.addEventListener("pagehide", onPageHide);
    target.addEventListener("online", onOnline);
    registered.push("pagehide", "online");
  }

  // Only tick while there is something to send.
  timer = setInterval(() => {
    void (async () => {
      if (stopped) return;
      if ((await outbox.pendingCount()) > 0) await run("timer");
    })();
  }, pollMs);
  registered.push("timer");

  // Mount is its own trigger: on iOS this is often the first execution since the
  // app was last closed, so anything queued in a dead zone drains here.
  registered.push("app-mount");
  void run("app-mount");

  return {
    flushNow: (trigger = "manual-retry") => run(trigger),
    registered,
    stop() {
      stopped = true;
      if (timer) clearInterval(timer);
      if (doc) doc.removeEventListener("visibilitychange", onVisibility);
      if (target) {
        target.removeEventListener("pagehide", onPageHide);
        target.removeEventListener("online", onOnline);
      }
    },
  };
}
