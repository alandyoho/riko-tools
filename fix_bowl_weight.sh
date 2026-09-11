#!/usr/bin/env bash
#
# fix_bowl_weight.sh — correct the Neakasa Riko's stored empty-bowl weight
# using only bash + curl + openssl + jq. No Python, no SDK.
#
# WHY THIS EXISTS
#   The Riko ships with its stored empty-bowl weight set to 65 g. The bowls in
#   the box weigh ~68 g. So every "food left in bowl" reading carries ~3 g of
#   phantom food, and the app's tare button does NOT fix it (it zeroes the
#   platform but never updates the stored constant). This sends the correct
#   value directly via the same cloud API the app uses.
#
# USAGE
#   ./fix_bowl_weight.sh -e you@example.com -w 68             # prompts for password
#   ./fix_bowl_weight.sh -e you@example.com -p 'pass' -w 68
#   ./fix_bowl_weight.sh -e ... -w 68 -d WL0300...           # pick a device (if you have >1)
#   ./fix_bowl_weight.sh -e ... -w 68 -v                      # verbose
#   ./fix_bowl_weight.sh -e ... -w 68 -n                      # dry run: auth only
#
# BEFORE RUNNING
#   * Weigh your empty, dry bowl on a kitchen scale; pass that number to -w.
#   * TAKE THE BOWL OFF THE TRAY (the command re-zeros the platform). Put it back after.
#
# REQUIRES: bash, curl, openssl, jq, xxd. All standard on macOS; on minimal
#   Linux you may need: apt install jq xxd  (xxd is in the vim-common package). jq via
#   `brew install jq` / `apt install jq`).
#
# STATUS: tested working against a live account 2026-09-10. Four chained auth
#   requests across three signing schemes. If it ever breaks after a Neakasa
#   backend change, run with -v to see which step fails; the gateway echoes the
#   expected signature string in an X-Ca-Error-Message header on a sig mismatch.
#
# Constants below were published in the open-source neakasa-litterbox-sdk.
# Unofficial; not affiliated with Neakasa.

set -euo pipefail

# ---- published app constants -------------------------------------------------
APP_KEY="32715650"
APP_SECRET="698ee0ef531c3df2ddded87563643860"
BOOT_KEY="3J74PRUE5TKPJP32"      # AES-128-CBC key for the login token
BOOT_IV="QB8GC2X6WK39FF93"       # AES-128-CBC IV
PRODUCT_ID="a123nCqsrQm3vEbt"
AREA_CODE="1"
UA="neakasa-litterbox-sdk/0.2.2"
REST_LOGIN="https://us.neakasa.com/api/login"
BOOTSTRAP="cn-shanghai.api-iot.aliyuncs.com"

EMAIL="" PASSWORD="" WEIGHT="" DEVICE="" DRYRUN=0 VERBOSE=0
while getopts "e:p:w:d:nv" o; do case $o in
  e) EMAIL=$OPTARG;; p) PASSWORD=$OPTARG;; w) WEIGHT=$OPTARG;;
  d) DEVICE=$OPTARG;; n) DRYRUN=1;; v) VERBOSE=1;;
  *) echo "usage: $0 -e email [-p pass] -w grams [-d device_name] [-n] [-v]"; exit 2;;
esac; done
[[ -z $EMAIL || -z $WEIGHT ]] && { echo "need -e email and -w grams" >&2; exit 2; }

# ---- dependency preflight ----------------------------------------------------
# Checks the tools we need and, if any are missing, offers to install them with
# your system's package manager. It asks first and shows the command — it never
# installs anything silently or runs sudo without you saying yes.
check_deps() {
  local missing=() t
  for t in curl openssl jq xxd; do command -v "$t" >/dev/null || missing+=("$t"); done
  [[ ${#missing[@]} -eq 0 ]] && return 0

  echo "Missing required tool(s): ${missing[*]}" >&2
  local os cmd=""
  os=$(uname -s)
  if [[ $os == Darwin ]]; then
    if command -v brew >/dev/null; then
      cmd="brew install ${missing[*]}"
    else
      echo "You need Homebrew to install these on macOS. Install it from https://brew.sh" >&2
      echo "then re-run this script." >&2
      return 1
    fi
  elif command -v apt-get >/dev/null; then
    # xxd ships in vim-common on Debian/Ubuntu
    local pkgs=("${missing[@]/xxd/xxd}")
    cmd="sudo apt-get update && sudo apt-get install -y ${missing[*]}"
  elif command -v dnf >/dev/null; then
    cmd="sudo dnf install -y ${missing[*]/xxd/vim-common}"
  elif command -v pacman >/dev/null; then
    cmd="sudo pacman -S --noconfirm ${missing[*]/xxd/vim}"
  else
    echo "Couldn't detect your package manager. Please install: ${missing[*]}" >&2
    return 1
  fi

  echo
  echo "I can install them by running:"
  echo "    $cmd"
  read -rp "Run this now? [y/N] " ans
  [[ $ans == [yY]* ]] || { echo "Okay — install them yourself and re-run." >&2; return 1; }
  eval "$cmd"
  # re-check
  for t in "${missing[@]}"; do command -v "$t" >/dev/null || {
    echo "Still missing $t after install — sorry, please install it manually." >&2; return 1; }
  done
  echo "All set."
}
check_deps || exit 2

[[ -z $PASSWORD ]] && { read -rsp "Neakasa password: " PASSWORD; echo; }

vlog(){ [[ $VERBOSE == 1 ]] && echo "  $*" >&2 || true; }

# ---- dummy-proofing: make the user take the bowl off before we re-zero -------
# resetScaleZero re-zeros the platform. If the bowl is on the tray when it runs,
# the zero point captures the bowl and every later reading is wrong. So we make
# removing it an explicit step, and confirming it's back an explicit step.
if [[ $DRYRUN == 0 ]]; then
  echo
  echo "IMPORTANT: take the food BOWL OUT of the tray now."
  echo "(This resets the scale's zero point. If the bowl is on the tray, the zero"
  echo " will be wrong and readings will be off. You'll put it back at the end.)"
  echo
  read -rp "Bowl removed? Press Enter to continue (or Ctrl-C to abort): " _
fi

# ---- primitives --------------------------------------------------------------
b64(){ openssl base64 -A; }
hs256(){ openssl dgst -sha256 -hmac "$1" -binary | b64; }
hs1(){ openssl dgst -sha1 -hmac "$1" -binary | b64; }
md5h(){ openssl dgst -md5 -hex | sed 's/^.*= *//'; }
md5b(){ openssl dgst -md5 -binary | b64; }
urlenc(){ jq -sRr @uri; }
uuid(){ if command -v uuidgen >/dev/null; then uuidgen; else
  printf '%s-%s-4%s-%s-%s\n' "$(openssl rand -hex 4)" "$(openssl rand -hex 2)" \
    "$(openssl rand -hex 2|cut -c2-)" "$(openssl rand -hex 2)" "$(openssl rand -hex 6)"; fi; }
rfc1123(){ LC_ALL=C date -u '+%a, %d %b %Y %H:%M:%S GMT'; }

# ============================================================================
# STEP 1 — Neakasa REST login (HMAC-SHA256 sign, double-md5 password)
# ============================================================================
echo "[1/5] Neakasa login…" >&2
TS=$(date +%s)
SIGN=$(printf '%s' "${APP_KEY}${TS}" | hs256 "$APP_SECRET" | tr a-z A-Z)
PW=$(printf '%s' "$PASSWORD" | md5h); PW=$(printf '%s' "$PW" | md5h)
LOGIN_JSON=$(jq -cn --arg a "$AREA_CODE" --arg p "$PRODUCT_ID" --arg e "$EMAIL" \
  --arg pw "$PW" --arg ua "$UA" \
  '{areaCode:$a,productId:$p,userName:$e,phone:"",email:$e,password:$pw,
    userAppVersion:"2.2.6",deviceNumber:"bash",deviceToken:"bash",deviceType:"2",devSysVer:$ua}')
DATAENC=$(printf '%s' "$LOGIN_JSON" | urlenc)
vlog "sign=$SIGN"
R1=$(curl -s "${REST_LOGIN}?data=${DATAENC}" \
  -H "appId: $APP_KEY" -H "sign: $SIGN" -H "timestamp: $TS" -H "request-id: $SIGN" \
  -H "version: 226" -H "versionString: 2.2.6" -H "brand: Generic" -H "model: bash" \
  -H "Accept-Language: en" -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Charset: UTF-8" -H "Accept: */*" -H "User-Agent: $UA")
[[ $(jq -r '.code' <<<"$R1") == 0 ]] || { echo "login failed: $R1" >&2; exit 1; }
LOGIN_TOKEN=$(jq -r '.data.loginToken' <<<"$R1")
AUTHCODE=$(jq -r '.data.userInfo.aliAuthenticationToken' <<<"$R1")
vlog "authCode=${AUTHCODE:0:10}…"

# ---- decrypt the login token to get the session AES key/iv -------------------
# loginToken = base64(AES-128-CBC-NoPadding("userToken@userId@aesKey@aesIv", BOOT_KEY, BOOT_IV))
KEYHEX=$(printf '%s' "$BOOT_KEY" | xxd -p | tr -d '\n')
IVHEX=$(printf '%s'  "$BOOT_IV"  | xxd -p | tr -d '\n')
PLAIN=$(printf '%s' "$LOGIN_TOKEN" | openssl base64 -d -A \
        | openssl enc -aes-128-cbc -d -K "$KEYHEX" -iv "$IVHEX" -nopad 2>/dev/null \
        | tr -d '\000')
USER_TOKEN=$(cut -d@ -f1 <<<"$PLAIN")
[[ -z $USER_TOKEN ]] && { echo "login-token decrypt failed (plain='$PLAIN')" >&2; exit 1; }
vlog "login token decrypted ok"

# ============================================================================
# helpers: signed IoT gateway POST (HMAC-SHA1) and OA POST (HMAC-SHA256)
# ============================================================================
iot_call(){  # host path payload-json apiVer [iotToken]
  local host=$1 path=$2 payload=$3 apiver=$4 tok=${5:-}
  local id nonce ts date body md5 sts sig url
  id=$(uuid); nonce=$(uuid); ts=$(( $(date +%s) * 1000 )); date=$(rfc1123)
  if [[ -n $tok ]]; then
    body=$(jq -cn --arg id "$id" --arg av "$apiver" --arg tok "$tok" --argjson d "$payload" \
      '{a:$id,b:"1.0",c:{apiVer:$av,language:"en-US",iotToken:$tok},d:$d,id:$id,params:{"$ref":"$.d"},request:{"$ref":"$.c"},version:"1.0"}')
  else
    body=$(jq -cn --arg id "$id" --arg av "$apiver" --argjson d "$payload" \
      '{a:$id,b:"1.0",c:{apiVer:$av,language:"en-US"},d:$d,id:$id,params:{"$ref":"$.d"},request:{"$ref":"$.c"},version:"1.0"}')
  fi
  md5=$(printf '%s' "$body" | md5b)
  url="/${path#/}?x-ca-request-id=$id"
  sts=$(printf '%s\n%s\n%s\n%s\n%s\nx-ca-key:%s\nx-ca-nonce:%s\nx-ca-signature-method:HmacSHA1\nx-ca-timestamp:%s\n%s' \
    "POST" "application/json; charset=utf-8" "$md5" "application/octet-stream; charset=utf-8" \
    "$date" "$APP_KEY" "$nonce" "$ts" "$url")
  sig=$(printf '%s' "$sts" | hs1 "$APP_SECRET")
  vlog "IoT POST https://$host$url"
  curl -s "https://${host}${url}" --data-binary "$body" \
    -H "x-ca-key: $APP_KEY" -H "x-ca-signature-method: HmacSHA1" \
    -H "x-ca-timestamp: $ts" -H "x-ca-nonce: $nonce" -H "x-ca-signature: $sig" \
    -H "x-ca-signature-headers: x-ca-nonce,x-ca-timestamp,x-ca-key,x-ca-signature-method" \
    -H "content-md5: $md5" -H "content-type: application/octet-stream; charset=utf-8" \
    -H "accept: application/json; charset=utf-8" -H "ca_version: 1" \
    -H "date: $date" -H "user-agent: $UA"
}

oa_call(){  # host path body-json [vid]
  local host=$1 path=$2 bodyjson=$3 vid=${4:-}
  local key val sbody wbody nonce ts date sts sig
  key=$(jq -r 'keys[0]' <<<"$bodyjson"); val=$(jq -c ".$key" <<<"$bodyjson")
  sbody="$key=$val"
  wbody="$key=$(printf '%s' "$val" | jq -sRr @uri)"
  nonce=$(uuid); ts=$(date +%s); date=$(rfc1123)
  sts=$(printf '%s\n%s\n%s\n%s\n%s\nx-ca-key:%s\nx-ca-nonce:%s\nx-ca-signature-method:HmacSHA256\nx-ca-timestamp:%s\n%s?%s' \
    "POST" "application/json" "" "application/x-www-form-urlencoded" \
    "$date" "$APP_KEY" "$nonce" "$ts" "$path" "$sbody")
  sig=$(printf '%s' "$sts" | hs256 "$APP_SECRET")
  vlog "OA POST https://$host$path"
  curl -s "https://${host}${path}" --data "$wbody" \
    -H "accept: application/json" -H "content-type: application/x-www-form-urlencoded" \
    -H "date: $date" -H "x-ca-key: $APP_KEY" -H "x-ca-nonce: $nonce" \
    -H "x-ca-signature: $sig" \
    -H "x-ca-signature-headers: x-ca-nonce,x-ca-timestamp,x-ca-key,x-ca-signature-method" \
    -H "x-ca-signature-method: HmacSHA256" -H "x-ca-timestamp: $ts" \
    -H "user-agent: $UA" ${vid:+-H "vid: $vid"}
}

# ============================================================================
# STEP 2 — region/get -> oa + api endpoints
# ============================================================================
echo "[2/5] resolve region…" >&2
R2=$(iot_call "$BOOTSTRAP" "living/account/region/get" \
      "$(jq -cn --arg a "$AUTHCODE" '{authCode:$a,type:"THIRD_AUTHCODE"}')" "1.0.2")
OA=$(jq -r '.data.oaApiGatewayEndpoint' <<<"$R2")
API=$(jq -r '.data.apiGatewayEndpoint' <<<"$R2")
[[ $OA == null || -z $OA ]] && { echo "region/get failed: $R2" >&2; exit 1; }
vlog "oa=$OA api=$API"

# ============================================================================
# STEP 3 — connect.json -> vid
# ============================================================================
echo "[3/5] OA connect…" >&2
R3=$(oa_call "$OA" "/api/prd/connect.json" \
      "$(jq -cn --arg k "$APP_KEY" '{request:{context:{appKey:$k},config:{version:0,lastModify:0},device:{}}}')")
VID=$(jq -r '.data.vid // .vid // empty' <<<"$R3")
[[ -z $VID ]] && { echo "connect.json failed: $R3" >&2; exit 1; }

# ============================================================================
# STEP 4 — loginbyoauth.json (with vid) -> sid
# ============================================================================
echo "[4/5] OA login…" >&2
R4=$(oa_call "$OA" "/api/prd/loginbyoauth.json" \
      "$(jq -cn --arg a "$AUTHCODE" --arg k "$APP_KEY" \
         '{loginByOauthRequest:{authCode:$a,oauthPlateform:23,oauthAppKey:$k,riskControlInfo:{}}}')" \
      "$VID")
SID=$(jq -r '.data.data.loginSuccessResult.sid // empty' <<<"$R4")
[[ -z $SID ]] && { echo "loginbyoauth failed: $R4" >&2; exit 1; }

# ============================================================================
# STEP 5 — iotToken, find device, read + set bowl weight
# ============================================================================
echo "[5/5] mint iotToken + set bowl weight…" >&2
R5=$(iot_call "$API" "account/createSessionByAuthCode" \
      "$(jq -cn --arg s "$SID" --arg k "$APP_KEY" '{request:{authCode:$s,accountType:"OA_SESSION",appKey:$k}}')" \
      "1.0.4")
IOTTOKEN=$(jq -r '.data.iotToken' <<<"$R5")
[[ $IOTTOKEN == null || -z $IOTTOKEN ]] && { echo "createSession failed: $R5" >&2; exit 1; }

RL=$(iot_call "$API" "uc/listBindingByAccount" '{}' "1.0.8" "$IOTTOKEN")
if [[ -n $DEVICE ]]; then
  IOTID=$(jq -r --arg d "$DEVICE" '.data.data[]? | select(.deviceName==$d) | .iotId' <<<"$RL" | head -1)
  [[ -z $IOTID || $IOTID == null ]] && { echo "no device named '$DEVICE' on this account." >&2
    echo "devices found:" >&2
    jq -r '.data.data[]? | "  \(.deviceName)  (\(.productName))"' <<<"$RL" >&2; exit 1; }
else
  # all Riko/feeder devices on the account
  mapfile -t RIKOS < <(jq -r '.data.data[]? | select((.productName//""|ascii_downcase)|test("riko|feeder|pet")) | .deviceName' <<<"$RL")
  if [[ ${#RIKOS[@]} -eq 0 ]]; then
    # fall back to any single device
    mapfile -t RIKOS < <(jq -r '.data.data[]?.deviceName' <<<"$RL")
  fi
  if [[ ${#RIKOS[@]} -eq 0 ]]; then
    echo "no devices on this account: $RL" >&2; exit 1
  elif [[ ${#RIKOS[@]} -gt 1 ]]; then
    echo "This account has ${#RIKOS[@]} devices. Pick one with -d <device_name>:" >&2
    jq -r '.data.data[]? | "  -d \(.deviceName)   (\(.productName))"' <<<"$RL" >&2
    exit 2
  fi
  DEVICE="${RIKOS[0]}"
  IOTID=$(jq -r --arg d "$DEVICE" '.data.data[]? | select(.deviceName==$d) | .iotId' <<<"$RL" | head -1)
fi
[[ -z $IOTID || $IOTID == null ]] && { echo "couldn't resolve iotId for $DEVICE" >&2; exit 1; }
echo "targeting device: $DEVICE"
vlog "iotId=$IOTID"

RP=$(iot_call "$API" "thing/properties/get" \
      "$(jq -cn --arg i "$IOTID" '{iotId:$i}')" "1.0.4" "$IOTTOKEN")
CUR=$(jq -r '.data.bowlStatus.value.bowlGram // "?"' <<<"$RP")
echo "current stored empty-bowl weight: ${CUR} g"

[[ $DRYRUN == 1 ]] && { echo "[dry run] would set it to ${WEIGHT} g — stopping."; exit 0; }
[[ $CUR == "$WEIGHT" ]] && { echo "already ${WEIGHT} g — nothing to do."; exit 0; }

RS=$(iot_call "$API" "thing/service/invoke" \
      "$(jq -cn --arg i "$IOTID" --argjson w "$WEIGHT" \
         '{iotId:$i,identifier:"resetScaleZero",args:{bowlGram:$w}}')" "1.0.5" "$IOTTOKEN")
if [[ $(jq -r '.code' <<<"$RS") == 200 ]]; then
  echo
  echo "Done — stored bowl weight is now ${WEIGHT} g."
  echo
  echo "Last step: put the food BOWL BACK on the tray."
  read -rp "Bowl replaced? Press Enter to verify: " _
  sleep 2
  RV=$(iot_call "$API" "thing/properties/get" \
        "$(jq -cn --arg i "$IOTID" '{iotId:$i}')" "1.0.4" "$IOTTOKEN")
  SC=$(jq -r '.data.bowlStatus.value.curWeight // "?"' <<<"$RV")
  GR=$(jq -r '.data.bowlStatus.value.bowlGram // "?"' <<<"$RV")
  IN=$(jq -r '.data.bowlStatus.value.bBowlIn // "?"' <<<"$RV")
  if [[ $IN == 1 ]]; then
    LEFT=$(( SC - GR ))
    echo "Bowl detected. Scale reads ${SC} g, stored weight ${GR} g -> ${LEFT} g of food."
    if [[ ${LEFT#-} -le 2 ]]; then
      echo "Looks right: an empty bowl reads about zero. You're done."
    else
      echo "Hmm, that's more than expected for an empty bowl. If the bowl was empty,"
      echo "your bowl may weigh a different amount than ${WEIGHT} g -- weigh it and re-run."
    fi
  else
    echo "(Couldn't confirm the bowl is seated, but the weight was set to ${GR} g.)"
  fi
else
  echo "invoke returned: $RS" >&2; exit 1
fi
