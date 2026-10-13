// riko_feeder_secret.js — capture the HMAC secret used for the FEEDER backend.
//
// The feeder backend (usapi.neakasapet.com) signs with app key 32711645 and a
// secret we don't have. The Android app logs into that backend at startup/login,
// computing sign = HMAC-SHA256(feeder_secret, "32711645" + timestamp). This hook
// watches every HMAC and prints ONLY the ones whose MESSAGE contains "32711645"
// — i.e. feeder-backend signs — showing the key (the secret) used for each.
//
// It hooks HMAC_Init_ex (key) and HMAC_Update (message) and correlates them per
// thread, so each printed block is a matched key+message pair.
//
// Run:  frida -U -f com.jhkj.neakasa -l riko_feeder_secret.js
// Then LOG IN fresh in the app (clear it first: adb shell pm clear com.jhkj.neakasa).
// The feeder login fires once — watch for [FEEDER SIGN].

const FEEDER_APPKEY = "32711645";
const LITTERBOX_APPKEY = "32715650";  // shown for contrast, not the target

function bytesToStr(ptr, len) {
  try {
    const b = new Uint8Array(ptr.readByteArray(len));
    let s = "";
    for (let i = 0; i < b.length; i++)
      s += (b[i] >= 32 && b[i] < 127) ? String.fromCharCode(b[i]) : ".";
    return s;
  } catch (e) { return "?"; }
}
function bytesToHex(ptr, len) {
  try {
    return Array.from(new Uint8Array(ptr.readByteArray(len)))
      .map(b => b.toString(16).padStart(2, "0")).join("");
  } catch (e) { return "?"; }
}

function resolve(name) {
  const out = [];
  Process.enumerateModules().forEach(function (m) {
    if (!/libssl|libcrypto/.test(m.name)) return;
    try { const e = m.getExportByName(name); if (e) out.push([m.name, e]); } catch (_) {}
  });
  return out;
}

// per-thread: remember the last key seen at Init, pair it with the message at Update
const lastKey = {};   // tid -> {ascii, hex, len}

resolve("HMAC_Init_ex").forEach(function (pair) {
  Interceptor.attach(pair[1], {
    onEnter(args) {
      const keyPtr = args[1], keyLen = args[2].toInt32();
      if (keyPtr.isNull() || keyLen <= 0 || keyLen > 128) return;
      lastKey[this.threadId] = {
        ascii: bytesToStr(keyPtr, keyLen),
        hex: bytesToHex(keyPtr, keyLen),
        len: keyLen,
      };
    }
  });
});

resolve("HMAC_Update").forEach(function (pair) {
  Interceptor.attach(pair[1], {
    onEnter(args) {
      const dataPtr = args[1], dataLen = args[2].toInt32();
      if (dataPtr.isNull() || dataLen <= 0 || dataLen > 512) return;
      const msg = bytesToStr(dataPtr, dataLen);
      const k = lastKey[this.threadId];
      if (!k) return;
      if (msg.indexOf(FEEDER_APPKEY) === 0 || msg.indexOf(FEEDER_APPKEY) >= 0) {
        console.log("\n================ [FEEDER SIGN] ================");
        console.log("message : " + msg);
        console.log("KEY ascii: " + k.ascii);
        console.log("KEY hex  : " + k.hex + "  (len " + k.len + ")");
        console.log("==============================================");
        console.log(">>> the KEY above is the feeder secret. Send it to Claude. <<<");
      }
    }
  });
});

console.log("feeder-secret hook armed.");
console.log("Now LOG IN fresh in the app. Watching for HMAC messages containing " + FEEDER_APPKEY + " …");
console.log("(litterbox app key " + LITTERBOX_APPKEY + " is ignored — we only want the feeder one.)");
