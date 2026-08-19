import withSerwistInit from "@serwist/next";

/**
 * Serwist, not next-pwa.
 *
 * next-pwa's last npm release was 5.6.0 in August 2022, it has no documented App
 * Router support, and Next's own PWA guide points at Serwist for service-worker
 * offline caching. Serwist is also maintained by the person who maintained the
 * next-pwa fork, so it is the continuation rather than a competitor.
 *
 * The service worker exists for ONE job here: precache the app shell so a cold
 * launch in a dead zone still boots. It is deliberately NOT the flush mechanism,
 * because Background Sync does not exist in any version of Safari or iOS Safari, so
 * a service-worker flush would silently never run on the phones field crews carry.
 * The flush lives in the page (see src/lib/outbox-runtime.ts).
 */
const withSerwist = withSerwistInit({
  swSrc: "src/app/sw.ts",
  swDest: "public/sw.js",
  // In dev the SW gets in the way of iteration and hides changes behind a cache.
  disable: process.env.NODE_ENV === "development",
  // Precaching a manifest that 404s aborts registration entirely, which is a
  // documented Serwist failure mode on Next (dynamic-css-manifest.json).
  exclude: [/dynamic-css-manifest\.json$/, /app-build-manifest\.json$/, /\.map$/],
});

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The webhook receiver verifies an Ed25519/RSA signature over the RAW body, which
  // needs node:crypto. Choosing the edge runtime for that route fails at deploy
  // rather than at design time, so the default node runtime is kept deliberately.
  experimental: {},
};

export default withSerwist(nextConfig);
