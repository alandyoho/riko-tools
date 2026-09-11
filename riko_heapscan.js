// riko_heapscan.js — find the Neakasa feeder HMAC secret in the running app's memory.
//
// Strategy: the secret must exist as bytes in memory for pointycastle to HMAC with.
// We scan readable memory for ASCII strings of plausible secret length, and for each
// candidate compute HMAC-SHA256(candidate, appKey+timestamp) and compare to a captured
// sign. A match is proof, not a guess. Runs entirely in-process; hooks nothing.
//
// Oracle (captured feeder request):
//   sign      = 69gizY6/qeayaMr5BqfhtV4Zbolo3gysfy4amSmNo6M=
//   appKey    = 32711645
//   timestamp = 1788815260
//
// Run:  frida -U -f com.jhkj.neakasa -l riko_heapscan.js
//   or attach to the running app:  frida -U Neakasa -l riko_heapscan.js
// then in the app open the feeding-history screen (so the secret is live in memory),
// and in the frida console type:  scan()

const APPKEY = "32711645";
const TS = "1788815260";
const TARGET_B64 = "69gizY6/qeayaMr5BqfhtV4Zbolo3gysfy4amSmNo6M=";

// ---- crypto via NDK libcrypto (present in the app: libcrypto.1.1.so) ----------
// We use the device's own OpenSSL HMAC so we don't reimplement it in JS.
let HMAC = null;
function initCrypto() {
  const libname = "libcrypto.1.1.so";
  // Frida 17 changed the module API. Support both old and new.
  function findExp(name) {
    // new API: Module.getGlobalExportByName, or per-module getExportByName
    try { const r = Module.getGlobalExportByName(name); if (r) return r; } catch (e) {}
    try {
      const m = Process.getModuleByName(libname);
      try { const r = m.getExportByName(name); if (r) return r; } catch (e) {}
      try { const r = m.findExportByName(name); if (r) return r; } catch (e) {}
    } catch (e) {}
    // old API
    try { const r = Module.findExportByName(libname, name); if (r) return r; } catch (e) {}
    return null;
  }
  const hmac = findExp("HMAC");
  const evp = findExp("EVP_sha256");
  if (!hmac || !evp) { console.log("HMAC exports not found in " + libname); return false; }
  const HMACfn = new NativeFunction(hmac, "pointer",
    ["pointer", "pointer", "int", "pointer", "int", "pointer", "pointer"]);
  const sha256 = new NativeFunction(evp, "pointer", [])();
  HMAC = function (keyBytes, msgBytes) {
    const key = Memory.alloc(keyBytes.length); key.writeByteArray(keyBytes);
    const msg = Memory.alloc(msgBytes.length); msg.writeByteArray(msgBytes);
    const out = Memory.alloc(32); const outlen = Memory.alloc(4);
    HMACfn(sha256, key, keyBytes.length, msg, msgBytes.length, out, outlen);
    return new Uint8Array(out.readByteArray(32));
  };
  return true;
}

function b64(bytes) {
  const t = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let s = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = bytes[i + 1], c = bytes[i + 2];
    s += t[a >> 2] + t[((a & 3) << 4) | (b >> 4)];
    s += (b === undefined) ? "=" : t[((b & 15) << 2) | (c >> 4)];
    s += (c === undefined) ? "=" : t[c & 63];
  }
  return s;
}

const msgBytes = () => {
  const s = APPKEY + TS; const a = [];
  for (let i = 0; i < s.length; i++) a.push(s.charCodeAt(i));
  return a;
};
function strBytes(s) { const a = []; for (let i = 0; i < s.length; i++) a.push(s.charCodeAt(i) & 0xff); return a; }
function hexBytes(s) { const a = []; for (let i = 0; i < s.length; i += 2) a.push(parseInt(s.substr(i, 2), 16)); return a; }

function test(cand) {
  // try raw, and hex-decoded, as key
  const M = msgBytes();
  const forms = [strBytes(cand)];
  if (cand.length % 2 === 0 && /^[0-9a-fA-F]+$/.test(cand)) forms.push(hexBytes(cand));
  for (const key of forms) {
    if (b64(HMAC(key, M)) === TARGET_B64) return true;
  }
  return false;
}

// ---- heap scan ----------------------------------------------------------------
function scan(opts) {
  opts = opts || {};
  const MAXREGION = (opts.maxMB || 64) * 1024 * 1024;  // skip mappings bigger than this
  const CHUNK = 1024 * 1024;                            // process 1 MB at a time
  if (!HMAC && !initCrypto()) { console.log("could not bind libcrypto HMAC"); return; }

  // rw- first (Dart heap / runtime data — where an assembled secret lives),
  // then r-- (constants), skipping oversized file mappings.
  const rw = Process.enumerateRanges("rw-");
  const ro = Process.enumerateRanges("r--");
  const ranges = rw.concat(ro).filter(r => r.size <= MAXREGION);
  console.log("scanning " + ranges.length + " regions (rw first), <= " +
              (MAXREGION/1048576) + "MB each…");

  const re = /[A-Za-z0-9+/=_\-]{16,64}/g;
  const seen = new Set();
  let tested = 0, hit = null, ri = 0;

  for (const r of ranges) {
    ri++;
    if (ri % 25 === 0) console.log("  region " + ri + "/" + ranges.length +
                                   "  tested " + tested + " candidates…");
    for (let off = 0; off < r.size && !hit; off += CHUNK) {
      const n = Math.min(CHUNK, r.size - off);
      let buf;
      try { buf = r.base.add(off).readByteArray(n); } catch (e) { break; }
      if (!buf) break;
      const bytes = new Uint8Array(buf);
      let s = "";
      for (let i = 0; i < bytes.length; i++) {
        const c = bytes[i];
        s += (c >= 32 && c < 127) ? String.fromCharCode(c) : "\n";
      }
      let m;
      while ((m = re.exec(s)) !== null) {
        const cand = m[0];
        if (seen.has(cand)) continue;
        seen.add(cand); tested++;
        if (test(cand)) { hit = cand; break; }
      }
    }
    if (hit) break;
  }
  if (hit) {
    console.log("\n*** SECRET FOUND ***");
    console.log("feeder app_secret = " + hit);
  } else {
    console.log("no match among " + tested + " unique candidates");
    console.log("try: refresh the feeding screen, then scan() again");
  }
}

rpc.exports = { scan: scan };
console.log("loaded. open the app's feeding-history screen, then type:  scan()");
