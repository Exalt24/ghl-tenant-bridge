"use client";

/**
 * /capture -- the field capture surface. This is what WIRES outbox.ts into a UI; before
 * it existed the queue had zero callers.
 *
 * DESIGN IS REFERENCE-DRIVEN, NOT INVENTED. Read from real screenshots of CompanyCam,
 * plus Encircle's and Jobber's documented strings:
 *
 *   * CompanyCam's actual mobile UI shows a THIN, MUTED offline strip reading
 *     "You seem to be offline." and a calm grey card reading "3 items waiting to
 *     upload." with a single-word "View" action. Not a coloured alert. Not blocking.
 *     My first instinct was a red banner, which is precisely the Housecall Pro mistake
 *     whose persistent "OFFLINE Limited Functionality" bar false-fires on good signal
 *     and gets complained about in reviews.
 *   * So the copy here is COUNT PLUS ACTION, never STATUS PLUS COLOUR, and it follows
 *     web.dev's guidance that non-technical audiences misread the word "offline". It
 *     says what will happen ("will upload when you have signal"), not what is wrong.
 *   * Encircle's pattern is a decrementing count that DISAPPEARS at zero rather than
 *     turning into a green tick, so the quiet state is the absence of noise.
 *   * Encircle's own 3-star review is "who's bright idea was it to make you save a
 *     picture before you can take the next picture". Capture therefore never blocks on
 *     a save: files are queued and the input is immediately reusable.
 *   * CompanyCam stamps GPS and time AT CAPTURE, not at upload, because the upload can
 *     be hours later. Same here.
 *
 * ARIA, checked against MDN rather than assumed:
 *   * The pending count is NOT a live region. role="status" carries an implicit
 *     aria-live="polite" and aria-atomic="true", and MDN states plainly that it is
 *     inappropriate for a frequently-updating counter: every photo would fire a full
 *     announcement and bury the user. So the count is plain text, and a single polite
 *     region announces only MEANINGFUL transitions (all sent, or something failed).
 *   * The queue is a plain <ul> of <li> with real <button> elements, NOT a listbox with
 *     option roles. Buttons inside a listbox is a documented antipattern.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Outbox, requestPersistentStorage, type OutboxItem } from "@/lib/outbox";
import { createBrowserOutboxStorage } from "@/lib/outbox-storage-browser";
import { startOutboxRuntime, type RuntimeHandle } from "@/lib/outbox-runtime";

/** Long edge to downscale to. A stock phone photo is far larger than anything a roof
 *  report needs, and Safari's total canvas budget is device-specific and finite, so
 *  full-size originals are how you meet a quota error and a black image. */
const MAX_EDGE = 1600;
const JPEG_QUALITY = 0.8;

async function downscale(file: File): Promise<Uint8Array> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const w = Math.max(1, Math.round(bitmap.width * scale));
  const h = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no 2d context");
  ctx.drawImage(bitmap, 0, 0, w, h);
  // Release the decoded bitmap explicitly. Safari hoards image memory and does not
  // reclaim it just because a reference went out of scope.
  bitmap.close();
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: JPEG_QUALITY });
  return new Uint8Array(await blob.arrayBuffer());
}

/** Best-effort position, captured AT capture time. Never blocks a photo. */
function currentPosition(): Promise<{ lat: number; lon: number } | null> {
  if (typeof navigator === "undefined" || !navigator.geolocation) return Promise.resolve(null);
  return new Promise((resolve) => {
    const done = (v: { lat: number; lon: number } | null) => resolve(v);
    const t = setTimeout(() => done(null), 4000);
    navigator.geolocation.getCurrentPosition(
      (p) => { clearTimeout(t); done({ lat: p.coords.latitude, lon: p.coords.longitude }); },
      () => { clearTimeout(t); done(null); },
      { enableHighAccuracy: false, timeout: 4000, maximumAge: 60_000 }
    );
  });
}

export default function CapturePage() {
  const [items, setItems] = useState<OutboxItem[]>([]);
  const [online, setOnline] = useState(true);
  const [persisted, setPersisted] = useState<boolean | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [busy, setBusy] = useState(false);

  const [thumbs, setThumbs] = useState<Record<string, string>>({});

  const boxRef = useRef<Outbox | null>(null);
  const runtimeRef = useRef<RuntimeHandle | null>(null);
  const lastCountRef = useRef<number>(0);
  const storageRef = useRef<ReturnType<typeof createBrowserOutboxStorage> | null>(null);
  // Object URLs must be revoked or every refresh leaks a blob, which on a phone doing
  // 40 photos a roof is a real memory problem rather than a tidiness one.
  const thumbUrlsRef = useRef<Record<string, string>>({});

  const refresh = useCallback(async () => {
    const box = boxRef.current;
    if (!box) return;
    const list = await box.list();
    setItems(list);

    // Build a thumbnail per queued photo from the bytes already on disk. Read back
    // rather than kept in memory, so a reload still shows them.
    const store = storageRef.current;
    if (store) {
      const live = new Set(list.map((i) => i.id));
      for (const [id, url] of Object.entries(thumbUrlsRef.current)) {
        if (!live.has(id)) {
          URL.revokeObjectURL(url);
          delete thumbUrlsRef.current[id];
        }
      }
      let added = false;
      for (const it of list) {
        if (!it.bytesRef || thumbUrlsRef.current[it.id]) continue;
        try {
          const bytes = await store.readBytes(it.bytesRef);
          const buf = new Uint8Array(bytes).buffer as ArrayBuffer;
          thumbUrlsRef.current[it.id] = URL.createObjectURL(
            new Blob([buf], { type: "image/jpeg" })
          );
          added = true;
        } catch {
          // A missing byte blob is itself worth seeing as a broken thumbnail rather
          // than silently rendering a normal-looking row.
        }
      }
      // Functional update: reading `thumbs` here would make it a dependency of
      // refresh, whose identity sits in the mount effect's dep array, so every
      // thumbnail batch would rebuild the Outbox and re-register the flush triggers.
      setThumbs((prev) => {
        const next = { ...thumbUrlsRef.current };
        const same =
          Object.keys(next).length === Object.keys(prev).length &&
          Object.keys(next).every((k) => prev[k] === next[k]);
        return same ? prev : next;
      });
      void added;
    }

    // Announce transitions only, never the count itself. A polite live region that
    // fires per photo is worse than no announcement at all.
    const prev = lastCountRef.current;
    if (prev > 0 && list.length === 0) setAnnouncement("All photos uploaded.");
    const failed = list.filter((i) => i.state === "failed").length;
    if (failed > 0) setAnnouncement(`${failed} photo${failed === 1 ? "" : "s"} need attention.`);
    lastCountRef.current = list.length;
  }, []);

  useEffect(() => {
    const storage = createBrowserOutboxStorage();
    storageRef.current = storage;
    const box = new Outbox({
      storage,
      // No transport configured in this build: Supabase env is optional here, and the
      // real one lives in outbox-transport-supabase.ts. This deliberately FAILS rather
      // than pretending to send, so nothing is ever deleted on a fake success.
      transport: {
        async uploadBytes() { throw new Error("no transport configured"); },
        async upsertRow() { return { rowsWritten: 0 }; },
      },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
      onChange: () => { void refresh(); },
    });
    boxRef.current = box;

    runtimeRef.current = startOutboxRuntime({ outbox: box, onFlush: () => void refresh() });

    // Ask on every mount, not once at install: a granted persistence can be reset, and
    // WebKit's heuristics for granting it include being installed to the home screen.
    void requestPersistentStorage(navigator).then((r) => setPersisted(r.persisted));

    const sync = () => setOnline(navigator.onLine);
    sync();
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    void refresh();

    return () => {
      runtimeRef.current?.stop();
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
      for (const url of Object.values(thumbUrlsRef.current)) URL.revokeObjectURL(url);
      thumbUrlsRef.current = {};
    };
  }, [refresh]);

  const onFiles = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const box = boxRef.current;
    const files = Array.from(e.target.files ?? []);
    // Clear the input immediately so the same file can be picked again and the control
    // is reusable while the previous batch is still being written.
    e.target.value = "";
    if (!box || files.length === 0) return;

    setBusy(true);
    const gps = await currentPosition();
    for (const f of files) {
      try {
        const bytes = await downscale(f);
        await box.enqueue("photo", {
          org_id: "demo-org",
          captured_lat: gps?.lat ?? null,
          captured_lon: gps?.lon ?? null,
          original_name: f.name,
          original_bytes: f.size,
        }, bytes);
      } catch (err) {
        setAnnouncement(
          `Could not save ${f.name}: ${err instanceof Error ? err.message : "unknown error"}`
        );
      }
    }
    setBusy(false);
    await refresh();
  }, [refresh]);

  const pending = items.length;
  const failed = items.filter((i) => i.state === "failed").length;

  return (
    <main style={{ padding: "16px", maxWidth: 680, margin: "0 auto" }}>
      {/* Thin, muted, non-blocking. Copied from CompanyCam's real treatment rather than
          the red alert bar I would otherwise have built. */}
      {!online && (
        <div
          data-testid="offline-strip"
          style={{
            margin: "-16px -16px 12px", padding: "6px 12px", fontSize: 13,
            background: "#1b2740", color: "#9fb0cc", textAlign: "center",
          }}
        >
          No signal. Photos are saved on this phone.
        </div>
      )}

      <h1 style={{ fontSize: 20, margin: "4px 0 14px" }}>Field Capture</h1>

      {/* Count plus action. Disappears entirely at zero, so the quiet state is silence
          rather than a green tick. */}
      {pending > 0 && (
        <div
          data-testid="pending-card"
          style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            gap: 12, background: "#182339", borderRadius: 10, padding: "12px 14px",
            marginBottom: 14,
          }}
        >
          <span data-testid="pending-count" style={{ fontSize: 15 }}>
            {pending} {pending === 1 ? "photo" : "photos"} will upload when you have signal.
          </span>
          <button
            type="button"
            onClick={() => void runtimeRef.current?.flushNow("manual-retry")}
            style={{
              background: "none", border: "none", color: "#7fb2ff",
              fontSize: 15, fontWeight: 600, cursor: "pointer", padding: 4,
            }}
          >
            Retry
          </button>
        </div>
      )}

      <label
        htmlFor="shot"
        style={{
          display: "block", textAlign: "center", padding: "16px 14px",
          border: "1px solid #2c74d6", borderRadius: 12, cursor: "pointer",
          background: "#1b3f78", color: "#eaf2ff", fontWeight: 600, fontSize: 16,
          marginBottom: 16,
        }}
      >
        {busy ? "Saving..." : "Take photos"}
        <input
          id="shot"
          data-testid="shot"
          type="file"
          /* image/jpeg, NOT image/*: asking for jpeg makes iOS transcode HEIC for us,
             and a HEIC reaching createImageBitmap throws. */
          accept="image/jpeg"
          capture="environment"
          multiple
          onChange={onFiles}
          style={{ position: "absolute", width: 1, height: 1, opacity: 0 }}
        />
      </label>

      {pending > 0 && (
        <ul data-testid="queue" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {items.map((i) => (
            <li
              key={i.id}
              style={{
                display: "flex", justifyContent: "space-between", alignItems: "center",
                padding: "10px 12px", borderBottom: "1px solid #1d2942", fontSize: 14,
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
                {/* The photo itself. Three identical timestamps tell a user nothing;
                    the image is how they recognise which shot is stuck. */}
                {thumbs[i.id] ? (
                  <img
                    src={thumbs[i.id]}
                    alt=""
                    width={40}
                    height={40}
                    style={{ borderRadius: 6, objectFit: "cover", flex: "0 0 auto", background: "#0c1222" }}
                  />
                ) : (
                  <span style={{
                    width: 40, height: 40, borderRadius: 6, flex: "0 0 auto",
                    background: "#0c1222", border: "1px solid #23324f",
                  }} />
                )}
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {String(i.payload.original_name ?? "photo")}
                  <span style={{ color: "#9fb0cc" }}>
                    {" · "}{new Date(i.capturedAt).toLocaleTimeString()}
                    {i.attempts > 0 && ` · ${i.attempts} attempt${i.attempts === 1 ? "" : "s"}`}
                  </span>
                </span>
              </span>
              {/* Two distinct user-facing states, per Encircle: waiting is normal,
                  needs-attention is not, and conflating them makes people re-shoot. */}
              <span style={{ color: i.state === "failed" ? "#ffab70" : "#9fb0cc" }}>
                {i.state === "failed" ? "Needs attention" : "Waiting"}
              </span>
            </li>
          ))}
        </ul>
      )}

      {failed > 0 && (
        <p style={{ fontSize: 13, color: "#9fb0cc", marginTop: 14 }}>
          Nothing is lost. These stay on this phone until they upload.
        </p>
      )}

      {persisted === false && (
        <p data-testid="persist-warning" style={{ fontSize: 13, color: "#7d8ea8", marginTop: 18 }}>
          Tip: add this to your home screen. Photos stay safest on a phone that opens it
          regularly.
        </p>
      )}

      {/* The ONLY live region, and it announces transitions rather than the count. */}
      <p role="status" style={{
        position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)",
      }}>
        {announcement}
      </p>
    </main>
  );
}
