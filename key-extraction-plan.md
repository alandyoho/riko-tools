# Plan: extract the Neakasa app's crypto key

## Goal

Recover the AES key (and the login `sign` algorithm/key) that the Neakasa app uses
to build the `token` / `uid` / `sign` headers for `usapi.neakasapet.com`. With them,
`neakasa.py` could log in and read the app backend on its own, instead of depending
on a `token` triple captured by hand from the phone (which expires).

Payoff: self-sufficient access to the app API — intake ledger, cat profile, food
database, config — and, most usefully, reproducible login so nothing needs manual
re-capture. Not required for device control, which already works through Aliyun.

## What we already know (don't re-do this)

- The app is **Flutter**; logic is in `libapp.so` (compiled Dart), not the Java.
- The crypto is Neakasa's own: `flutter_module/common/utils/aes/AESNoPadding.dart`,
  `CryptoPKCS7.dart`, `MD5.dart`, `RequestInterceptor.dart`.
- `uid` = AES(no-pad) of the numeric user id; 16-byte ciphertext. This is our
  **oracle**: a correct key decrypts `TMe8dkYoZfLbrkvPN+xTLw==` to `400133257`.
- **Static extraction failed.** Every key-shaped string constant in `libapp.so` was
  tested against the oracle (ECB/CBC, 16/24/32-byte, hex/ascii, zero/key IV). No hit.
  The key is runtime-built, raw bytes, or derived — not a stored printable string.
- The `sign` header is enforced on `/api/login/user` but NOT re-checked against query
  params on `/api/feeder/record`.

## The approach: observe the AES call at runtime

Since the key isn't findable statically, run the app and read the key at the moment
it's used. Two routes; try them in this order.

### Route A — Frida hook (preferred, most surgical)

Attach Frida to the running app and intercept the AES operation, printing the key,
IV, and plaintext as they pass through.

1. **Environment.** Android Studio emulator, a **Google APIs** image (rootable via
   `adb root`), OR a rooted physical Android phone. adb is already installed.
2. **Install the app** (`adb install`, from the APKPure APK we already have).
3. **Frida server** matching the emulator arch (arm64) pushed to the device and run
   as root; `frida-tools` on the Mac.
4. **Find the hook point.** The Dart AES likely bottoms out in one of:
   - `libcrypto.1.1.so` (OpenSSL `AES_set_encrypt_key` / `EVP_*`) — easiest, stable
     symbol names. Hook `AES_set_encrypt_key`; its first arg is the raw key bytes.
   - pointycastle pure-Dart AES inside `libapp.so` — harder, no symbols; would need
     the reFlutter route instead.
   Start by hooking OpenSSL key-schedule functions in `libcrypto`; if the app uses
   the system crypto, the key appears there directly.
5. **Trigger it.** Open the app / pull to refresh so it signs a request, and read the
   key Frida prints.
6. **Verify against the oracle** with the existing test harness (decrypt `uid` →
   expect `400133257`). Confirming, not guessing.

### Route B — reFlutter snapshot dump (fallback)

If the crypto is pure-Dart in `libapp.so` with no OpenSSL call to hook:

1. `reFlutter` repackages the APK against an instrumented Flutter engine that dumps
   the Dart snapshot (classes, string pools, sometimes constants) and opens a
   Frida-friendly socket.
2. Install the repackaged APK in the emulator, run it, dump.
3. Search the dump for the key / the RequestInterceptor logic.
More setup than A, but works when there's no native crypto symbol to catch.

### Route C — trace the sign, separately

`sign` is its own problem (HMAC or similar, not the AES). Same Frida session can hook
the hashing call in `RequestInterceptor`'s path. Needed only for self-sufficient
login; the ledger read just needs the AES key + a still-valid token.

## Definition of done

- The `uid` oracle decrypts correctly with the recovered key → AES key confirmed.
- `neakasa.py` can build a valid `token`/`uid` for an arbitrary request.
- Stretch: reproduce `sign` well enough that `POST /api/login/user` succeeds from
  the script, removing all dependence on captured tokens.

## Cost / honest assessment

Several hours of specialized work (Frida setup + finding the offset). The only thing
it unlocks beyond the current Aliyun-based tooling is the app backend — intake
history, cat/food metadata, and reproducible login. Nothing Mixtape needs. Worth it
if the reverse-engineering is enjoyable in itself or if self-sufficient intake
tracking becomes a real requirement; otherwise the captured-token reader in
`neakasa.py` is enough for occasional analysis.

## If we stop here

`neakasa.py` already reads the ledger with a captured token triple. That's the
practical capability; the plan above only removes the manual capture step.
