# What survives, and what expires

This project is reused as a capability demo across gigs. So every dependency is
classified by whether it will still work in six months without someone paying for
it or re-enabling a trial. Anything in the EXPIRES column is a liability, not a
feature: a demo that dies quietly is worse than one that was never built, because
you find out in front of a client.

Written 2026-08-20 after noticing the Custom Webhook action is a **premium**
action, and that the sandbox runs on **trial access to Enterprise features**.

## FREE AND PERMANENT — build the demo on these

| Capability | Why it is safe |
|---|---|
| **Marketplace app webhooks** | Free. Ed25519-signed. 12 retries with backoff + jitter. 30-day delivery logs with manual replay. Covers 58 event types. No premium flag. |
| **Ed25519 / legacy-RSA signature verification** | Public keys are published in the docs. Pure `node:crypto`, no service dependency at all. |
| **Private Integration Tokens** | Free, and sandbox PITs behave like production ones for auth. |
| **Location-scoped tenancy + isolation** | It is GHL's own boundary (`403 "The token does not have access to this location."`), not a paid add-on. |
| **NWS live alerts** (`api.weather.gov`) | US government API. No key, no account. Only requirement is a `User-Agent` (empty UA returns 403, measured). |
| **NCEI SWDI historical hail** (`ncei.noaa.gov/swdiws`) | US government API. No key. Archive back to at least 2010, point geometry with hail size in inches. |
| **Supabase RLS + Postgres constraints** | Our own schema. The idempotency guarantee is a unique index, not a vendor feature. |
| **OAuth 2.0 install flow** | Standard OAuth against documented endpoints. A **private app needs no app review** and installs into up to 5 agencies. |

## EXPIRES OR COSTS MONEY — never let the demo depend on these

| Dependency | The catch |
|---|---|
| **Workflow "Custom Webhook" action** | **Premium.** $0.01 per execution from the agency wallet, 100 free per sub-account once enabled, and it is toggled per sub-account so it can simply be off. |
| **Sandbox account itself** | Lives **up to 6 months** from creation (created 2026-08-19, so ~Feb 2027). Data may be reset or purged at any time, and it can be deactivated earlier under Fair Use review. |
| **"Trial access to Enterprise features"** | The sandbox docs grant this explicitly for testing. Anything that only works because of it is unreproducible on a real account. |
| **Sandbox limits** | 25 req/10s, 10,000/day, per location and non-multiplying. **2 locations maximum.** Fine for a demo, useless for a load test. |
| **Inbound Webhook trigger** | Also a premium trigger. |

## Consequences for how this repo is built

1. **The Marketplace app path is the PRIMARY inbound path.** It is free, signed,
   retried and logged. The premium workflow action is a documented FALLBACK, used
   only where the 58-event catalogue has no equivalent.
2. **That fallback exists for exactly one reason: form submissions.** There is no
   `FormSubmit` or `SurveySubmit` event. If a demo does not need form triggers, it
   should not touch the premium action at all.
3. **The published sandbox workflow is dormant.** It fires only when a contact is
   explicitly enrolled (`POST /contacts/{id}/workflow/{id}`), so it cannot quietly
   accumulate premium executions. Exactly one execution has been run, on purpose.
4. **Weather is the most portable capability here** and, for a roofing client, the
   most differentiating. Two government APIs, no keys, no expiry, and the
   historical one reaches back over a decade. If everything else had to be thrown
   away, this would still demo.
5. **Nothing in the test suite depends on a premium feature.** Five of the six
   suites run with no GHL account at all; only `tenant_isolation.py` needs the
   sandbox, and it uses free PITs.

## If the sandbox dies

Re-creating it is free and takes about twenty minutes: developer account, App Test
Account, two sub-accounts, one PIT per sub-account. `docs/INTEGRATION_CONTRACT.md`
carries every measured value needed to redo it, and the ids are the only thing that
change. The four suites that do not touch GHL keep passing throughout.
