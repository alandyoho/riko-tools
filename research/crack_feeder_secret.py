#!/usr/bin/env python3
"""
crack_feeder_secret.py — find the Neakasa FEEDER app's HMAC secret against a
captured request, testing every candidate string across every reasonable
interpretation and sign-formula variant.

The mistake this corrects: earlier we tested candidate strings only as a verbatim
HMAC key, compared against an uppercased sign. But (a) the captured feeder sign is
MIXED CASE, so an uppercase comparison would reject the true secret, and (b) the
secret might be used hex-decoded, or the message order/format might differ. Any one
of those makes a correct candidate look wrong.

Offline. The oracle is a real captured request:
    sign      = 69gizY6/qeayaMr5BqfhtV4Zbolo3gysfy4amSmNo6M=
    app_key   = 32711645
    timestamp = 1788815260
A candidate is correct iff some (interpretation, formula) reproduces that sign.

Feed it candidate strings on stdin or via --file (one per line). Extract them with:
    strings -n 8 libapp.so | sort -u > cands.txt
    grep -rhoE '[A-Za-z0-9+/=_-]{8,64}' sources/ | sort -u >> cands.txt
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import sys

APP_KEY = "32711645"
TS = "1788815260"
TARGET = "69gizY6/qeayaMr5BqfhtV4Zbolo3gysfy4amSmNo6M="
TARGET_BYTES = base64.b64decode(TARGET)   # compare raw digest, case-proof


def key_forms(s: str):
    """Every plausible way a stored string becomes an HMAC key."""
    yield ("raw", s.encode())
    yield ("raw-upper", s.upper().encode())
    yield ("raw-lower", s.lower().encode())
    # hex string -> raw bytes (very common: secret stored as hex text, used as bytes)
    try:
        if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
            yield ("hex-decoded", bytes.fromhex(s))
    except ValueError:
        pass
    # base64 string -> raw bytes
    try:
        if len(s) >= 16 and len(s) % 4 == 0:
            yield ("b64-decoded", base64.b64decode(s))
    except Exception:
        pass


def messages(s: str):
    """Every plausible message the HMAC is computed over."""
    yield APP_KEY + TS
    yield TS + APP_KEY
    yield APP_KEY + TS + s          # some schemes fold the secret/id in
    yield f"{APP_KEY}{TS}"


def digests(key: bytes, msg: bytes):
    """Both common HMAC hashes."""
    yield hmac.new(key, msg, hashlib.sha256).digest()
    yield hmac.new(key, msg, hashlib.sha1).digest()


def check(candidate: str) -> str | None:
    for kname, key in key_forms(candidate):
        if not key:
            continue
        for msg in messages(candidate):
            for dig in digests(key, msg.encode()):
                if dig == TARGET_BYTES:
                    return kname
    return None


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="candidate file, one per line (default: stdin)")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the harness with the known litter-box secret shape")
    args = ap.parse_args()

    if args.self_test:
        # sanity: fabricate a target from a known secret and confirm we recover it
        secret = "deadbeefcafebabe0011223344556677"
        tgt = hmac.new(secret.encode(), (APP_KEY + TS).encode(), hashlib.sha256).digest()
        globals()["TARGET_BYTES"] = tgt
        print("self-test:", "PASS" if check(secret) else "FAIL")
        return 0

    src = open(args.file) if args.file else sys.stdin
    n = 0
    seen = set()
    for line in src:
        c = line.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        n += 1
        how = check(c)
        if how:
            print(f"\n*** MATCH ***")
            print(f"secret    : {c}")
            print(f"key form  : {how}")
            print(f"appkey    : {APP_KEY}")
            return 0
    print(f"tested {n} unique candidates across "
          f"{len(list(key_forms('abcd')))}x{len(list(messages('x')))}x2 variants each — no match")
    return 1


if __name__ == "__main__":
    sys.exit(main())
