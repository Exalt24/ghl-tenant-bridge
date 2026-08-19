/**
 * outbox-storage-browser.ts -- the real browser storage behind the outbox.
 *
 * SPLIT BY DATA SHAPE, ON PURPOSE
 * -------------------------------
 * Photo BYTES go to the Origin Private File System. Queue METADATA goes to
 * IndexedDB. They are different problems and the same store is wrong for both.
 *
 * OPFS for bytes because it is Baseline (widely available since March 2023,
 * including iOS Safari) and it does not pay IndexedDB's structured-clone
 * serialization cost on every large ArrayBuffer, which is the dominant cost when the
 * payload is a few hundred kilobytes of JPEG.
 *
 * IndexedDB for metadata because the queue needs ordered enumeration and cheap
 * per-key updates, which a flat file directory does not give you.
 *
 * TWO TRAPS ENCODED HERE
 * ----------------------
 * 1. NO Blob EVER CROSSES INTO STORAGE. Bytes are handled as ArrayBuffer /
 *    Uint8Array throughout. Modern iOS can store a Blob in IndexedDB, and the
 *    old "Safari cannot" claim traces to a private-browsing bug that is fixed,
 *    but Workbox removed Blob bodies from its own queue after real Safari
 *    failures and the precaution costs nothing.
 * 2. OPFS EXISTS ON SAFARI BUT THE WRITE METHOD DOES NOT, UNTIL VERY RECENTLY.
 *    navigator.storage.getDirectory() has been supported in Safari for years, so a
 *    naive `isOpfsAvailable()` check passes there and then the write throws.
 *    FileSystemFileHandle.createWritable() is Baseline "newly available" only since
 *    September 2025 and requires SAFARI / iOS 26.0 (Chrome 86, Firefox 111). Every
 *    iPhone on iOS 25 or earlier therefore has OPFS directories and no way to write
 *    to them from the main thread.
 *
 *    That is not an edge case for this product. Field crews carry old phones, so on
 *    iOS the IndexedDB path below is the COMMON path for now and OPFS is the
 *    optimisation. Hence canWriteOpfs() feature-detects createWritable on a real
 *    handle rather than trusting getDirectory, and byte storage falls back to
 *    IndexedDB as ArrayBuffer. (Safari does expose createSyncAccessHandle, but it is
 *    worker-only, and routing every field photo through a worker to support one
 *    browser generation is a lot of machinery for no gain over IndexedDB.)
 *
 * The whole eviction story sits above this file: on iOS an origin with no user
 * interaction for seven days of browser use loses IndexedDB, OPFS and Cache API
 * together. Nothing here can prevent that; requestPersistentStorage() in outbox.ts
 * is the mitigation and it reports whether it worked.
 */

import type { OutboxItem, OutboxStorage } from "./outbox";

const DB_NAME = "asc_outbox";
const DB_VERSION = 2;
const STORE = "items";
/** Byte fallback store, used when createWritable is unavailable (iOS < 26). */
const BYTES_STORE = "bytes";
const OPFS_DIR = "outbox";

/**
 * Is there an OPFS directory at all? Necessary but NOT sufficient: Safari has had
 * this for years while lacking any way to write. Use canWriteOpfs() to decide.
 */
export function isOpfsAvailable(nav: Navigator = navigator): boolean {
  return typeof nav?.storage?.getDirectory === "function";
}

/**
 * Can we actually WRITE to OPFS from this thread?
 *
 * Detected on a real FileSystemFileHandle rather than a prototype sniff, because
 * that is the object whose method is missing on older Safari. The probe file is
 * removed afterwards, and any failure answers "no" rather than propagating, since
 * the only use of this is choosing a backend.
 *
 * Cached: the answer cannot change within a page lifetime and the probe touches disk.
 */
let opfsWritableCache: boolean | null = null;
export async function canWriteOpfs(): Promise<boolean> {
  if (opfsWritableCache !== null) return opfsWritableCache;
  if (!isOpfsAvailable()) return (opfsWritableCache = false);
  try {
    const dir = await opfsDir();
    const fh = await dir.getFileHandle(".capability-probe", { create: true });
    const ok = typeof (fh as { createWritable?: unknown }).createWritable === "function";
    try { await dir.removeEntry(".capability-probe"); } catch { /* probe cleanup is best effort */ }
    return (opfsWritableCache = ok);
  } catch {
    return (opfsWritableCache = false);
  }
}

/** Test seam: force a backend so both paths can be exercised on one browser. */
export function __setOpfsWritableForTests(v: boolean | null): void {
  opfsWritableCache = v;
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        // keyPath "id" is a UUIDv7, so IndexedDB's natural key order IS capture
        // order and list() needs no sort or secondary index.
        db.createObjectStore(STORE, { keyPath: "id" });
      }
      if (!db.objectStoreNames.contains(BYTES_STORE)) {
        // Out-of-line keys: the key is the bytes ref, the value is an ArrayBuffer.
        // NEVER a Blob, per trap 1 in the header.
        db.createObjectStore(BYTES_STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB.open failed"));
  });
}

function tx<T>(
  db: IDBDatabase,
  mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>,
  store: string = STORE
): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB request failed"));
    t.onabort = () => reject(t.error ?? new Error("indexedDB transaction aborted"));
  });
}

async function opfsDir(): Promise<FileSystemDirectoryHandle> {
  const root = await navigator.storage.getDirectory();
  return root.getDirectoryHandle(OPFS_DIR, { create: true });
}

/** A byte ref is "outbox/<uuid>"; OPFS wants just the file name. */
function fileNameFor(ref: string): string {
  const name = ref.split("/").pop();
  if (!name) throw new Error(`bad bytes ref: ${ref}`);
  return name;
}

export function createBrowserOutboxStorage(): OutboxStorage {
  let dbPromise: Promise<IDBDatabase> | null = null;
  const db = () => (dbPromise ??= openDb());

  return {
    async put(item: OutboxItem) {
      await tx(await db(), "readwrite", (s) => s.put(item));
    },

    async list(): Promise<OutboxItem[]> {
      // getAll returns records in ascending key order, and the key is a UUIDv7,
      // so this is capture order by construction.
      return (await tx<OutboxItem[]>(await db(), "readonly", (s) => s.getAll())) ?? [];
    },

    async delete(id: string) {
      await tx(await db(), "readwrite", (s) => s.delete(id));
    },

    async writeBytes(ref: string, bytes: Uint8Array) {
      // A fresh ArrayBuffer copy either way: a Uint8Array can be a view over a
      // larger buffer, and both backends would otherwise persist the whole thing.
      const buf = bytes.slice().buffer as ArrayBuffer;

      if (await canWriteOpfs()) {
        const dir = await opfsDir();
        const fh = await dir.getFileHandle(fileNameFor(ref), { create: true });
        const w = await fh.createWritable();
        try {
          await w.write(buf);
        } finally {
          // Nothing is on disk until close() resolves, so this must not be skipped
          // on an error path.
          await w.close();
        }
        return;
      }

      // iOS < 26 and anything else without createWritable.
      await tx(await db(), "readwrite", (s) => s.put(buf, ref), BYTES_STORE);
    },

    async readBytes(ref: string): Promise<Uint8Array> {
      // Try OPFS first, then IndexedDB, REGARDLESS of the current capability. An
      // item queued before a browser update lives in the other backend, and a read
      // that only consulted today's backend would declare those bytes missing.
      if (isOpfsAvailable()) {
        try {
          const dir = await opfsDir();
          const fh = await dir.getFileHandle(fileNameFor(ref));
          return new Uint8Array(await (await fh.getFile()).arrayBuffer());
        } catch (e) {
          if ((e as { name?: string })?.name !== "NotFoundError") throw e;
        }
      }
      const buf = await tx<ArrayBuffer | undefined>(
        await db(), "readonly", (s) => s.get(ref) as IDBRequest<ArrayBuffer | undefined>, BYTES_STORE
      );
      if (!buf) {
        const err = new Error(`no bytes at ${ref}`);
        err.name = "NotFoundError";
        throw err;
      }
      return new Uint8Array(buf);
    },

    async deleteBytes(ref: string) {
      // Both backends, for the same reason read checks both. Already-gone is
      // success: a flush interrupted between removing bytes and deleting the row
      // re-enters here, and throwing would wedge that item permanently.
      if (isOpfsAvailable()) {
        try {
          const dir = await opfsDir();
          await dir.removeEntry(fileNameFor(ref));
        } catch (e) {
          if ((e as { name?: string })?.name !== "NotFoundError") throw e;
        }
      }
      await tx(await db(), "readwrite", (s) => s.delete(ref), BYTES_STORE);
    },
  };
}

/**
 * Bytes used and the quota the browser admits to.
 *
 * Treat the quota as advisory. It is derived from TOTAL disk size rather than free
 * space, and an embedded in-app WebView gets a far smaller share than a home-screen
 * app, so the only reliable signal is a QuotaExceededError on write. This exists to
 * warn a user at ~80% rather than to make decisions.
 */
export async function storageUsage(): Promise<{ usedBytes: number; quotaBytes: number; ratio: number }> {
  if (typeof navigator?.storage?.estimate !== "function") {
    return { usedBytes: 0, quotaBytes: 0, ratio: 0 };
  }
  const e = await navigator.storage.estimate();
  const used = e.usage ?? 0;
  const quota = e.quota ?? 0;
  return { usedBytes: used, quotaBytes: quota, ratio: quota > 0 ? used / quota : 0 };
}
