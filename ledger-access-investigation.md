# How the Riko intake ledger was cracked — investigation record

A durable account of how we got self-sufficient access to the feeder intake ledger
(`usapi.neakasapet.com/api/feeder/record`), so this doesn't have to be rediscovered.
Written after the fact, with the dead ends included on purpose — most of the elapsed
time went into wrong turns that are worth flagging.

## The answer, up front

The ledger request needs, all reproducible from public values:

- **host**: `https://usapi.neakasapet.com`
- **paths**: `/api/feeder/record`, `/api/feeder/record/statistics`
- **appid**: `32715650`  (the litter-box/SDK app key — NOT the iOS app key `32711645`)
- **secret**: `698ee0ef531c3df2ddded87563643860`  (from the SDK; confirmed live)
- **sign** (in BOTH `sign` and `request-id` headers):
  `base64(HMAC_SHA256(secret, appid + timestamp)).UPPERCASE()`
- **uid** header: `aes_encrypt_with_boot_key(str(ali_user_id))`  (SDK function, unchanged)
- **token** header: `aes_encrypt("{userToken}@{seconds}.{millis}", session_aes_key, session_aes_iv)`
  — note the timestamp format `seconds.millis`, NOT the SDK's `generate_session_token`
  which uses `@milliseconds`. Same AES key (the session key from login), different plaintext.
- **params**: `data_type=0, device_name=<serial>, start_time, end_time (unix seconds),
  user_id=<ali_user_id>, bind_status=1`

**And the one that actually mattered: log in as the account that OWNS THE FEEDER IN THE
PHONE APP.** Here that was `stidwillyoho@gmail.com` (user 400133257), NOT the automation
account `alandyoho@gmail.com` (user 400133342). Same email family, genuinely separate
Neakasa user records. Everything else was reproducible for days; this was the blocker.

Working implementation: `neakasa.py`.

## What was true vs. what we wrongly believed

| Belief that cost time | Reality |
|---|---|
| "There's a separate feeder app secret we don't have" | No. Same secret (`698ee0ef…`) signs everything. |
| "The iOS app key `32711645` is what the feeder uses" | That's iOS-only. Android/SDK uses `32715650`. Testing against `32711645` made every oracle fail. |
| "The secret is hidden in the app binary; we must extract it" | It was in the SDK the whole time. Static search of 400k+ strings found nothing because there was nothing to find. |
| "`DFA84B10B7ACDD25` is the token AES key" (most frequent Frida key) | Red herring — some other cipher. Token uses the per-session `aes_key`/`aes_iv` from login. |
| "The feeder backend is unreachable / different identity we can't be" | Reachable. We just had to log in as the account that owns the feeder. |
| "The SDK's session token works everywhere" | Its `@milliseconds` format is rejected by the feeder backend, which wants `@seconds.millis`. |

## Sequence of what actually moved us forward

1. **Read the SDK source first.** `neakasa_litterbox_sdk` already contained the app key,
   secret, boot key/iv, and the AES/HMAC/md5 helpers. Confirming `aes_encrypt_with_boot_key("400133257")`
   reproduced a captured `uid` byte-for-byte proved the crypto was right. (We should have
   done this on day one instead of the APK teardown.)
2. **Offline oracle testing** against a captured `sign`: `HMAC(secret, appid+ts)` compared
   to a real captured signature. Case-insensitive (compare raw digest bytes) — the real
   sign is UPPERCASE and a case-sensitive compare hid the match.
3. **Frida on the Android app in an emulator** to observe crypto at runtime. This is what
   resolved the token format and, crucially, revealed the app authenticates as the feeder
   owner's user id — the clue that led to the account realization.
4. **The account realization**: the app encrypted `400133257` as its uid even though the
   automation login yields `400133342`. Different accounts. Logging in as `stidwillyoho`
   returned `code:0` immediately.

## Queries / techniques that WORKED (repeat these)

- **Confirm crypto against a known plaintext/ciphertext oracle, comparing raw bytes not base64:**
  ```python
  import hmac,hashlib,base64
  base64.b64encode(hmac.new(SECRET.encode(),(APPID+ts).encode(),hashlib.sha256).digest()).decode().upper() == captured_sign
  ```
- **Read the SDK for constants and algorithms before reversing anything:**
  `grep -rn "APP_KEY\|APP_SECRET\|BOOT_KEY\|def aes_encrypt\|generate_session_token" <sdk path>`
- **Frida: correlate the crypto KEY with the DATA on the same thread.** Hook the init
  (key) and update (plaintext) of the cipher, keyed by `this.threadId`, so you print the
  exact key used for a specific plaintext — not just "all keys seen."
- **Frida: filter HMAC messages by the appkey prefix** to find which backend a sign is for
  (`msg.startsWith("32715650")` vs `"32711645"`).
- **Spawn with `-f` not attach** (`frida -U -f com.jhkj.neakasa -l hook.js`) to avoid the
  attach-lock hang, and to catch startup/login crypto.
- **Probe endpoints and read the error CODE, not just success/failure:** 404 = wrong path,
  1009 = missing/wrong params, 1001 = params ok but content/identity wrong, 1006 = bad sign,
  1007 = bad token. The changing code told us which layer we were fixing.
- **Write Frida output to a file** (`... | tee log.txt`) and grep it, rather than reading
  a scrolling console.

## Queries / techniques to AVOID (these wasted time)

- **Do NOT scan process memory for the secret** (`Process.enumerateRanges` + regex). It
  wedged Frida's JS thread hard (unkillable with Ctrl-C) on large regions, twice. If you
  must, cap region size, chunk reads, do rw- before r--, and print progress — but hooking
  the crypto call is strictly better and can't hang.
- **Do NOT trust a static string search's absence as proof** without testing every
  interpretation. We "ruled out" `698ee0ef…` early by testing it as an AES key (wrong) when
  it was an HMAC key. Test each candidate as: raw, hex-decoded, base64-decoded, and against
  every message ordering and both SHA1/SHA256.
- **Do NOT assume the most-frequent captured key is the one you want.** `DFA84B10B7ACDD25`
  dominated the capture and was irrelevant. Correlate to the specific plaintext instead.
- **Do NOT carry over values from the iOS capture.** iOS and Android use different app keys
  (`32711645` vs `32715650`) and bundle IDs (`com.jihai.Neakasa` vs `com.jhkj.neakasa`).
- **Do NOT compare base64 signatures case-sensitively** — the real sign is uppercased.
- **Do NOT assume one email = one account.** The same email had two user records on two
  backends. Check `ali_user_id` after login and confirm it matches the feeder owner.

## Environment notes for repeating the Frida step

- Emulator: `avdmanager` needs a JDK (`brew install --cask temurin`). Use an **arm64**
  Google-APIs image (rootable; runs native on Apple Silicon). Boot with `-writable-system`.
- `pip install frida-tools` fails under PEP 668; use `pipx install frida-tools`.
- frida-server version must EXACTLY match `frida --version`; push to `/data/local/tmp`,
  run as root (`adb root` first).
- The app is Flutter (pointycastle bundled) but still calls system `libssl.so`/`libcrypto.so`
  for HMAC/AES — so hooking those exported functions works. Its own HTTP/TLS is internal to
  `libflutter.so` (no `SSL_write` catch), which is why we read crypto, not wire traffic.

## Security / hygiene reminders

- Captured Frida logs (`logs*.txt`) contain live AES keys, session tokens, and the
  userToken. Gitignore and delete them; never commit.
- The app secret/keys here are the SDK's public ones, safe to keep in code.
- Rotate any account password that was echoed to a terminal during testing.
