## Free vs paid, and why it matters for reuse

**The Marketplace app webhook path is the primary path, deliberately.** It is free,
Ed25519-signed, retried 12 times with backoff, and carries 30 days of delivery logs.

The workflow "Custom Webhook" action is a **premium** action and is used only as a
fallback for form submissions, which have no event in the catalogue.

Geocoding and parcels are keyless and free. The sandbox runs on trial access to
Enterprise features and expires about six months after creation, so nothing
load-bearing is built on it: most suites need no GHL account at all. Full
classification in `docs/DEMO_PORTABILITY.md`.
