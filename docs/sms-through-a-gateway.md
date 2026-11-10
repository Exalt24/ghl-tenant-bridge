# Sending HighLevel texts through an Android SMS gateway

The HighLevel sandbox refuses to send a text ("Trial mode not enabled for this agency", see [pipelines-funnels-memberships.md](pipelines-funnels-memberships.md)). HighLevel lets an account add a custom Conversation Provider, which is a Delivery URL that gets every outbound message. This page describes a small bridge in [`sms-provider/`](../sms-provider/) that answers that URL and sends the text through the [android-sms-gateway](https://github.com/capcom6/android-sms-gateway) app on a phone, using its local server API. It then tells HighLevel whether the text was delivered.

This is code, a test suite and one real run. The suite uses stand-in servers written from the documentation. On 2026-11-10 one text sent from the HighLevel sandbox through a custom Conversation Provider reached the bridge over a Cloudflare tunnel and went out from a real phone (see [A real run](#a-real-run-2026-11-10)). The status call back to HighLevel has not run against the real API.

## What it does

```
HighLevel  --POST /outbound?token=...-->  bridge  --POST /message-->  gateway app on the phone  --> carrier
HighLevel  <--PUT .../messages/{id}/status--  bridge  <--GET /message/{id} (polled)--  gateway app
```

1. HighLevel posts each outbound message to the Delivery URL.
2. The bridge checks the secret, then (when `GHL_SIGNATURE_MODE` is `warn` or `enforce`) the Ed25519 signature over the raw body, then checks the body, and records the `messageId` in a small JSON file.
3. It posts the text to the gateway and answers HighLevel `200` as soon as the gateway has queued it.
4. In the background it polls `GET /message/{id}` until the state is `Delivered` or `Failed`, or 60 seconds pass.
5. It tells HighLevel the result with the update message status call.

The answers to HighLevel:

| Status | When |
|---|---|
| 200 | The text was queued on the gateway, or the `messageId` was seen before (`"duplicate": true`, nothing is sent). |
| 400 | The body is not a JSON object, or `messageId`, `phone` (E.164, like `+15864603685`) or `message` is missing or bad, or the text is over 1600 characters. |
| 401 | The secret is missing or wrong (checked before the body is read), or `GHL_SIGNATURE_MODE=enforce` and `X-GHL-Signature` is missing, not valid or does not match the body. |
| 413 | The body is over 64 KB. |
| 422 | `type` is not `SMS`. Email deliveries are refused and nothing is sent. |
| 502 | The gateway refused the text or did not answer. HighLevel is told `failed`. |

A replay of a `messageId` is never sent twice. One exception: when the gateway answered with a non-2xx status, nothing was queued on the phone, so the same `messageId` may be tried again. A post to the gateway that got no answer at all is treated as possibly sent and is never repeated.

Attachments are ignored. A text goes out as plain SMS, and the response says how many attachments were dropped (`attachmentsIgnored`).

## What HighLevel's documentation says

Read on 2026-11-10 from these pages:

- [Conversation Providers](https://marketplace.gohighlevel.com/docs/marketplace-modules/ConversationProviders.md)
- [Conversation Provider, Outbound Message](https://marketplace.gohighlevel.com/docs/webhook/ProviderOutboundMessage.md)
- [Update message status](https://marketplace.gohighlevel.com/docs/ghl/conversations/update-message-status.md), and its [2021-04-15 version](https://marketplace.gohighlevel.com/docs/2021-04-15/ghl/conversations/update-message-status.md)

**The outbound body.** The schema lists `contactId`, `locationId`, `messageId`, `emailMessageId`, `type` (`SMS` or `Email`), `attachments` (an array, the examples hold URL strings), `message`, `phone`, `emailTo`, `emailFrom`, `html`, `subject` and `userId`. The SMS example is:

```json
{
  "contactId": "GKBhT6BfwY9mjzXAU3sq",
  "locationId": "GKAWb4yu7A4LSc0skQ6g",
  "messageId": "GKJxs4P5L8dWc5CFUITM",
  "type": "SMS",
  "phone": "+15864603685",
  "message": "The text message to be sent to the contact",
  "attachments": ["https://www.google.com/images/branding/googlelogo/1x/googlelogo_color_272x92dp.png"],
  "userId": "GK56r6wdJDrkUPd0xsmx"
}
```

There is no `conversationProviderId` in the documented body, so the bridge does not read one. The pages do not say what answer HighLevel expects from the Delivery URL, so the bridge answers `200` with a small JSON body.

**How the call is authenticated.** The pages say Delivery URL posts carry `X-GHL-Signature` (Ed25519, over the body) and no other signature header. They describe no shared secret and no way to add a custom header. The Delivery URL is a string you type, so the bridge takes its secret as a query parameter: `https://<host>/outbound?token=<BRIDGE_TOKEN>`. It also accepts an `X-Bridge-Token` header for a caller that can set one (a reverse proxy, a test). The secret is compared in constant time and the request line is never logged, because it holds the secret. On top of that the bridge can verify the signature, described next.

**What it verifies.** With `GHL_SIGNATURE_MODE` set to `warn` or `enforce`, the bridge checks `X-GHL-Signature` over the exact bytes of the request body, before the body is parsed. It uses the `cryptography` package's Ed25519 verification (the same package and the same published key as the relay in [`relay/`](../relay/) and the Worker in [`edge/`](../edge/)), so there is no curve code in the bridge and the comparison is constant time. The legacy `X-WH-Signature` header (RSA) is never accepted: a post that carries only that header counts as unsigned, and a legacy header beside a wrong `X-GHL-Signature` does not rescue it. A header that is missing, empty, not base64 (HighLevel sometimes sends the literal `N/A`), truncated or from another key is a mismatch. The check runs after the secret check and before the body is validated, so in `enforce` mode an unsigned body is a `401` whatever it contains.

The public key is the one HighLevel publishes in the [Webhook Integration Guide](https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide) (read on 2026-11-10; the page has no separate authentication page, its Security section is the source): `MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=`, the base64 of the key's SPKI DER. The page states that legacy `X-WH-Signature` "will be deprecated on September 1, 2026" and that the current header is the Ed25519 one. It says "we sign the webhook body with our private key" but not whether that means the received bytes or a re-serialised body, and its sample code verifies `JSON.stringify(req.body)`. The bridge verifies the received bytes only, as the Worker does. The relay also tries the compact re-serialisation, which this bridge does not.

| `GHL_SIGNATURE_MODE` | A good signature | A missing or wrong signature |
|---|---|---|
| `off` (default) | Accepted | Accepted, the header is not read. Today's token only behaviour. |
| `warn` | Accepted | Accepted, and one warning line in the log naming the reason (no header, only the legacy header, or no match). The log never holds the signature, the secret or the text. |
| `enforce` | Accepted | `401`, nothing is sent and nothing is stored, so the same `messageId` can still arrive correctly signed later. |

Start in `warn`, look at the log for the first real delivery, and move to `enforce` only when a real post verifies. Because the signed bytes are not documented and no real post has been seen, `enforce` could refuse every real post if HighLevel signs something other than the bytes it sends.

**The status call.** The method is `PUT`, not `POST`:

```
PUT https://services.leadconnectorhq.com/conversations/messages/{messageId}/status
Authorization: Bearer <token>
Version: 2021-04-15
{"status": "failed", "error": {"code": "RESULT_MODEM_ERROR", "type": "sms_gateway", "message": "..."}}
```

`status` is one of `delivered`, `failed`, `pending`, `read`. `error` is optional, and when present `code`, `type` and `message` are all strings. The scope is `conversations/message.write`. The `Version` header differs between page versions: the current page lists `v3` and the 2021-04-15 page lists `2021-04-15`. The bridge sends `2021-04-15` and `HIGHLEVEL_API_VERSION` changes it. The Conversation Providers page also says message statuses can only be updated with the token of the marketplace app that owns the provider, so a Private Integration Token or another app's token may be refused. That was not tried.

## What the gateway does

Seen on 2026-11-10 against the gateway app on a phone, before the bridge was written: `POST /message` answers `202` with state `Pending`. The message then goes `Pending`, `Processed`, `Sent`, `Delivered`, or ends in `Failed` with a `recipients[].error` such as `RESULT_MODEM_ERROR`. The bridge posts `{"message": text, "phoneNumbers": [phone], "simNumber": SIM_NUMBER}` with basic auth. `simNumber` is left out when `SIM_NUMBER` is not set. The stand-in gateway in the tests repeats this sequence. It is not the app.

How the final state is mapped:

| Gateway | Reported to HighLevel |
|---|---|
| `Delivered` | `delivered` |
| `Failed` | `failed`, with `error.code` set to the gateway's error (`RESULT_MODEM_ERROR`) or `unknown` |
| Still `Pending`, `Processed` or `Sent` after `POLL_DEADLINE` (60 s) | `failed`, with `error.code` `timeout` and the last state in the message |
| Gateway refused or did not answer the send | `failed`, `error.code` `gateway` |

## How it was tested

`sms-provider/tests/` runs the bridge on a local port and talks to it over real HTTP. A stand-in gateway and a stand-in HighLevel run beside it, each on its own port. They are written from the documentation above and check what the real ones are documented to check: basic auth on the gateway, a bearer token, a `Version` header, the `PUT` method, the status values and the shape of the `error` object on the HighLevel side. Each stand-in has a positive control test, so a refusal in a later test is a refusal for the reason named.

The signature tests (`tests/test_signature.py`) sign requests with a key pair generated by the test run and cover a good signature, a flipped byte in the body, another key, no header, the legacy header only, a legacy header beside a wrong signature, `N/A`, an empty and a truncated signature, and a signature made over the re-serialised body, each in `off`, `warn` and `enforce`, plus the order of the checks and the configuration rules. The suite covers the happy path, a replay (also while the first is still in flight, and eight at once), a bad or missing secret in the query and in the header, a body that is not JSON, bad fields, an Email delivery, an oversized body, the gateway answering 401, the `Failed` state with a modem error, a gateway that never finishes, a poll that fails once, and the status call being refused by HighLevel. It also checks that no secret reaches the log, the store file or a configuration repr.

Run on 2026-11-10:

```
cd sms-provider
python -m pytest -q                # 119 passed
python tests/mutants.py            # SMS BRIDGE MUTANTS: 88 of 88 caught, 0 problem(s)
```

The mutation check copies the package, breaks one guard per mutant (a removed secret check, a dropped basic auth header, `PUT` changed to `POST`, `Failed` no longer final, and so on) and requires the suite to go red. The signature guards each have a mutant too (no check, wrong mode, legacy header accepted, re-serialised bytes, a key override outside the test environment, and so on). Two mutants survived on the first run of the earlier set: with `Failed` or `Delivered` not treated as final, the bridge still got the right answer but kept polling until the deadline. The tests now count the polls, and both are caught. CI runs both commands in the `sms-provider` job.

## A real run (2026-11-10)

What ran, in order:

1. The gateway app was on the phone with the local server on, reached from the PC with `adb forward tcp:18080 tcp:8080`.
2. The bridge ran on the PC with `GHL_SIGNATURE_MODE=warn` and `SIM_NUMBER=2`, behind `cloudflared tunnel --url http://127.0.0.1:8787`.
3. In the HighLevel developer portal a private app was created in the free sandbox, with the conversation and contact scopes, a client key, a redirect URL and one Conversation Provider (type SMS, a custom provider) whose Delivery URL was the tunnel address plus `/outbound?token=...`. The module's Create button stayed disabled until the Auth section and a client key existed.
4. The app was installed in one sandbox sub-account through its install link.
5. A contact was created with a real mobile number, and a message was posted to `POST /conversations/messages` with the provider's id. HighLevel recorded it as `TYPE_CUSTOM_SMS` from "Phone SMS bridge".

What came back:

| Send | Bridge answer | Gateway | HighLevel's record |
|---|---|---|---|
| First, default SIM | 502 | `Failed`, `RESULT_MODEM_ERROR` on SIM 1 | `failed`, "Request failed with status code 502", a Try again link in the conversation |
| Second, `SIM_NUMBER=2` | 200 | `Delivered` | `sent` |

The conversation as HighLevel shows it, with the failed send above the delivered one:

![The HighLevel conversation with two outbound texts, the first marked failed with a Try again link and the second sent](screenshots/sms-gateway-live/highlevel-conversation.png)

Limits of this run: one contact, a handful of texts, one phone, sandbox data. The status stayed `sent` and not `delivered` in HighLevel because the bridge had no access token for the status call back. The install redirected the one time OAuth code to the tunnel's `/oauth` path, which the bridge does not serve, so no token was exchanged. The Delivery URL post was accepted in `warn` mode, and no signed post from HighLevel has been seen to verify yet, so `enforce` is still untried.

## How to run it

Python 3.11 or newer, with `requests` and `cryptography`:

```
cd sms-provider
pip install -r requirements.txt
```

Settings are environment variables:

| Variable | Needed | Meaning |
|---|---|---|
| `BRIDGE_TOKEN` | yes | The shared secret, at least 16 characters. |
| `GATEWAY_URL` | yes | The gateway's local server address, for example `http://127.0.0.1:18080`. |
| `GATEWAY_USER` | yes | The gateway's basic auth user. |
| `GATEWAY_PASSWORD` | yes | The gateway's basic auth password. |
| `SIM_NUMBER` | no | Which SIM to send from (1 or 2). Left out of the request when unset. |
| `HIGHLEVEL_ACCESS_TOKEN` | no | Token for the status call. Without it texts still go out, and the bridge only logs and stores the result. |
| `HIGHLEVEL_API_VERSION` | no | The `Version` header for the status call. Default `2021-04-15`. |
| `HIGHLEVEL_API_BASE` | no | Default `https://services.leadconnectorhq.com`. |
| `GHL_SIGNATURE_MODE` | no | `off`, `warn` or `enforce`, see above. Default `off`. Anything else stops the bridge at start. |
| `GHL_PUBLIC_KEY_B64` | no | Replaces the published key, and is read only when `BRIDGE_ENV` is exactly `test` (the same rule as the Worker and the relay). Elsewhere it is ignored. A value that is not an Ed25519 public key stops the bridge at start. |
| `BRIDGE_HOST`, `BRIDGE_PORT` | no | Default `127.0.0.1` and `8787`. |
| `STORE_PATH` | no | The replay file. Default `sms_store.json` in the working directory. |
| `POLL_INTERVAL`, `POLL_DEADLINE` | no | Seconds between polls and the total wait. Default 1 and 60. |

Reach the phone from the PC over USB with adb, then start the bridge:

```
adb forward tcp:18080 tcp:8080

BRIDGE_TOKEN=... GATEWAY_URL=http://127.0.0.1:18080 GATEWAY_USER=... GATEWAY_PASSWORD=... \
SIM_NUMBER=1 HIGHLEVEL_ACCESS_TOKEN=... \
python -m sms_bridge
```

`GET /health` answers `{"ok": true}` without a secret.

To give HighLevel a public address for free, use a Cloudflare quick tunnel:

```
cloudflared tunnel --url http://127.0.0.1:8787
```

It prints a `https://<random>.trycloudflare.com` address. Put `https://<random>.trycloudflare.com/outbound?token=<BRIDGE_TOKEN>` in the provider's Delivery URL. The address changes every time the tunnel restarts, and anyone with the full URL can send texts from the phone, so treat the URL as a secret.

## Limits

- **A personal phone and SIM are the sender.** The recipient sees a normal mobile number, and the texts use that SIM's plan.
- **Delivery depends on the carrier.** `Delivered` needs a delivery report from the carrier. A text can be sent and never confirmed, and it is then reported `failed` after 60 seconds even if it arrives later. Do not resend on that alone.
- **Not for bulk or production.** One phone, one SIM, one message queue, polled one text at a time, a tunnel that restarts. Carriers and the SMS rules for business texting (registration, opt out handling) are not handled here.
- **Not a replacement for a registered provider.** It gets a test text out of a sandbox. It does not give a registered sender, throughput or support.
- **The signature check was tested only with a locally generated key pair.** No post signed by HighLevel has reached the bridge, and HighLevel's private key is not ours, so it is not known that a real post verifies over the received bytes. The default is `off`, where the secret in the URL is the whole check and a leaked URL lets anyone send.
- **Texts only.** Email deliveries get a 422, attachments are dropped, and there are no inbound texts: a reply to the phone does not reach HighLevel.
- **Restarts.** If the bridge stops while it is polling, that message stays unreported and a replay of it is answered as a duplicate, not resent. The store file grows by one small entry per message and is never pruned.
- **The status call is unproven.** The method, headers and body come from HighLevel's pages. The token rule (only the provider app's token may update a status) and the `Version` value have not been tried against HighLevel.
- **One real run, not a load test.** One text went through on 2026-11-10 after a first one failed on a SIM modem error. The status call back (`PUT` message status) has only run against the stand-in, because the bridge was started without the app's access token.
