#!/usr/bin/env python3
# LIFESPAN: permanent
"""
Mutation check for sms_bridge: each mutant breaks ONE guard in a temp copy of the package and the test suite, run
against that copy, must go red (pytest exit code 1, with the first failing test named). A mutant whose anchor is not
found exactly once is STALE and fails the run, so a reworded line cannot turn this into a quiet pass. The unmutated
copy is run first and must pass, so a red run is the mutant's doing.

Usage:  python tests/mutants.py [name filter ...]   (run from sms-provider/ or anywhere)
"""
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = 4

# (name, file, anchor found exactly once, replacement)
MUTANTS = [
    # the shared secret
    ("the secret is not checked", "app.py", 'if not token_ok(bridge.cfg.bridge_token, query_token, self.headers.get("X-Bridge-Token")):', "if False:"),
    ("any non-empty secret is accepted", "app.py", 'if given and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):', "if given:"),
    ("the query secret is ignored", "app.py", "for given in (query_token, header_token):", "for given in (header_token,):"),
    ("the header secret is ignored", "app.py", "for given in (query_token, header_token):", "for given in (query_token,):"),
    ("an unknown route is served", "app.py", "if parts.path != ROUTE:", "if False:"),
    ("the health route is gone", "app.py", 'if urlsplit(self.path).path == "/health":', "if False:"),
    ("the request line with the secret is logged", "app.py", 'log.info("%s %s", self.command, urlsplit(self.path).path)', 'log.info("%s", self.requestline)'),
    ("an unread body is left on a kept-alive connection", "app.py", "if not self.body_read:", "if False:"),
    # the body
    ("a body that is not UTF-8 JSON crashes the handler", "app.py", "except (UnicodeDecodeError, ValueError):", "except UnicodeDecodeError:"),
    ("a JSON list is accepted as a body", "app.py", "if not isinstance(body, dict):", "if False:"),
    ("messageId is optional", "app.py", "if not isinstance(message_id, str) or not message_id.strip():", "if False:"),
    ("type is optional", "app.py", "if not isinstance(kind, str) or not kind.strip():", "if False:"),
    ("any type is sent as a text", "app.py", 'if kind != "SMS":', "if False:"),
    ("the type is matched without case", "app.py", 'if kind != "SMS":', 'if kind.upper() != "SMS":'),
    ("the phone is not checked", "app.py", "if not isinstance(phone, str) or not PHONE.match(phone.strip()):", "if False:"),
    ("the phone pattern accepts anything", "app.py", 'PHONE = re.compile(r"^\\+[1-9][0-9]{6,14}$")', 'PHONE = re.compile(r"^.*$")'),
    ("the phone is sent untrimmed", "app.py", '"phone": phone.strip()', '"phone": phone'),
    ("the text is optional", "app.py", "if not isinstance(text, str) or not text.strip():", "if False:"),
    ("the text length is not capped", "app.py", "if len(text) > MAX_TEXT:", "if False:"),
    ("attachments may be any value", "app.py", "if not isinstance(attachments, list):", "if False:"),
    ("the body size is not capped", "app.py", "if length > MAX_BODY:", "if False:"),
    ("a negative Content-Length is read", "app.py", "if length < 0:", "if False:"),
    # HighLevel's Ed25519 signature
    ("the signature is never checked", "app.py", 'if bridge.cfg.ghl_signature_mode != "off":', "if False:"),
    ("the signature is checked in off mode", "app.py", 'if bridge.cfg.ghl_signature_mode != "off":', "if True:"),
    ("enforce accepts a bad signature", "app.py", 'if bridge.cfg.ghl_signature_mode == "enforce":', "if False:"),
    ("warn refuses like enforce", "app.py", 'if bridge.cfg.ghl_signature_mode == "enforce":', 'if True:'),
    ("a bad signature is accepted whatever the verdict", "app.py", "if verdict != signature.OK:", "if False:"),
    ("warn does not log a mismatch", "app.py", 'log.warning("signature check failed (%s); accepted because GHL_SIGNATURE_MODE is warn", verdict)', "pass"),
    ("warn logs the signature header", "app.py", 'accepted because GHL_SIGNATURE_MODE is warn", verdict)', 'accepted because GHL_SIGNATURE_MODE is warn %s", verdict, self.headers.get("X-GHL-Signature"))'),
    ("warn logs the request line with the secret", "app.py", 'accepted because GHL_SIGNATURE_MODE is warn", verdict)', 'accepted because GHL_SIGNATURE_MODE is warn %s", verdict, self.path)'),
    ("the signature is checked over re-serialised JSON", "app.py", "signature.check(self.headers, raw, bridge.ghl_key)", 'signature.check(self.headers, json.dumps(json.loads(raw)).encode("utf-8"), bridge.ghl_key)'),
    ("the body is validated before the signature", "app.py", '            if bridge.cfg.ghl_signature_mode != "off":\n',
     '            try:\n                validate(raw)\n            except Rejected as exc:\n                return self._send(exc.status, {"error": exc.reason})\n            if bridge.cfg.ghl_signature_mode != "off":\n'),
    ("the key is loaded even when the check is off", "app.py", 'if cfg.ghl_signature_mode != "off" else None', "if True else None"),
    ("the check uses the published key whatever is configured", "app.py", "signature.check(self.headers, raw, bridge.ghl_key)", "signature.check(self.headers, raw, signature.load_key(signature.GHL_ED25519_SPKI_B64))"),
    ("the legacy header is accepted", "signature.py", 'value = headers.get("X-GHL-Signature")', 'value = headers.get("X-GHL-Signature") or headers.get("X-WH-Signature")'),
    ("a missing header passes", "signature.py", 'return LEGACY_ONLY if headers.get("X-WH-Signature") else MISSING', "return OK"),
    ("a legacy only post is reported as missing", "signature.py", 'return LEGACY_ONLY if headers.get("X-WH-Signature") else MISSING', "return MISSING"),
    ("an invalid signature passes", "signature.py", "except InvalidSignature:\n        return BAD", "except InvalidSignature:\n        return OK"),
    ("the verification is skipped", "signature.py", "key.verify(sig, raw)", "pass"),
    ("the signature is verified over an empty body", "signature.py", "key.verify(sig, raw)", 'key.verify(sig, b"")'),
    ("a header that is not base64 crashes the handler", "signature.py", "except (binascii.Error, ValueError):\n        return BAD", "except ZeroDivisionError:\n        return BAD"),
    ("a key that is not Ed25519 is accepted", "signature.py", "if not isinstance(key, Ed25519PublicKey):", "if False:"),
    ("the published key is wrong", "signature.py", 'GHL_ED25519_SPKI_B64 = "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg="', 'GHL_ED25519_SPKI_B64 = "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOh="'),
    ("the default mode is enforce", "config.py", 'ghl_signature_mode: str = "off"', 'ghl_signature_mode: str = "enforce"'),
    ("the mode is case sensitive", "config.py", '.strip().lower() or Config.ghl_signature_mode', '.strip() or Config.ghl_signature_mode'),
    ("an unknown mode is accepted", "config.py", "if mode not in SIGNATURE_MODES:", "if False:"),
    ("the mode is read but never used", "config.py", "ghl_signature_mode=mode,", 'ghl_signature_mode="off",'),
    ("the key override works outside test", "config.py", 'if override and env.get("BRIDGE_ENV", "").strip() == "test":', "if override:"),
    ("the test environment name is matched without case", "config.py", '.strip() == "test":', '.strip().lower() == "test":'),
    ("the key override is read but never used", "config.py", "ghl_public_key_b64=key,", "ghl_public_key_b64=signature.GHL_ED25519_SPKI_B64,"),
    ("a bad key is accepted at start", "config.py", "        signature.load_key(key)\n    except ValueError", "        pass\n    except ValueError"),
    # idempotency and the store
    ("a replayed message is sent again", "store.py", 'if old is not None and not old.get("retryable"):', "if False:"),
    ("an unanswered post may be sent again", "app.py", '"No answer from the gateway (%s)" % exc)', '"No answer from the gateway (%s)" % exc, retryable=True)'),
    ("a refused text cannot be tried again", "app.py", '"The gateway refused the text (%d)" % exc.status, retryable=True)', '"The gateway refused the text (%d)" % exc.status, retryable=False)'),
    ("the attempt count never grows", "store.py", '"attempts": (old or {}).get("attempts", 0) + 1', '"attempts": 1'),
    ("the store is not written after an update", "store.py", "rec.update(fields)\n            self._flush()", "rec.update(fields)"),
    # the gateway
    ("the send carries no basic auth", "gateway.py", 'json=body, auth=self.auth, timeout=self.timeout)', "json=body, timeout=self.timeout)"),
    ("the status poll carries no basic auth", "gateway.py", '"/message/" + gateway_id, auth=self.auth, timeout=self.timeout)', '"/message/" + gateway_id, timeout=self.timeout)'),
    ("phoneNumbers is not a list", "gateway.py", '"phoneNumbers": [phone]}', '"phoneNumbers": phone}'),
    ("simNumber is never sent", "gateway.py", 'body["simNumber"] = self.sim_number', "pass"),
    ("simNumber is always sent", "gateway.py", "if self.sim_number is not None:", "if True:"),
    ("a refused send is taken for an accepted one", "gateway.py", "if resp.status_code not in (200, 201, 202):", "if False:"),
    ("Failed is not a final state", "gateway.py", 'TERMINAL = ("Delivered", "Failed")', 'TERMINAL = ("Delivered",)'),
    ("Delivered is not a final state", "gateway.py", 'TERMINAL = ("Delivered", "Failed")', 'TERMINAL = ("Failed",)'),
    ("the polling deadline is ignored", "gateway.py", "if time.monotonic() >= end:", "if False:"),
    ("the gateway's error text is dropped", "gateway.py", "error = _recipient_error(data) or error", "error = error"),
    ("every state is read as Delivered", "gateway.py", 'state = str(data.get("state", state))', 'state = "Delivered"'),
    # what is reported to HighLevel
    ("a timeout is reported as delivered", "app.py", 'self._finish(mid, "failed", "timeout",', 'self._finish(mid, "delivered", "timeout",'),
    ("anything but Failed is reported as delivered", "app.py", 'if result["state"] == "Delivered":', 'if result["state"] != "Failed":'),
    ("a failure with no text has no reason", "app.py", 'reason = result["error"] or "unknown"', 'reason = result["error"]'),
    ("a failure carries no error object", "app.py", 'error = {"code": code, "type": "sms_gateway", "message": text} if status == "failed" else None', "error = None"),
    ("a delivery carries an error object", "app.py", 'if status == "failed" else None', "if True else None"),
    ("the callback is not recorded as sent", "app.py", "self.store.update(mid, reported=True)", "pass"),
    ("a refused callback is not recorded", "app.py", "self.store.update(mid, reported=False, reportError=str(exc))", "pass"),
    ("the callback sends no bearer word", "highlevel.py", '"Authorization": "Bearer " + self._token', '"Authorization": self._token'),
    ("the callback sends no Version header", "highlevel.py", '"Version": self.version, ', ""),
    ("the callback is a POST", "highlevel.py", "self.session.put(url, json=body", "self.session.post(url, json=body"),
    ("the message id is not encoded in the path", "highlevel.py", 'quote(message_id, safe="")', "message_id"),
    ("a refused callback counts as accepted", "highlevel.py", "if resp.status_code != 200:", "if False:"),
    ("a missing HighLevel token is silent", "highlevel.py", 'raise HighLevelError("HIGHLEVEL_ACCESS_TOKEN is not set")', "pass"),
    ("a token picks no reporter", "app.py", "if cfg.highlevel_token else NoReporter()", "if False else NoReporter()"),
    # config and the entry point
    ("a missing variable is not named", "config.py", "    if missing:\n", "    if False:\n"),
    ("a short bridge token is accepted", "config.py", 'if len(env["BRIDGE_TOKEN"]) < 16:', "if False:"),
    ("a number of zero or less is accepted", "config.py", "if value <= 0:", "if False:"),
    ("the bridge token is in repr", "config.py", "bridge_token: str = field(repr=False)", "bridge_token: str"),
    ("the gateway password is in repr", "config.py", "gateway_password: str = field(repr=False)", "gateway_password: str"),
    ("the HighLevel token is in repr", "config.py", "highlevel_token: str | None = field(default=None, repr=False)", "highlevel_token: str | None = None"),
    ("a bad configuration exits 0", "app.py", '        return 2\n', '        return 0\n'),
]


def run_suite(workdir):
    res = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--tb=line"],
                         cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    return res.returncode, res.stdout


def copy_tree(dest):
    for name in ("sms_bridge", "tests", "pytest.ini"):
        src = os.path.join(ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, name), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy(src, dest)


def one(item):
    name, fname, old, new, base = item
    with tempfile.TemporaryDirectory() as tmp:
        copy_tree(tmp)
        path = os.path.join(tmp, "sms_bridge", fname)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(base[fname].replace(old, new))
        try:
            code, out = run_suite(tmp)
        except subprocess.TimeoutExpired:
            return name, "caught", "(the suite hung)"
    failed = re.search(r"^FAILED (\S+)", out, re.M) or re.search(r"^(tests[\\/]\S+?):\d+: ", out, re.M)
    if code == 1:
        return name, "caught", failed.group(1) if failed else "?"
    return name, ("SURVIVED" if code == 0 else "ERROR"), out.strip().splitlines()[-1] if out.strip() else ""


def main():
    only = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
    base = {}
    for fname in {m[1] for m in MUTANTS}:
        with open(os.path.join(ROOT, "sms_bridge", fname), encoding="utf-8") as fh:
            base[fname] = fh.read()   # universal newlines: a CRLF checkout must not make an anchor stale
    with tempfile.TemporaryDirectory() as tmp:
        copy_tree(tmp)
        code, out = run_suite(tmp)
    if code != 0:
        print("BASELINE FAILS, no mutant was run:\n" + out[-2000:])
        return 1
    todo, bad = [], 0
    for name, fname, old, new in MUTANTS:
        if only and not any(o in name.lower() for o in only):
            continue
        n = base[fname].count(old)
        if n != 1:
            print("STALE    %s (anchor found %d times)" % (name, n))
            bad += 1
        else:
            todo.append((name, fname, old, new, base))
    caught = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as pool:
        for name, verdict, detail in pool.map(one, todo):
            print("%-8s %s  [%s]" % (verdict, name, detail))
            if verdict == "caught":
                caught += 1
            else:
                bad += 1
    print("SMS BRIDGE MUTANTS: %d of %d caught, %d problem(s)" % (caught, len(todo), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
