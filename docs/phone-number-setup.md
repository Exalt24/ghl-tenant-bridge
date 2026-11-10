# Trying to connect a free Twilio trial to the HighLevel sandbox

On 2026-11-11 I tried to close the "phone number setup" gap in the free HighLevel developer sandbox by connecting a free Twilio trial account to the sub-account `ASC Test Tenant B` (agency `DAC Dev Sandbox`). The earlier pages ([snapshot-and-settings-check.md](snapshot-and-settings-check.md), [pipelines-funnels-memberships.md](pipelines-funnels-memberships.md)) only read the phone pages. This run went further, and it stopped at two real blockers. No Twilio connection was made, no number was bought or searched for, nothing was saved in HighLevel, and no text was sent. Every record in the sandbox is invented. Nothing here is a client system. The numbers in square brackets are the screenshots in `screenshots/phone-setup/`.

| Step | Result |
|---|---|
| (a) Sign in to the Twilio trial with the stored login and read the Account SID, Auth Token and trial number | Stopped at Twilio's "Verify you are human" box on the login form. That tick is a person's click and was not touched. [01] |
| (b) Find where HighLevel accepts a Twilio connection | No such control exists in this sandbox. HighLevel's own help page says the switch is a support request, not a setting. [02 to 07] |
| (c) Phone system and trial number in the sub-account | Not reached. The sub-account still says "Your phone system requires configuration". [06] |
| (d) Workflow Send SMS action | Not re-run, because nothing changed that could change its answer. The earlier refusal stands: `Trial mode not enabled for this agency`. |

## (a) Twilio sign in

The stored Twilio login (`Operations/.secrets/twilio_login.txt`, never printed here) was typed into the sign in form in a signed in browser profile. The email step went through and the password step showed a box with an empty tick box labelled "Verify you are human" above the Continue button. [01] That is a human check. It was not clicked, and nothing was tried to get around it, so the Account SID, the Auth Token and the trial number were never read. The email is covered with a black bar in the screenshot and the password field is masked.

## (b) What the HighLevel sandbox offers

All pages below were read in the agency view and in the sub-account `ASC Test Tenant B`.

- **Agency, Settings, Phone Integration, Agency Settings.** The banner says "Connected to LeadConnector" with "Pay as you go", "Better Cost with Real-time billing", "Increased security" and "Telephony Management". The strip under it says "A2P and carrier fees are passed through without markup; A2P is not billed for non-LC accounts." The page text holds no mention of Twilio (0 matches for the word on the page). The Quick Links are Complete A2P verification, Resolve Suspension, Connect to LC Phone and Set Messaging Limits. [05]
- **Agency, Phone Integration, Sub Account Settings.** "You are rebilling telephone usage (Calls, SMS, Phone No, etc) to your clients" with a Connect Stripe button. Both sub-accounts show Number Intelligence Disabled, Rebilling Disabled and Managed By "Not available" with a "Create sub-account" link under it. Export, Move Numbers and "Switch all sub-accounts to Lc Phone" sit above the list. [02]
- **The row menu.** The three dots on the `ASC Test Tenant B` row open one entry, "Link to LeadConnector". It was read and closed with Escape. [03] Both that entry and the "Create sub-account" link are HighLevel's own LC Phone, not Twilio, so neither was pressed.
- **Move Numbers.** The button opens a dialog that moves numbers between sub-accounts under the same telephony parent, "only US and Canada numbers", and says "A2P Status will not be moved". It was opened, read and closed with its X, with nothing selected. [04]
- **Sub-account, Settings, Phone System, Phone numbers.** "Your phone system requires configuration. Please reach out to our support team for assistance." with the Phone System Settings button. [06]
- **Sub-account, Phone System, Additional Settings.** One tab, Telephony Provider. "Current Provider. You can choose your Telephony Provider" and, in the box, "No Telephony Provider(s) found". A note on the left says "Telephony Providers are only visible to agency admin and owners". [07]
- **Other routes tried.** The sub-account address `/settings/phone_integration` and `/settings/conversation-providers` each rendered an empty page, and the sub-account Integrations page rendered its tab strip (Integrations, Facebook, TikTok and LinkedIn Form Fields Mapping) with an empty body in the headless browser. The sub-account Settings menu has Phone System and no entry called Phone Integration.

So the sandbox has no field for a Twilio Account SID or Auth Token anywhere that was opened.

**What HighLevel's own help page says.** The article "How to Disable LC Phone System for Subaccount/Location (LC to Twilio)" (modified 20 March 2025) says switching a sub-account to your own Twilio account is done by sending HighLevel support the Location ID, the Twilio Account SID and the Twilio Auth Token through a "Disable LC Phone Form", and that "our high-level support team will review your request". The form itself (read, not filled) asks for a name, an email, the Location ID, the Account SID and the Auth Token, and carries an agreement that includes "This step is irreversible" and "All A2P brands and campaigns will be removed", and it ends in a bot protection step. It was not submitted, for three reasons. It is irreversible by its own words. It hands a Twilio Auth Token to a third party. And with (a) blocked there is no SID or token to put in it.

**Twilio's side, from search results, not confirmed in the console.** A Twilio help article says a trial account can have one active subaccount, and an error page says trial subaccounts cannot buy phone numbers. Both are second hand and the sources disagree on the first point, so nothing here depends on them.

## The honest result

- The free Twilio route is not something the sandbox lets a user do from its pages. The documented route is a support request that HighLevel processes, it cannot be undone, and it was not made.
- The only phone controls the sandbox does offer are for HighLevel's own LC Phone (link, create sub-account, switch all, move numbers, connect Stripe to rebill). None of them is free to use for a number, and none was pressed.
- The sub-account therefore still has no phone system and no number. No claim is made that a text, a number or a Twilio connection worked.

## Limits and what was not done

- **One human click was left alone.** The Twilio login box needs a person to tick "Verify you are human". After that tick the stored login would work in the same browser profile, and the SID, token and trial number could be read.
- **Not pressed:** Connect Stripe, Switch all sub-accounts to Lc Phone, Link to LeadConnector, Create sub-account, Phone System Settings, Export, every Learn More link, and the Account Creation tab was not reopened (it was read on 2026-11-10 in [snapshot-and-settings-check.md](snapshot-and-settings-check.md)).
- **Opened and closed with nothing saved:** the agency dashboard's "Update your Tax ID now" dialog and a Prospecting tour tip, both closed with their X, and the Move Numbers dialog.
- **Counts.** In HighLevel, 9 pages or routes were loaded (agency dashboard, agency settings menu, Phone Integration on two tabs, the sub-account Phone System on two tabs, two empty routes and the Integrations shell), and 3 dialogs or menus were opened and closed (Move Numbers, the row menu, the Tax ID dialog). The Twilio login was loaded in 2 steps, plus 2 vendor pages read (the help article and the support form). The agency-wide platform sweep tool was not run, because the pages that matter are these few.
- **The browser.** A signed in Edge profile for the sandbox was driven in the background on its own debug port (9351) one step at a time, because the usual port was held by another session's browser. It was closed at the end.
- **Settings changed:** none.
