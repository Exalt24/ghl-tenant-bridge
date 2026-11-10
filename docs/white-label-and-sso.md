# A white label domain and single sign on, set up in the free sandbox

On 2026-11-10 two settings that the earlier [read only check](snapshot-and-settings-check.md) had only looked at were actually set up in the free HighLevel developer sandbox `DAC Dev Sandbox`: a white label domain on its own address, and single sign on through an outside identity provider. Both worked. A real sign in was made on the branded address through the identity provider and landed on the agency dashboard. Every record is invented and nothing here is a client system. The numbers in square brackets are the screenshots in `screenshots/pipelines-funnels/`, which continue from 57.

| Part | Done | Read back | Result |
|---|---|---|---|
| White label domain `app.ghl-lab-dc.dynv6.net` | CNAME added at dynv6, Add domain, Verify records [59 to 63] | page after reload [65], brand record from the page's own API call | verified, saved, branded login page loads over https [66] |
| API domain `links.ghl-lab-dc.dynv6.net` | same route [64] | page after reload [65] | verified and saved, then removed on purpose [76, 77] |
| SSO with an Auth0 tenant (OpenID Connect) | four step form saved [68 to 70] | config read back after a reload, test status [72] | test PASSED, SSO switched on [73], real sign in worked [74, 75], switched off again [78] |

## 1. Before anything was changed

This is the undo record. The agency Company Settings page, White Label tab, read before any typing [57, 58]:

- Brand name `DAC Dev Sandbox`, Brand email filled with the account's own professional address, Brand phone number empty.
- White Label Domain empty, API Domain empty. No Google login switch was shown yet.
- Privacy Policy URL and Terms & Conditions URL empty, Custom JS and Custom CSS empty (one blank line each), no theme chosen.
- The brand record read from the page's own call, `GET /companies/brand/<COMPANY_ID>/all-brands`: one brand, `isDefault: true`, `domain` absent, `spareDomain` an empty string, `logoUrl`, `theme` and both policy fields empty.

The SSO tab showed the illustration and one blue Enable SSO button. [67] Learn More was opened in a new tab and closed again. The article is "Setting Up Single Sign-On (SSO) on HighLevel" and says two things that matter here: HighLevel supports OpenID Connect only (SAML is "part of the future roadmap"), and "Before enabling SSO, two conditions must be met: Your agency must be on the $497 plan. A Whitelabel domain must already be configured." The first condition was not enforced in this sandbox, which is covered under Limits.

## 2. The white label domain

**What the page asks for.** The Domains part has a White Label Domain box ("Please enter your white label domain and click on Add Domain") and an API Domain box. The small question mark beside White Label Domain says "Click for setup instructions" and opens the support article "How to Set Up a Whitelabel Domain for the Desktop Web App". It says to create a CNAME with the target `whitelabel.ludicrous.cloud.`, to add the domain in Agency View, Settings, Company, Whitelabel, and that the branded address is used "alongside the standard HighLevel link (e.g., app.gohighlevel.com)". So agency sign in at the normal address is not moved.

**Typing the domain.** `app.ghl-lab-dc.dynv6.net` was typed into the box, and Add domain became enabled. [59] Add domain opened "Connect your domain/subdomain" with the domain filled in and two choices, Continue and "Add record manually". [60] Add record manually was chosen. The dialog said "Set up your domain manually by adding the following records" and showed one record. [61] The values were read from the input boxes and not from the picture:

- Record `CNAME`
- Host `app.ghl-lab-dc`
- Required value `whitelabel.ludicrous.cloud`

**The record at dynv6.** Signed in at dynv6 with the saved login, the zone `ghl-lab-dc.dynv6.net` got a CNAME named `app` with data `whitelabel.ludicrous.cloud.` The trailing dot was typed this time, because the funnel work had shown dynv6 stores a name without a dot as relative to the zone. [62] A lookup against 8.8.8.8 then answered `app.ghl-lab-dc.dynv6.net canonical name = whitelabel.ludicrous.cloud`.

**Verify records.** The button spun for about ten seconds, with HighLevel's note "DNS updates could require up to 30 seconds. If changes aren't acknowledged, please retry." The page's calls were:

```
GET /companies/verify-domain/<COMPANY_ID>?domainType=whitelabelapp&domainValue=app.ghl-lab-dc.dynv6.net  ->  {"valid":true,"message":""}
PUT /companies/brand/<BRAND_ID>                                                                          ->  200
GET /companies/verify-cname/<COMPANY_ID>?domainType=whitelabelapp&domainValue=app.ghl-lab-dc.dynv6.net  ->  {"isMigrate":false,"domainType":"whitelabelapp","message":""}
```

The dialog closed and the White Label Domain box turned grey with an edit pencil and a red trash button, and a new switch appeared under the API Domain, "Google login" ("Enabling Google Login will allow users with option to login with Google OAuth."). It was left off. [63]

**Does it serve.** Straight after verifying, an https request to the new address failed at the TLS handshake (curl exit 35). That fits the article, which says the certificate is issued once the CNAME points the right way. About ten minutes later the same request answered HTTP 200 with a valid certificate, and `http://` answered 301 to https. In a browser the address shows a HighLevel sign in page headed "Sign into your account", with Email, Password, "Forgot password?" and Sign in. [66] No logo shows because none was uploaded.

**The API domain.** The same route was repeated for `links.ghl-lab-dc.dynv6.net`. The manual record the page asked for was a CNAME with host `links.ghl-lab-dc` and required value `brand.ludicrous.cloud`, a different target from the white label one. [64] It was added at dynv6 with a trailing dot, Verify records ran about 25 seconds, and the call came back `{"valid":true,"message":""}` for `domainType=apidomain`. After a reload both domains were in the boxes. [65] The page's brand record read back with `"domain":"app.ghl-lab-dc.dynv6.net"` and `"spareDomain":"links.ghl-lab-dc.dynv6.net"`, so the API domain is stored under the field name `spareDomain`. The API domain answered HTTP 200 over https as well.

**Reverting the API domain.** The API domain brands the links the platform generates across the sub-accounts for things like forms, calendars and payments, which would include the forms and calendars from the earlier work, so it was not left on a lab address. The red trash button opened "Remove domain - links.ghl-lab-dc.dynv6.net?" with the line "All your payment links, trigger links etc. going forward will not contain this domain." and a warning, "This will not remove the CNAME record in your DNS settings". [76] Remove was pressed. After a reload the box was empty again and the brand record read `"spareDomain":""`. [77] The `links` CNAME was then deleted at dynv6, which asked "This will permanently delete the CNAME record links.ghl-lab-dc.dynv6.net." and answered `DELETE 204`. The white label domain and its `app` record were kept as the evidence of this part.

## 3. Single sign on

**What Enable SSO does.** It opens a four step form and nothing is switched on by pressing it. [68] The steps are Client ID & Secret, Open ID Configuration, User Details Mapping, and Review and Finish Setup. There was no plan gate, upgrade text, payment or card request anywhere on the page.

**An identity provider, free and without a card.** The article lists Auth0, Azure and Okta. Auth0's sign up page says "Start building with your free plan. No credit card required." It took the account's own professional email, sent a six digit code to that mailbox (read from the mailbox and typed in), asked for a password (a random one, kept in the secrets folder), then asked only for Account Type (Personal) and Country. It never showed a phone number request or a human check. The dashboard then showed a development tenant and "You have 22 days left in your trial to experiment with features that are not in the Free plan", with a "Please enter your billing information here" line that was not followed. In it, a Regular Web Application `HighLevel sandbox SSO` was created and a tenant user was added through Create via JSON with `email_verified` set to true, because the form later asks HighLevel to read that field.

**The form.** Step 1 took the Auth0 Client ID and Client Secret, with the mechanism locked to OIDC. [68] Step 2 asked "Do you have an OpenID configuration URL?". Yes was chosen and `https://<TENANT>.us.auth0.com/.well-known/openid-configuration` entered, which had answered a plain request with the provider's JSON first, with the scopes `openid profile email`. The Redirect URL was prefilled as `https://app.ghl-lab-dc.dynv6.net` followed by a fixed `/login/sso`, so the white label domain is a real prerequisite and not just a line in the article. [69] Step 3 took Remote ID Field `sub`, Email Field `email` and Email Verified Field `email_verified`, the Auth0 values from the article. [70] Step 4 listed the entries and Save answered "SSO configuration saved successfully." The call was `POST /auth/sso-config`, HTTP 201.

**The callback.** Auth0 needs the redirect address whitelisted. `https://app.ghl-lab-dc.dynv6.net/login/sso` was added under Allowed Callback URLs and saved ("Successfully saved your changes. Changes may take up to 30 seconds before they take effect."). [71]

**The test.** After saving, the page said "SSO Test Required. You must run a test to complete your SSO setup." Test SSO took the browser to the Auth0 sign in page "Log in to <TENANT> to continue to HighLevel sandbox SSO". The tenant user signed in, Auth0 sent the browser back to `https://app.ghl-lab-dc.dynv6.net/login/sso?code=...&state=TEST::`, and the Test Status panel then read:

> PASSED
> SSO test completed successfully. You can now enable SSO for your organization.

[72]

**Switching it on.** The Enable SSO switch in Additional Settings gave "SSO has been enabled successfully." [73] Hide Other Login Options was not touched, so email and password sign in stayed available. The branded sign in page then showed "Or Continue with" and a "Sign in with SSO" button under Sign in. [74]

**A real sign in.** On the branded page Sign in with SSO went to `/login/sso`, to Auth0 (which did not ask for credentials again), back to `/login/sso?login=success`, then `/agency_launchpad` and `/agency_dashboard`, all on `app.ghl-lab-dc.dynv6.net`. The agency dashboard loaded under the branded address. [75] A pop-up asking for company details appeared on that first load and was closed with Cancel, with nothing entered.

**Read back and switched off.** After a reload the SSO Configuration panel held the Client ID, the secret, scopes `openid profile email`, the redirect address and the three mapping fields, and the Test Status still read PASSED. HighLevel had filled in only the Authorization URL from the discovery document, `https://<TENANT>.us.auth0.com/authorize`; the Token Endpoint and User Info Endpoint boxes were empty, and the test and the sign in worked anyway. Enable SSO was then switched off again ("SSO has been disabled successfully."), and the saved configuration and the passed test were kept. [78]

## What went wrong, and the fix

- **The test did not come back to the settings page.** The article says the test returns to the Company Settings SSO tab. Here the browser ended on the branded sign in page at `https://app.ghl-lab-dc.dynv6.net/`, because that address had no HighLevel session. Opening Company Settings in the normal agency window showed the result already stored as PASSED.
- **The save call returns the secret.** Both `POST /auth/sso-config` and the `GET` that follows it send the client secret back in plain text to a signed in agency user. It was not written into this repository, and the secret is only in the ignored secrets folder.
- **My browser server died on a native confirm box.** The dynv6 delete asks with a browser confirm. A second client connected to the same headless browser had no dialog handler, so the confirm was dismissed before the click's own handler could accept it, once ending the browser. The saved dynv6 session was lost with it and was signed in again. The fix was to answer the confirm inside the page and accept only when its text named the `links` record, and the call then returned `DELETE 204`.
- **The first Auth0 country pick failed.** It failed because the first select on the page was the language list. Filtering for the one holding Philippines fixed it.

## Limits, with what was not done

- **Sandbox only.** The sandbox expires up to 6 months after creation and its data can be reset. The Auth0 tenant is a free development tenant on a 22 day trial, and no card was entered.
- **The plan condition was not tested.** The article says SSO needs "the $497 plan". This sandbox has trial access to Enterprise features, and the page, the form, the test and the switch all worked with no plan text. So this shows the feature works in the sandbox, not that it works on any plan.
- **Left in place.** The white label domain `app.ghl-lab-dc.dynv6.net` and its `app` CNAME at dynv6, the saved SSO configuration with its passed test, and the Auth0 tenant, application and tenant user. To undo the domain press the trash button beside it on the White Label tab, then delete the `app` record at dynv6. To undo the SSO setup use the three dot menu on the SSO tab, which the article says deletes the configuration and expires the test. The normal agency address `app.gohighlevel.com` was never changed and kept working all through.
- **Reverted.** The API domain and its `links` CNAME, and the Enable SSO switch.
- **Not done.** No logo, privacy policy or terms address, custom JS or CSS, or theme was set. The Google login switch was left off. A second brand was not created (Add new brand was disabled when the page loaded and its reason was not looked into). Hide Other Login Options was not turned on, because it would remove email sign in. Whether Enable SSO can be switched on before a passed test was not tried. SAML is not offered. SSO signs in existing users only, as the article says, and no new user was created through it. Only one real SSO sign in was made, with one user.
- **Nothing was sent.** No email or text went out from HighLevel. The only mail was Auth0's own sign up code, sent to the account's own mailbox.
