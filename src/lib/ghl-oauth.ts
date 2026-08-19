/**
 * GoHighLevel Marketplace OAuth 2.0 — install flow, token refresh, and the
 * agency-to-location token exchange.
 *
 * ===================== WRITTEN AGAINST THE OFFICIAL SPECS =====================
 * Paths and parameters were taken from HighLevel's own OpenAPI specs in
 * github.com/GoHighLevel/highlevel-api-docs, then the routes were probed live on
 * 2026-08-19. Not from a blog, and not from a summary.
 *
 * THE PATH RENAME, which looks like a docs contradiction until you see both specs:
 *
 *   Version 2021-07-28  (apps/oauth.json)      Version v3 (apps/v3/oauth-v3.json)
 *   ----------------------------------------   ---------------------------------
 *   POST /oauth/locationToken                  POST /oauth/location-token
 *   GET  /oauth/installedLocations             GET  /oauth/installed-locations
 *   POST /oauth/token                          POST /oauth/token
 *
 * v3 moved to kebab-case. MEASURED BEHAVIOUR of the live API:
 *
 *   /oauth/locationToken   + Version: 2021-07-28  -> 401 (route exists)
 *   /oauth/locationToken   + Version: v3          -> 401 (route exists)
 *   /oauth/location-token  + Version: 2021-07-28  -> 404 (route does NOT exist)
 *   /oauth/location-token  + Version: v3          -> 401 (route exists)
 *
 * So the camelCase form is served under BOTH versions and the kebab-case form only
 * under v3. This module therefore defaults to camelCase, which survives a version
 * bump in either direction. A 404-versus-401 distinction is what proves a route
 * exists at all; treating a 401 as "broken" is how the earlier confusion happened.
 *
 * THE VERSION HEADER IS MANDATORY. Omitting it returns
 * 401 {"message":"version header was not found."}. A bogus value returns
 * 400 "Invalid API version segment '<x>' at position 0". Measured both.
 */

const API_BASE = "https://services.leadconnectorhq.com";
const MARKETPLACE_BASE = "https://marketplace.gohighlevel.com";

/** 2021-07-28 is still served and has no published retirement date, but it is in a
 *  maintenance window (critical fixes only). v3 (released 2026-06-11) is current.
 *  Pinned explicitly so a silent upstream default can never move under us. */
export const DEFAULT_VERSION = process.env.GHL_API_VERSION ?? "2021-07-28";

export type GhlUserType = "Company" | "Location";

export interface GhlTokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
  scope: string;
  userType: GhlUserType;
  companyId?: string;
  /** Present on Location-type tokens only. */
  locationId?: string;
  userId?: string;
  approvedLocations?: string[];
  planId?: string;
  isBulkInstallation?: boolean;
}

export interface OAuthConfig {
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  version?: string;
}

function cfg(partial?: Partial<OAuthConfig>): OAuthConfig {
  const c: OAuthConfig = {
    clientId: partial?.clientId ?? process.env.GHL_CLIENT_ID ?? "",
    clientSecret: partial?.clientSecret ?? process.env.GHL_CLIENT_SECRET ?? "",
    redirectUri: partial?.redirectUri ?? process.env.GHL_REDIRECT_URI ?? "",
    version: partial?.version ?? DEFAULT_VERSION,
  };
  if (!c.clientId || !c.clientSecret || !c.redirectUri) {
    throw new Error(
      "GHL OAuth is not configured. Set GHL_CLIENT_ID, GHL_CLIENT_SECRET and " +
        "GHL_REDIRECT_URI (from the Marketplace Developer Portal app settings)."
    );
  }
  return c;
}

/**
 * Step 1: send the user here to choose which account to connect.
 *
 * NOTE the host: the authorize step is on the MARKETPLACE host, not the API host.
 * Sending it to services.leadconnectorhq.com is a common and confusing mistake.
 *
 * `state` is not optional in practice: without it the callback is open to CSRF,
 * where an attacker gets their own account connected into a victim's session. Pass
 * a random value, store it against the session, and compare on callback.
 */
export function buildAuthorizeUrl(
  scopes: string[],
  state: string,
  partial?: Partial<OAuthConfig>
): string {
  const c = cfg(partial);
  const qs = new URLSearchParams({
    response_type: "code",
    client_id: c.clientId,
    redirect_uri: c.redirectUri,
    scope: scopes.join(" "), // space-separated, per the Scopes doc
    state,
  });
  return `${MARKETPLACE_BASE}/oauth/chooselocation?${qs.toString()}`;
}

async function tokenRequest(
  body: Record<string, string>,
  version: string
): Promise<GhlTokenResponse> {
  const res = await fetch(`${API_BASE}/oauth/token`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
      Version: version,
    },
    body: new URLSearchParams(body).toString(),
  });
  const text = await res.text();
  if (!res.ok) {
    // Deliberately does NOT echo the response body into the thrown message beyond a
    // truncated form: token endpoints can reflect credentials in error payloads.
    throw new Error(`GHL /oauth/token -> ${res.status}: ${text.slice(0, 200)}`);
  }
  return JSON.parse(text) as GhlTokenResponse;
}

/** Step 2: exchange the callback `code` for tokens. */
export function exchangeCode(
  code: string,
  partial?: Partial<OAuthConfig>
): Promise<GhlTokenResponse> {
  const c = cfg(partial);
  return tokenRequest(
    {
      client_id: c.clientId,
      client_secret: c.clientSecret,
      grant_type: "authorization_code",
      code,
      redirect_uri: c.redirectUri,
    },
    c.version!
  );
}

/**
 * Refresh.
 *
 * CRITICAL: refresh tokens ROTATE. The docs state the original refresh token
 * becomes invalid and the response carries a new one. So the caller MUST persist
 * `refresh_token` from every response. Storing only the first one means the second
 * refresh fails and the tenant silently disconnects, which is the classic way an
 * integration dies quietly a day after it is installed.
 */
export function refreshAccessToken(
  refreshToken: string,
  partial?: Partial<OAuthConfig>
): Promise<GhlTokenResponse> {
  const c = cfg(partial);
  return tokenRequest(
    {
      client_id: c.clientId,
      client_secret: c.clientSecret,
      grant_type: "refresh_token",
      refresh_token: refreshToken,
    },
    c.version!
  );
}

/**
 * Step 3, and the one that actually fixes "Token's user type mismatch!".
 *
 * An AGENCY install yields userType: Company, which cannot call sub-account
 * endpoints no matter which scopes were granted. This mints a Location token for
 * one sub-account. The App-Install webhook delivers the `locationId`, which is the
 * documented moment to call this.
 *
 * `usePathVariant` exists because of the rename documented at the top of this file.
 * The default 'camel' is served under both API versions.
 */
export async function getLocationToken(
  agencyAccessToken: string,
  companyId: string,
  locationId: string,
  opts?: { version?: string; usePathVariant?: "camel" | "kebab" }
): Promise<GhlTokenResponse> {
  const version = opts?.version ?? DEFAULT_VERSION;
  const path = opts?.usePathVariant === "kebab" ? "/oauth/location-token" : "/oauth/locationToken";

  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${agencyAccessToken}`,
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
      Version: version,
    },
    body: new URLSearchParams({ companyId, locationId }).toString(),
  });
  const text = await res.text();
  if (!res.ok) {
    if (res.status === 404) {
      throw new Error(
        `GHL ${path} -> 404. That path does not exist for Version ${version}. ` +
          `The kebab-case form only exists on v3; use the camelCase default.`
      );
    }
    throw new Error(`GHL ${path} -> ${res.status}: ${text.slice(0, 200)}`);
  }
  return JSON.parse(text) as GhlTokenResponse;
}

/** Which sub-accounts have this app installed. Path name also changed in v3. */
export async function listInstalledLocations(
  agencyAccessToken: string,
  companyId: string,
  appId: string,
  opts?: { version?: string; usePathVariant?: "camel" | "kebab" }
): Promise<unknown> {
  const version = opts?.version ?? DEFAULT_VERSION;
  const path =
    opts?.usePathVariant === "kebab" ? "/oauth/installed-locations" : "/oauth/installedLocations";
  const qs = new URLSearchParams({ companyId, appId });
  const res = await fetch(`${API_BASE}${path}?${qs.toString()}`, {
    headers: {
      Authorization: `Bearer ${agencyAccessToken}`,
      Accept: "application/json",
      Version: version,
    },
  });
  if (!res.ok) throw new Error(`GHL ${path} -> ${res.status}`);
  return res.json();
}

/**
 * Compute an absolute expiry from `expires_in`.
 *
 * Refreshing exactly at expiry loses every request in flight, so a skew is
 * subtracted. Access tokens are about 24h, so 5 minutes is generous and cheap.
 */
export function expiresAt(token: GhlTokenResponse, skewSeconds = 300): Date {
  return new Date(Date.now() + Math.max(0, token.expires_in - skewSeconds) * 1000);
}

export function isExpired(expiry: Date | string, now: Date = new Date()): boolean {
  const d = typeof expiry === "string" ? new Date(expiry) : expiry;
  return now >= d;
}
