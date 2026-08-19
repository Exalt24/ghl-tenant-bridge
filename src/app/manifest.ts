import type { MetadataRoute } from "next";

/**
 * Next's native manifest convention. No library involved.
 *
 * Installability matters here for a reason beyond a home-screen icon: on iOS,
 * WebKit's stated heuristics for granting PERSISTENT STORAGE include whether the
 * site is installed as a home-screen app, and an origin without persistence loses
 * IndexedDB, OPFS and Cache API together after seven days without interaction. So
 * for a field app the manifest is part of the durability story, not decoration.
 *
 * Chrome's install criteria need name (or short_name), a 192px and a 512px icon,
 * start_url, and a display mode from the standalone family. A service worker is no
 * longer required for installability, though one exists here for offline boot.
 *
 * Safari enforces none of this and will "Add to Dock" with or without a manifest, so
 * the manifest is written for Chromium's rules and Safari simply benefits.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Field Capture",
    short_name: "Capture",
    description:
      "Offline-capable field photo and inspection capture. Queues locally and uploads when signal returns.",
    start_url: "/capture",
    display: "standalone",
    background_color: "#0c1222",
    theme_color: "#0c1222",
    orientation: "portrait",
    icons: [
      // Maskable is ignored on iOS, which needs an apple-touch-icon instead (set in
      // the root layout). Both are provided rather than assuming one covers both.
      { src: "/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
