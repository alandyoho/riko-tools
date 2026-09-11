// riko_hmac_hook.js — catch the HMAC key at the moment the app signs a request.
//
// No memory scanning. We hook the one-shot HMAC() in libcrypto.1.1.so and print
// its key argument every time it fires, then confirm which call reproduces the
// captured feeder sign. Also hooks HMAC_Init_ex (the streaming API) in case the
// app uses that form.
//
// Run:  frida -U Neakasa -l riko_hmac_hook.js
// Then in the app: open feeding-history / pull to refresh. Watch the output.
//
// Oracle: sign for appKey+ts "32711645"+"1788815260" must be
//   69gizY6/qeayaMr5BqfhtV4Zbolo3gysfy4amSmNo6M=

function hx(ptr, len) {
  try { return Array.from(new Uint8Array(ptr.readByteArray(len)))
    .map(b => b.toString(16).padStart(2, "0")).join(""); } catch (e) { return "?"; }
}
function asc(ptr, len) {
  try {
    const b = new Uint8Array(ptr.readByteArray(len));
    let s = ""; for (let i = 0; i < b.length; i++)
      s += (b[i] >= 32 && b[i] < 127) ? String.fromCharCode(b[i]) : ".";
    return s;
  } catch (e) { return "?"; }
}

function resolve(name) {
  const lib = "libcrypto.1.1.so";
  try { const m = Process.getModuleByName(lib);
        try { return m.getExportByName(name); } catch (e) {}
        try { return m.findExportByName(name); } catch (e) {} } catch (e) {}
  try { return Module.getGlobalExportByName(name); } catch (e) {}
  try { return Module.findExportByname(lib, name); } catch (e) {}
  return null;
}

let hooked = 0;

// one-shot: HMAC(evp, key, key_len, data, data_len, out, out_len)
const HMAC = resolve("HMAC");
if (HMAC) {
  Interceptor.attach(HMAC, {
    onEnter(args) {
      const keyLen = args[2].toInt32();
      if (keyLen > 0 && keyLen <= 128) {
        console.log("\n[HMAC] key_len=" + keyLen +
          "\n  ascii: " + asc(args[1], keyLen) +
          "\n  hex  : " + hx(args[1], keyLen));
      }
    }
  });
  hooked++;
  console.log("hooked libcrypto HMAC()");
}

// streaming: HMAC_Init_ex(ctx, key, key_len, md, engine)
const INIT = resolve("HMAC_Init_ex");
if (INIT) {
  Interceptor.attach(INIT, {
    onEnter(args) {
      const keyPtr = args[1], keyLen = args[2].toInt32();
      if (!keyPtr.isNull() && keyLen > 0 && keyLen <= 128) {
        console.log("\n[HMAC_Init_ex] key_len=" + keyLen +
          "\n  ascii: " + asc(keyPtr, keyLen) +
          "\n  hex  : " + hx(keyPtr, keyLen));
      }
    }
  });
  hooked++;
  console.log("hooked libcrypto HMAC_Init_ex()");
}

if (hooked === 0) {
  console.log("!! no libcrypto HMAC exports found — the app's HMAC is pure-Dart");
  console.log("   (pointycastle). Tell Claude; we hook the Dart path instead.");
} else {
  console.log("\nready. In the app, open feeding-history or pull to refresh.");
  console.log("Every HMAC key used will print. The feeder secret is the ~32-char");
  console.log("value that appears right before a request goes out.");
}
