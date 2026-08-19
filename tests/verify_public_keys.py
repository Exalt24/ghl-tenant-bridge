"""Prove the keys in the SHIPPED source file are byte-identical to the ones
published in HighLevel's official guide.

This exists because I fabricated the RSA key body on the first write: I had only
its first 60 characters and filled the rest with plausible base64. A wrong public
key does not crash, it silently fails every verification, which is the worst
possible failure mode for a security primitive. So the shipped artifact gets
compared against the scraped source, not eyeballed.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

SHIPPED = Path(r"C:\Projects\Professional\Projects\Apps\ghl-tenant-bridge\src\lib\ghl-signature.ts")
SCRAPED = Path(r"C:\Users\Dax\AppData\Local\Temp\claude\C--Projects-Professional\491b0370-1067-441c-a628-29b572867fed\scratchpad\ghl_keys.json")

src = SHIPPED.read_text(encoding="utf-8")
truth = json.loads(SCRAPED.read_text(encoding="utf-8"))


def norm(pem):
    return "".join(re.sub(r"-----[A-Z ]+-----", "", pem).split())


pems = re.findall(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", src, re.S)
print("PEM blocks in shipped file: " + str(len(pems)))

fails = []
shipped_bodies = {norm(p) for p in pems}
for name, pem in truth.items():
    body = norm(pem)
    if body in shipped_bodies:
        print("  MATCH   " + name + "  (" + str(len(body)) + " b64 chars)")
    else:
        fails.append(name + " in the shipped file does NOT match the scraped key")
        print("  MISMATCH " + name)

# Any PEM in the file that is NOT one of the two real keys is fabricated.
truth_bodies = {norm(p) for p in truth.values()}
for b in shipped_bodies:
    if b not in truth_bodies:
        fails.append("shipped file contains an UNKNOWN/fabricated key: " + b[:48] + "...")

# And they must actually load.
node = subprocess.run(
    ["node", "-e", """
const crypto=require('crypto');
const fs=require('fs');
const src=fs.readFileSync(process.argv[1],'utf8');
const pems=src.match(/-----BEGIN PUBLIC KEY-----[\\s\\S]*?-----END PUBLIC KEY-----/g)||[];
let out=[];
for (const p of pems){
  try{const k=crypto.createPublicKey(p);out.push('OK '+k.asymmetricKeyType);}
  catch(e){out.push('FAIL '+e.message.slice(0,80));}
}
console.log(out.join('|'));
""", str(SHIPPED)],
    capture_output=True, text=True, timeout=60)
loaded = (node.stdout or "").strip()
print("node crypto load: " + loaded + (("  stderr=" + node.stderr[:120]) if node.returncode else ""))
if "FAIL" in loaded or "ed25519" not in loaded or "rsa" not in loaded:
    fails.append("shipped keys did not both load as rsa + ed25519: " + loaded)

print()
if fails:
    print("FAIL (" + str(len(fails)) + ")")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("PASS: both shipped keys are byte-identical to HighLevel's published keys and both load")
