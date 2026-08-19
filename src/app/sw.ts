/// <reference lib="webworker" />
/**
 * Service worker. ONE job: precache the app shell so a cold launch with no signal
 * still boots.
 *
 * WHAT THIS IS NOT
 * ---------------
 * It is NOT the upload mechanism. Background Sync is supported in no version of
 * Safari or iOS Safari, and iOS offers no Periodic Background Sync and no Background
 * Fetch either, so a service-worker flush would work in Chrome and silently never
 * run on the phones field crews carry. Worse, workbox-background-sync's non-Sync
 * fallback wires its replay to nothing, so it accepts writes and sits on them
 * forever. The flush therefore lives in the page (src/lib/outbox-runtime.ts) and this
 * file deliberately does not touch the queue.
 *
 * API routes are network-only. Caching a write endpoint would let a queued upload
 * appear to succeed against a cache, which is exactly the phantom the outbox guards
 * against at the other end.
 */

import { defaultCache } from "@serwist/next/worker";
import type { PrecacheEntry, SerwistGlobalConfig } from "serwist";
import { NetworkOnly, Serwist } from "serwist";

declare global {
  interface WorkerGlobalScope extends SerwistGlobalConfig {
    __SW_MANIFEST: (PrecacheEntry | string)[] | undefined;
  }
}

declare const self: ServiceWorkerGlobalScope;

const serwist = new Serwist({
  precacheEntries: self.__SW_MANIFEST,
  // Take over immediately. A field worker who reloads to fix a problem should get the
  // new worker, not wait for every tab to close.
  skipWaiting: true,
  clientsClaim: true,
  navigationPreload: true,
  runtimeCaching: [
    {
      // Never serve an API response from cache. A cached 200 on a write path would be
      // indistinguishable from a real one.
      matcher: ({ url }) => url.pathname.startsWith("/api/"),
      handler: new NetworkOnly(),
    },
    ...defaultCache,
  ],
});

serwist.addEventListeners();
