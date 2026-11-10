# Custom field, custom value and calendar booking through the HighLevel API

A custom field on contacts, a custom value on the sub-account and a calendar with a real booking, all made and read back through the HighLevel API v2 on 2026-11-10. No browser and no UI were used. The pages before this one ([lead-welcome-workflow.md](lead-welcome-workflow.md) and [pipelines-funnels-memberships.md](pipelines-funnels-memberships.md)) were built by hand in the UI. This one is the same sandbox driven from code, with every step read back.

Everything ran in the free HighLevel developer sandbox, sub-account `ASC Test Tenant B` (location `1VrYtmTq0g67GfzDixqN`), with that sub-account's Private Integration Token. Every record is invented: the contact is `Test Lead` with the address `test.lead@example.com`. Nothing here is a client system.

| Part | Made | Read back | Shown to take effect |
|---|---|---|---|
| Custom field `Lead source detail` (contact, text) | `POST /locations/{id}/customFields` | `GET` of the list, then `GET /contacts/{id}` | The test contact carries `Spring open house flyer` in it |
| Custom value `Welcome offer text` | `POST /locations/{id}/customValues` | `GET` of the list, three times | The text changed from the first to the second value after a `PUT` |
| Calendar `Discovery call (API demo)`, weekdays 9 to 5, 30 minute slots | `POST /calendars/` | `GET /calendars/{id}` | 80 free slots in the next 7 days, 79 after the booking |
| Appointment for the test contact | `POST /calendars/events/appointments` | `GET` of the appointment and of the contact's appointment list | The booked slot left the free slot list; the delete emptied the contact's list |

The script is [tests/custom_fields_values_calendars.py](../tests/custom_fields_values_calendars.py) and the request building and checks are in [tests/ghl_setup.py](../tests/ghl_setup.py). Both are described under "The code and the tests" below.

## How every call is made

Each request has these headers. The token is read from a file inside the code and is never printed or written to the transcript.

```
Authorization: Bearer <token>
Version: 2021-07-28
Accept: application/json
Content-Type: application/json     (only when there is a body)
```

The base address is `https://services.leadconnectorhq.com`. The sandbox allows 25 requests per 10 seconds, so a small throttle keeps every 10 second window at 20 requests or fewer and leaves 0.3 seconds between two calls. A full run is 21 to 23 calls.

The docs pages for these endpoints list `v3` as the only value for the `Version` header. The calls here send `2021-07-28`, the value the rest of this repo sends, and every one of them was accepted.

## 1. The custom field

The list is read first and the field is looked up by name, so a second run finds it and does not create another one.

```
GET /locations/1VrYtmTq0g67GfzDixqN/customFields?model=contact
200  {"customFields": []}
```

```
POST /locations/1VrYtmTq0g67GfzDixqN/customFields
{"name": "Lead source detail", "dataType": "TEXT", "model": "contact",
 "placeholder": "Where the lead first heard of us"}

201  {"customField": {"id": "ZpHcB3m6FaMtyj38dt6M", "model": "contact",
       "name": "Lead source detail", "fieldKey": "contact.lead_source_detail",
       "dataType": "TEXT", "locationId": "1VrYtmTq0g67GfzDixqN", ...},
      "backfillTriggered": false}
```

The scope the docs name for this call is `locations/customFields.write`. The list read back returns the same object with the same `fieldKey`.

**Setting it on the test contact.** `POST /contacts/upsert` finds a contact by email and creates one only when there is none, so it is safe to repeat. It returned `"new": false` on the recorded run, because the first run had already made `Test Lead`.

```
POST /contacts/upsert
{"locationId": "1VrYtmTq0g67GfzDixqN", "firstName": "Test", "lastName": "Lead",
 "email": "test.lead@example.com", "source": "api test"}
201  {"new": false, "contact": {"id": "WVaICbL8tXgSLt8guezy", "email": "test.lead@example.com", ...}}

PUT /contacts/WVaICbL8tXgSLt8guezy
{"customFields": [{"id": "ZpHcB3m6FaMtyj38dt6M", "field_value": "Spring open house flyer"}]}
200  {"succeeded": true, "contact": {...}}

GET /contacts/WVaICbL8tXgSLt8guezy
200  {"contact": {"id": "WVaICbL8tXgSLt8guezy", "firstName": "Test", "lastName": "Lead",
       "customFields": [{"id": "ZpHcB3m6FaMtyj38dt6M", "value": "Spring open house flyer"}, ...]}}
```

The check in the script compares the stored value with the text it sent, so a write that is accepted and then ignored fails the run. The offline test has a stand-in that does exactly that, and the run goes red.

**How a custom field is used.** This part is from HighLevel's documentation and was not run. The API returns the field's key as `contact.lead_source_detail`. HighLevel's help article on custom fields says contact custom fields can be used in forms, surveys, workflows and Smart Lists, and that their values can be merged into texts, emails and internal notifications. A merge field is the field's key inside double braces, the same form as the `{{contact.first_name}}` in the subject line of the welcome email in [lead-welcome-workflow.md](lead-welcome-workflow.md). So this field would be written `{{contact.lead_source_detail}}`. The help article on merge fields recommends inserting the token from the picker in the editor, because the picker inserts the exact supported form. No workflow or email using this field was built.

Sources: [How to Create and Use Custom Fields within HighLevel](https://help.gohighlevel.com/support/solutions/articles/48001161579-how-to-use-custom-fields) and [Overview of Merge Fields and Custom Variables](https://help.gohighlevel.com/support/solutions/articles/155000004390-overview-of-merge-fields-custom-variables).

## 2. The custom value

A custom value is one text for the whole sub-account, for things that are the same for every contact (an offer line, a support address). It is created, read, updated and read again.

```
POST /locations/1VrYtmTq0g67GfzDixqN/customValues
{"name": "Welcome offer text", "value": "10 percent off your first lesson"}
201  {"customValue": {"id": "IQcTBKk1DtRaMQAIPk5f", "name": "Welcome offer text",
       "fieldKey": "{{ custom_values.welcome_offer_text }}",
       "value": "10 percent off your first lesson", "locationId": "1VrYtmTq0g67GfzDixqN"}}

GET /locations/1VrYtmTq0g67GfzDixqN/customValues
200  {"customValues": [{"id": "IQcTBKk1DtRaMQAIPk5f", "name": "Welcome offer text",
       "fieldKey": "{{ custom_values.welcome_offer_text }}",
       "value": "10 percent off your first lesson", ...}]}

PUT /locations/1VrYtmTq0g67GfzDixqN/customValues/IQcTBKk1DtRaMQAIPk5f
{"name": "Welcome offer text", "value": "15 percent off your first lesson, this month only"}
200  {"customValue": {..., "value": "15 percent off your first lesson, this month only"}}

GET /locations/1VrYtmTq0g67GfzDixqN/customValues
200  {"customValues": [{"id": "IQcTBKk1DtRaMQAIPk5f", ...,
       "value": "15 percent off your first lesson, this month only"}]}
```

The API hands back the merge field ready to use: `{{ custom_values.welcome_offer_text }}`. The scope the docs name is `locations/customValues.write`.

**Repeat runs.** The script looks the value up by name, puts it back to the first text if it is not there, reads it, then writes the second text and reads again. Every run ends on the second text, so the end state does not depend on how many times it ran.

**How a custom value is used.** From the documentation, not run. HighLevel's help article says a custom value is a reusable placeholder: create it once, insert its merge field wherever the text is needed (emails, workflows, pages), and when it changes, update it once and not in every place. The same article says it can go into workflow email content and subject lines. Nothing here inserted it into a workflow or an email.

Source: [How to use Custom Values](https://help.gohighlevel.com/support/solutions/articles/155000008770-how-to-use-custom-values).

## 3. The calendar and the booking

The calendar list is read first and the calendar is looked up by name. The sub-account already had a calendar that came with its one user (`Team Member Test's Personal Calendar`). It was read and left alone, and a new one was made.

```
POST /calendars/
{"locationId": "1VrYtmTq0g67GfzDixqN", "name": "Discovery call (API demo)",
 "description": "Thirty minute discovery call, weekdays 9 to 5. Made by the API for a demo.",
 "calendarType": "personal", "isActive": true, "autoConfirm": true,
 "slotDuration": 30, "slotDurationUnit": "mins", "slotInterval": 30, "slotIntervalUnit": "mins",
 "allowBookingAfter": 0, "allowBookingAfterUnit": "mins",
 "allowBookingFor": 30, "allowBookingForUnit": "days",
 "teamMembers": [{"userId": "<the sub-account's one user>", "priority": 0.5, "selected": true}],
 "openHours": [{"daysOfTheWeek": [1], "hours": [{"openHour": 9, "openMinute": 0, "closeHour": 17, "closeMinute": 0}]},
               ... the same for days 2, 3, 4 and 5]}
201  {"calendar": {"id": "PchRTsNJux0kjHcHYhm1", "name": "Discovery call (API demo)",
       "calendarType": "personal", "slotDuration": 30, ...}}

GET /calendars/PchRTsNJux0kjHcHYhm1
200  {"calendar": {"id": "PchRTsNJux0kjHcHYhm1", "slotDuration": 30, "slotInterval": 30,
       "isActive": true, "autoConfirm": true,
       "openHours": [days 1 to 5, 9:00 to 17:00]}}
```

The create page lists `openHours` as deprecated in favour of separate Availability endpoints. It was accepted and read back as written, so it is what is used. The scope the docs name is `calendars.write`. A personal calendar needs exactly one team member, so the sub-account's one user (made by earlier work) is the member.

**Free slots, read before booking.** The range is epoch milliseconds, the end is exclusive and the range may not be longer than 31 days.

```
GET /calendars/PchRTsNJux0kjHcHYhm1/free-slots?startDate=1794297600000&endDate=1794902400000
200  {"2026-11-10": {"slots": ["2026-11-10T09:00:00-06:00", "2026-11-10T09:30:00-06:00", ...]}, "2026-11-11": {...}, ...}
```

There were 80 slots in the next 7 days, all on weekdays between 9:00 and 16:30 in the account's time zone (UTC minus 6). The first free weekday slot was taken.

**The booking.** `toNotify` is false, which the docs describe as "the automations will not run", so nothing was sent to anyone.

```
POST /calendars/events/appointments
{"calendarId": "PchRTsNJux0kjHcHYhm1", "locationId": "1VrYtmTq0g67GfzDixqN",
 "contactId": "WVaICbL8tXgSLt8guezy", "assignedUserId": "<the same user>",
 "title": "Discovery call (API demo)", "appointmentStatus": "confirmed",
 "startTime": "2026-11-10T09:00:00-06:00", "endTime": "2026-11-10T09:30:00-06:00",
 "toNotify": false, "ignoreFreeSlotValidation": false}
201  {"id": "Hxms5CEhgDINjAKGU9at", "status": "booked", "appointmentStatus": "confirmed",
       "state": "active", "calendarId": "PchRTsNJux0kjHcHYhm1", "contactId": "WVaICbL8tXgSLt8guezy"}

GET /calendars/events/appointments/Hxms5CEhgDINjAKGU9at
200  {"appointment": {"id": "Hxms5CEhgDINjAKGU9at", "calendarId": "PchRTsNJux0kjHcHYhm1",
       "contactId": "WVaICbL8tXgSLt8guezy", "startTime": "2026-11-10T09:00:00-06:00",
       "endTime": "2026-11-10T09:30:00-06:00", "state": "active", "deleted": false}}

GET /contacts/WVaICbL8tXgSLt8guezy/appointments
200  {"events": [{"id": "Hxms5CEhgDINjAKGU9at", "appointmentStatus": "confirmed",
       "title": "Discovery call (API demo)", "startTime": "2026-11-10 09:00:00",
       "endTime": "2026-11-10 09:30:00", ...}]}

GET /calendars/PchRTsNJux0kjHcHYhm1/free-slots?...   (same range again)
200  79 slots, and 2026-11-10T09:00:00-06:00 is not one of them
```

The scopes the docs name are `calendars/events.write` for the booking and `calendars/events.readonly` for the read. The script checks that the event is on the right calendar, for the right contact and starts at the slot that was offered, and that the slot is gone from the free list afterwards.

**Cleanup of only this booking.** The script deletes the appointment it made and nothing else.

```
DELETE /calendars/events/Hxms5CEhgDINjAKGU9at
200  {"succeeded": true}

GET /contacts/WVaICbL8tXgSLt8guezy/appointments
200  {"events": []}
```

The calendar, the custom field and the custom value stay in the sub-account as evidence. Before booking, the script also looks at the test contact's appointments and deletes any that are active, on this calendar, and for this contact. That clears a leftover from a run that stopped halfway. It never touches another contact's appointment or another calendar's, and the offline test checks both.

## Running it again

The recorded second run (21 calls) logged `custom field already exists`, `custom value already exists` and `calendar already exists`, and created only a new booking, which it read back and deleted. The numbers: 0 new fields, 0 new values, 0 new calendars, 0 new contacts, 1 booking made and removed. The end state is the same every time: one field, one value on its second text, one calendar and no active appointment.

## What went wrong

**The appointment read came back under a different key.** The docs page for `GET /calendars/events/appointments/{eventId}` shows the answer as `{"event": {...}}`. The sandbox answered `{"appointment": {...}}`. The first live run booked the appointment and then stopped with `event is on calendar None`, because the script looked under `event`. That run left its booking behind. The next run found it through the leftover check and deleted it, then stopped on the same key and left its own booking, which the run after that deleted. The script now reads `appointment` first and `event` as a fallback. The lesson in the code is that a field is not trusted until it has been seen in a real answer.

**Status codes differ from the docs in small ways.** The docs show a 200 for creating a calendar and a 201 for deleting an event. The sandbox answered 201 and 200. The script accepts either on each call and still fails on anything else.

**Names with a typo are real.** Answers carry `succeded` next to `succeeded` and `appoinmentStatus` next to `appointmentStatus`. The script reads the correctly spelled keys.

**The contact's appointment list uses another time format.** The single appointment carries an offset (`2026-11-10T09:00:00-06:00`). The contact's list shows the account's local time with no offset (`2026-11-10 09:00:00`). The script matches by id for that reason, not by time.

**Deleting a custom field leaves its value on the contact.** To record the creation responses in this page, the field, the value and the calendar made by the first run were deleted (they were mine) and made again. The contact record still lists the old field id with the old text next to the new one. The read back in the script looks up the new field id, so it passes, and the old entry is a harmless leftover that points at a field that no longer exists.

**Eventual consistency was not seen here.** The earlier notes say the contact list is slow to show a new contact. Every read in this run was by id or by a list that already held the object, and all of them were current at once.

## The code and the tests

| File | What it is |
|---|---|
| [tests/ghl_setup.py](../tests/ghl_setup.py) | The throttle, the HTTP client (no token in the transcript), the request builders, the response checks and the three parts. It makes no call by itself. |
| [tests/custom_fields_values_calendars.py](../tests/custom_fields_values_calendars.py) | The live run. Opt in: it exits 2 with a SKIP line unless `GHL_SANDBOX_PIT_FILE` names a token file, like `tests/tenant_isolation.py`. `GHL_CFVC_TRANSCRIPT` writes every call as JSON. |
| [tests/ghl_setup.test.py](../tests/ghl_setup.test.py) | 53 offline checks against a stand-in server on 127.0.0.1. CI runs it with no token. |
| [tests/ghl_setup_mutants.py](../tests/ghl_setup_mutants.py) | 25 mutants. Each breaks one guard in a copy of `ghl_setup.py` and the offline test has to go red. |

The stand-in server is written from the docs pages and from the answers above (shapes only). It refuses a missing token, a missing `Version` header, a booking on a slot it did not offer and more than 25 requests in 10 seconds, counted on a fake clock the throttle advances. The tests check the positive control first (all three parts finish and leave one field, one value, one calendar and no active booking), a repeat run, a missing scope on each part in turn (the exact text `The token is not authorized for this scope.` is kept and the other parts still run), a contact write that is accepted and not stored, a Saturday offer, a bad token and the rule that only our own appointment is deleted.

Measured on 2026-11-10: the offline test passed 53 of 53 and all 25 mutants were caught. The live run passed three times against the sandbox, plus the two runs that stopped on the `appointment` key.

```
python tests/ghl_setup.test.py
python tests/ghl_setup_mutants.py
GHL_SANDBOX_PIT_FILE=<token file> python tests/custom_fields_values_calendars.py
```

## Limits

- **No workflow uses the field or the value.** The merge field forms above are from HighLevel's help pages and from the keys the API returned. No workflow, email, text or page was built with them in the UI, and nothing was rendered.
- **No email or text was sent.** The booking has `toNotify` set to false. The sandbox also blocks email and text sending for its agency, as the other pages show.
- **Sandbox only.** One sandbox sub-account, one Private Integration Token, one user. Nothing ran on a real sub-account, a real calendar or a real lead.
- **Invented data.** `Test Lead`, `test.lead@example.com`, a made-up offer text and a made-up calendar.
- **One booking path.** One personal calendar with one team member, booked on a slot the free slot list offered. Not tried: round robin or class calendars, a calendar with a form, payments, recurring bookings, a booking made from the public booking page, rescheduling, the Availability endpoints, and a booking on a slot that was not offered.
- **One data type.** The custom field is plain text on the contact. Dropdowns, dates, files and opportunity fields were not made.
- **The time zone was read, not set.** The slots came back in the account's zone (UTC minus 6). The script did not change any time zone setting.
- **No scope or plan block was met.** The token had every scope these calls need, and the sandbox plan did not refuse the booking. The three refusal messages for a missing scope are covered only by the stand-in, which repeats the text the sandbox gave in earlier work.
- **The stand-in is not HighLevel.** It proves the script's logic and its checks, not HighLevel's behaviour.
