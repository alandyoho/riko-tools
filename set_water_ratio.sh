#!/usr/bin/env bash
#
# set_water_ratio.sh — set the Neakasa Riko's food-to-water ratio past the app's
# 1:5 limit, using only bash + curl + openssl + jq. No Python, no SDK.
#
# WHY THIS EXISTS
#   The Neakasa app won't let you add more than 5 g of water per gram of food.
#   The feeder itself accepts more: it has been run at 1:7 (8 g food / 56 g
#   water) since 2026-09-29 and delivers the full amount. This sets the water for every
#   scheduled meal, and for the default ("feed now") meal, to food x your ratio,
#   through the same cloud API the app uses. Food amounts, meal times and every
#   other setting are left exactly as they are.
#
# USAGE
#   ./set_water_ratio.sh
#
#   There are no options. It asks for your Neakasa login, shows your meals as
#   they are now, asks for the ratio, shows exactly what would change, and
#   writes nothing until you type "yes".
#
# TO UNDO
#   Run it again and enter 5 (the app's maximum), or edit the meals in the app.
#
# GOOD TO KNOW
#   * This changes how much water your cat's food is served with. Check the
#     "after" numbers before you confirm, and make sure the bowl can hold them.
#   * Editing a meal in the app sets THAT meal back to at most 1:5 (seen once:
#     one meal edited in the app came back at 1:5, the others kept 1:7). After
#     editing meals in the app, re-run this.
#   * Logging in here may sign the Neakasa app out on your phone; just log back in.
#
# REQUIRES: bash, curl, openssl, jq, xxd. All standard on macOS except jq
#   (`brew install jq`); on Linux: apt install jq xxd.
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

MIN_RATIO=1 MAX_RATIO=10         # grams of water per gram of food this script will set
MAX_WATER=600                    # the feeder's own per-meal water limit, in grams
VERBOSE=0; [[ -n ${DEBUG:-} ]] && VERBOSE=1

if [[ $# -gt 0 ]]; then
  echo "This script takes no options — just run it:  $0" >&2; exit 2
fi
if [[ ! -t 0 || ! -t 1 ]]; then
  echo "Run this in a terminal; it asks questions and needs your answers." >&2; exit 2
fi

# --- visual emphasis ----------------------------------------------------------
C_BANNER=$'\033[1;44;97m'   # bold white on blue
C_ACT=$'\033[1;92m'         # bold green — something you need to answer
C_HDR=$'\033[1;96m'         # bold cyan
C_WARN=$'\033[1;93m'        # bold yellow
C_OFF=$'\033[0m'
banner(){ echo; echo "${C_BANNER} $* ${C_OFF}"; }
vlog(){ [[ $VERBOSE == 1 ]] && echo "  $*" >&2 || true; }

echo
echo "${C_BANNER} Neakasa Riko — water ratio ${C_OFF}"
echo
echo "The app stops at 1:5 food to water. This sets a higher (or lower) ratio for"
echo "every scheduled meal and for the default meal. It changes the WATER amounts"
echo "only — food amounts and meal times stay as they are."
echo
echo "${C_HDR}It logs into your Neakasa account, exactly like the app does.${C_OFF}"
echo "Your password is used only to log in and is never saved or sent anywhere but"
echo "Neakasa. The full source is in this file — read it before running."
echo "Nothing is changed until you have seen the new numbers and typed \"yes\"."
echo
read -rp "${C_ACT}➤ Your Neakasa account email: ${C_OFF}" EMAIL
[[ -z $EMAIL ]] && { echo "No email entered — nothing to do."; exit 2; }

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

read -rsp "${C_ACT}➤ Password for ${EMAIL}: ${C_OFF}" PASSWORD; echo
echo "  (typing is hidden; this is only sent to Neakasa to log in)"
[[ -z $PASSWORD ]] && { echo "No password entered." >&2; exit 2; }

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
# STEP 5 — iotToken, find the feeder
# ============================================================================
echo "[5/5] open device session…" >&2
R5=$(iot_call "$API" "account/createSessionByAuthCode" \
      "$(jq -cn --arg s "$SID" --arg k "$APP_KEY" '{request:{authCode:$s,accountType:"OA_SESSION",appKey:$k}}')" \
      "1.0.4")
IOTTOKEN=$(jq -r '.data.iotToken' <<<"$R5")
[[ $IOTTOKEN == null || -z $IOTTOKEN ]] && { echo "createSession failed: $R5" >&2; exit 1; }

# Re-mint the iotToken. It expires quickly (≈ tens of seconds), so any step that
# happens after you've been asked something must refresh first, or calls fail
# with code 29003.
refresh_token() {
  local r t
  r=$(iot_call "$API" "account/createSessionByAuthCode" \
        "$(jq -cn --arg s "$SID" --arg k "$APP_KEY" '{request:{authCode:$s,accountType:"OA_SESSION",appKey:$k}}')" \
        "1.0.4")
  t=$(jq -r '.data.iotToken // empty' <<<"$r")
  [[ -n $t ]] && IOTTOKEN=$t
  return 0
}

RL=$(iot_call "$API" "uc/listBindingByAccount" '{}' "1.0.8" "$IOTTOKEN")
DEVICE=""
if true; then
  # all Riko/feeder devices on the account (bash 3.2 compatible — macOS ships 3.2)
  RIKOS=(); LABELS=()
  while IFS= read -r _line; do [[ -n $_line ]] && RIKOS+=("$_line"); done < <(
    jq -r '.data.data[]? | select((.productName//""|ascii_downcase)|test("riko|feeder|pet")) | .deviceName' <<<"$RL")
  if [[ ${#RIKOS[@]} -eq 0 ]]; then
    while IFS= read -r _line; do [[ -n $_line ]] && RIKOS+=("$_line"); done < <(
      jq -r '.data.data[]?.deviceName' <<<"$RL")
  fi
  if [[ ${#RIKOS[@]} -eq 0 ]]; then
    echo "no devices on this account: $RL" >&2; exit 1
  elif [[ ${#RIKOS[@]} -gt 1 ]]; then
    # interactive picker: arrow keys to move, Enter to select
    # build a label (name + product) for each device, bash 3.2 style
    LABELS=()
    for _d in "${RIKOS[@]}"; do
      _prod=$(jq -r --arg d "$_d" '.data.data[]? | select(.deviceName==$d) | .productName' <<<"$RL" | head -1)
      LABELS+=("$_d   ($_prod)")
    done
    sel=0; n=${#RIKOS[@]}
    draw() {
      printf '\033[?25l' >&2   # hide cursor
      echo "Select a device (↑/↓ or j/k, Enter to choose, q to quit):" >&2
      local i
      for i in "${!LABELS[@]}"; do
        if [[ $i -eq $sel ]]; then printf '  \033[7m> %s\033[0m\n' "${LABELS[$i]}" >&2
        else printf '    %s\n' "${LABELS[$i]}" >&2; fi
      done
    }
    clear_menu() { printf '\033[%dA\033[J' $((n+1)) >&2; }
    draw
    while true; do
      IFS= read -rsn1 key
      case $key in
        $'\x1b') read -rsn2 -t 0.001 k2; key+="$k2" ;;
      esac
      case $key in
        $'\x1b[A'|k) ((sel=(sel-1+n)%n)); clear_menu; draw ;;
        $'\x1b[B'|j) ((sel=(sel+1)%n));   clear_menu; draw ;;
        ""|$'\n') break ;;
        q|Q) printf '\033[?25h' >&2; echo "cancelled" >&2; exit 1 ;;
      esac
    done
    printf '\033[?25h' >&2   # show cursor
    DEVICE="${RIKOS[$sel]}"
    echo "selected: $DEVICE" >&2
  else
    DEVICE="${RIKOS[0]}"
  fi
  if [[ -z ${DEVICE:-} ]]; then DEVICE="${RIKOS[0]}"; fi
  IOTID=$(jq -r --arg d "$DEVICE" '.data.data[]? | select(.deviceName==$d) | .iotId' <<<"$RL" | head -1)
fi
[[ -z $IOTID || $IOTID == null ]] && { echo "couldn't resolve iotId for $DEVICE" >&2; exit 1; }
echo "targeting device: $DEVICE"
vlog "iotId=$IOTID"

# ---- read the feeder's meals --------------------------------------------------
get_props() {   # sets PROPS; refreshes the short-lived token once if it expired
  PROPS=$(iot_call "$API" "thing/properties/get" \
            "$(jq -cn --arg i "$IOTID" '{iotId:$i}')" "1.0.4" "$IOTTOKEN")
  if [[ $(jq -r '.code // 0' <<<"$PROPS") == 29003 ]]; then
    refresh_token
    PROPS=$(iot_call "$API" "thing/properties/get" \
              "$(jq -cn --arg i "$IOTID" '{iotId:$i}')" "1.0.4" "$IOTTOKEN")
  fi
  [[ $(jq -r '.code // 0' <<<"$PROPS") == 200 ]] || { echo "couldn't read the feeder: $PROPS" >&2; exit 1; }
}
read_meals() {  # sets PLAN (schedule JSON) and DEF (default-meal JSON)
  get_props
  PLAN=$(jq -c '.data.fdPlanStr.value | if type == "string" then fromjson else . end' <<<"$PROPS" 2>/dev/null || true)
  DEF=$(jq -c '.data.defFdCfg.value' <<<"$PROPS" 2>/dev/null || true)
  if [[ -z $PLAN || $PLAN == null || -z $DEF || $DEF == null ]] ||
     ! jq -e '(.food|type)=="array" and (.water|type)=="array" and (.time|type)=="array"
              and (.food|length)==(.water|length) and (.food|length)==(.time|length)' <<<"$PLAN" >/dev/null ||
     ! jq -e '(.food|type)=="number" and (.water|type)=="number"' <<<"$DEF" >/dev/null; then
    echo "This device didn't report a meal schedule in the shape this script expects." >&2
    echo "Nothing was changed. (Is it a Riko feeder? Run with DEBUG=1 for details.)" >&2
    vlog "fdPlanStr=$PLAN defFdCfg=$DEF"
    exit 1
  fi
}

# one line per meal: time, food, water, ratio — then the default meal
show_meals() {  # plan-json default-json
  jq -rn --argjson p "$1" --argjson def "$2" '
    def ratio(f; w): if f > 0 then "1:\((w / f * 10 | round) / 10)" else "-" end;
    def pad(n): tostring | (" " * ([n - length, 0] | max)) + .;
    def two: tostring | if length < 2 then "0" + . else . end;
    def hhmm: "\(. / 3600 | floor | two):\(. % 3600 / 60 | floor | two)";
    "    meal       food    water   ratio",
    ( range(0; $p.time | length) as $i
      | "    \($p.time[$i] | hhmm)    \($p.food[$i] | pad(5)) g  \($p.water[$i] | pad(5)) g   \(ratio($p.food[$i]; $p.water[$i]))\(if ($p.bEn[$i] // 1) == 0 then "   (off)" else "" end)" ),
    "    default  \($def.food | pad(5)) g  \($def.water | pad(5)) g   \(ratio($def.food; $def.water))   (used by \"feed now\")"
  '
}

read_meals
OLD_PLAN=$PLAN; OLD_DEF=$DEF
banner "YOUR MEALS NOW"
show_meals "$OLD_PLAN" "$OLD_DEF"

# ---- ask for the ratio ---------------------------------------------------------
echo
echo "How many grams of water per gram of food?"
echo "  The app allows up to 5. This script allows ${MIN_RATIO} to ${MAX_RATIO} (one decimal is fine, e.g. 6.5)."
RATIO=""
while [[ -z $RATIO ]]; do
  read -rp "${C_ACT}➤ Water per gram of food: ${C_OFF}" ans
  if [[ ! $ans =~ ^[0-9]+(\.[0-9])?$ ]] ||
     ! jq -en --argjson r "$ans" --argjson lo "$MIN_RATIO" --argjson hi "$MAX_RATIO" '$r >= $lo and $r <= $hi' >/dev/null; then
    echo "  Please enter a number from ${MIN_RATIO} to ${MAX_RATIO}."
    continue
  fi
  RATIO=$ans
done

NEW_PLAN=$(jq -c --argjson r "$RATIO" '.water = [.food[] | (. * $r | round)]' <<<"$OLD_PLAN")
NEW_DEF=$(jq -c --argjson r "$RATIO" '.water = (.food * $r | round)' <<<"$OLD_DEF")

BIGGEST=$(jq -n --argjson p "$NEW_PLAN" --argjson d "$NEW_DEF" '[$p.water[], $d.water] | max')
if jq -en --argjson b "$BIGGEST" --argjson m "$MAX_WATER" '$b > $m' >/dev/null; then
  echo
  echo "At 1:${RATIO} your largest meal would need ${BIGGEST} g of water, more than the"
  echo "feeder's limit of ${MAX_WATER} g per meal. Nothing was changed. Try a lower ratio."
  exit 1
fi
if jq -en --argjson a "$OLD_PLAN" --argjson b "$NEW_PLAN" --argjson c "$OLD_DEF" --argjson d "$NEW_DEF" \
     '$a.water == $b.water and $c.water == $d.water' >/dev/null; then
  echo
  echo "Every meal is already at 1:${RATIO} — nothing to change. You're done."
  exit 0
fi

FULLEST=$(jq -n --argjson p "$NEW_PLAN" --argjson d "$NEW_DEF" \
  '[range(0; $p.food | length) | $p.food[.] + $p.water[.]] + [$d.food + $d.water] | max')
banner "AFTER THE CHANGE (1:${RATIO})"
show_meals "$NEW_PLAN" "$NEW_DEF"
echo
echo "${C_WARN}Check these numbers.${C_OFF} Food and water are served together, so the fullest"
echo "meal would put ${FULLEST} g in the bowl — make sure your bowl holds that."
echo "Food amounts and meal times are not touched."
echo
read -rp "${C_ACT}➤ Type yes to apply, anything else to cancel: ${C_OFF}" ok
if [[ $ok != yes && $ok != YES && $ok != Yes ]]; then
  echo "Cancelled. No changes made — your feeder is exactly as it was."
  exit 0
fi

# ---- write, then read back -----------------------------------------------------
set_prop() {  # items-json
  local r
  refresh_token
  r=$(iot_call "$API" "thing/properties/set" \
        "$(jq -cn --arg i "$IOTID" --argjson items "$1" '{iotId:$i,items:$items}')" "1.0.5" "$IOTTOKEN")
  vlog "set -> $r"
  [[ $(jq -r '.code // 0' <<<"$r") == 200 ]]
}
# a change made in the app while this sat at the prompt would be overwritten — re-check first
refresh_token
read_meals
if ! jq -en --argjson a "$PLAN" --argjson b "$OLD_PLAN" --argjson c "$DEF" --argjson d "$OLD_DEF" \
       '$a == $b and $c == $d' >/dev/null; then
  echo "Your meals changed while this was waiting (edited in the app?). Nothing was"
  echo "written. Run it again to start from the current settings."
  exit 1
fi

echo "applying…"
if ! set_prop "$(jq -cn --arg p "$NEW_PLAN" '{fdPlanStr:$p}')"; then
  echo "The feeder refused the schedule change. Nothing was changed." >&2; exit 1
fi
if ! set_prop "$(jq -cn --argjson d "$NEW_DEF" '{defFdCfg:$d}')"; then
  echo "${C_WARN}The scheduled meals were updated, but the default meal was not.${C_OFF}" >&2
  echo "Run this again to finish, or set the default meal in the app." >&2; exit 1
fi

sleep 3
refresh_token
read_meals
banner "YOUR MEALS NOW"
show_meals "$PLAN" "$DEF"
echo
if jq -en --argjson a "$PLAN" --argjson b "$NEW_PLAN" --argjson c "$DEF" --argjson d "$NEW_DEF" \
     '$a.water == $b.water and $a.food == $b.food and $c.water == $d.water' >/dev/null; then
  echo "${C_ACT}✓ Done.${C_OFF} Every meal is now 1:${RATIO}. It applies from the next meal."
else
  echo "${C_WARN}The feeder reports different numbers than were sent.${C_OFF} Compare the table"
  echo "above with what you expected; it can be slow to report, so check again in the app."
fi
echo
echo "To undo: run this again and enter 5, or edit the meals in the app."
echo "Editing a meal in the app puts that meal back to 1:5 at most — re-run this afterwards."
