# GHL Tenant Bridge: brand file

Working name: **GHL Tenant Bridge** (repo `Exalt24/ghl-tenant-bridge`). Decided 2026-10-22, kept on purpose. Nothing here is a trademark claim or a clearance.

## Brief

- What it is: a multi-tenant GoHighLevel webhook bridge. A Cloudflare Worker (`edge/`) verifies the signature and stores each delivery once, a cron forwards it with backoff, and the Next.js and Supabase side holds the tenant data.
- Who sees it first: a hiring manager or client who reads the repo to judge whether the work is tested and real.
- Tone: plain and technical, every number measured.

## Name

- Decision: keep **GHL Tenant Bridge**. The repo did not become a different thing, the Worker receiver is the front door of the same bridge, and the name already says what it does. It also uses the vendor's own word for the product the listing names, so a reader finds it by the term they search for.
- Why not rename: the repo is linked from the portfolio, the profile README, the LinkedIn project block and a pending application. GitHub redirects an old repo URL, but a new name would still have to be swept through every surface and rechecked, for no gain in clarity.
- Backups if it is ever renamed: Tenant Relay, Stormline.
- Note: "GHL" is the vendor's own abbreviation, used here only to say which system the bridge talks to.

## Tagline

A multi-tenant GoHighLevel bridge: a Cloudflare Worker receives each webhook once, a cron forwards it, and Supabase keeps the tenant data.

## Mark

None. This is a backend repo with no app UI, so a logo would add nothing a reader uses. The README hero stays the real recording of the running app.

## Colours and fonts

None assigned. The repo carries no brand palette and uses no brand font.

## Collision evidence

`python Operations/tools/brand_name_check.py ghl-tenant-bridge tenant-relay stormline-bridge` run 2026-10-22: no exact npm, PyPI, crates, App Store or GitHub-owner hit for the two alternatives; the current name is a repo slug, not a product name that needs a domain. The manual USPTO, WIPO, IPOPHL and EUIPO searches the tool prints were not run, because nothing here is sold under the name.

Previous names: none
Kept on purpose: none
