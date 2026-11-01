#!/usr/bin/env python3
"""Bankroll — outil de la tâche planifiée (Supabase + Telegram + API-Football).

Variables d'environnement : SUPA_URL, SUPA_KEY (service_role), TG_TOKEN, AF_KEY.

  bankroll.py users                       -> utilisateurs (id, nom, chat Telegram, config)
  bankroll.py tg-fetch OUTDIR             -> nouveaux messages Telegram (photos téléchargées), JSON
  bankroll.py set-offset N                -> mémorise le dernier message traité
  bankroll.py add FILE.json               -> ajoute des paris [{"user_id","id","data":{...}}]
  bankroll.py find USER_ID TEXTE          -> paris récents dont la sélection contient TEXTE
  bankroll.py patch USER_ID BET_ID JSON   -> corrige un pari (fusion de champs, ex. {"s":200} ou {"st":"won"}) et le marque "man"
  bankroll.py delete USER_ID BET_ID       -> supprime un pari
  bankroll.py settle                      -> règle les paris en attente terminés, JSON des changements appliqués
  bankroll.py summary USER_ID [JOURS]     -> bilan texte des N derniers jours (défaut 1)
  bankroll.py send CHAT TEXTE             -> message Telegram
"""
import json, os, re, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

SUPA = os.environ.get("SUPA_URL", "").rstrip("/")
SKEY = os.environ.get("SUPA_KEY", "")
TGT = os.environ.get("TG_TOKEN", "")
AFK = os.environ.get("AF_KEY", "")

def http(url, headers=None, data=None, timeout=60, method=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def sb(method, path, body=None, prefer=None):
    for attempt in range(4):
        try:
            return _sb(method, path, body, prefer)
        except urllib.error.HTTPError:
            raise
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))

def _sb(method, path, body=None, prefer=None):
    h = {"apikey": SKEY, "Authorization": "Bearer " + SKEY, "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    raw = http(f"{SUPA}/rest/v1/{path}", headers=h, data=None if body is None else json.dumps(body).encode(), method=method)
    return json.loads(raw) if raw.strip() else None

def kv_get(key, default=None):
    r = sb("GET", f"kv?key=eq.{key}&select=value")
    return r[0]["value"] if r else default

def kv_set(key, value):
    sb("POST", "kv", [{"key": key, "value": value}], prefer="resolution=merge-duplicates,return=minimal")

def month_of(d):
    return (d or date.today().isoformat())[:7]

def upsert_bets(rows):
    out = []
    for r in rows:
        d = r["data"]
        out.append({"user_id": r["user_id"], "id": r["id"], "k": r.get("k") or month_of(d.get("d")), "d": d.get("d"),
                    "st": d.get("st", "pending"), "data": d, "updated_at": datetime.now(timezone.utc).isoformat()})
    for i in range(0, len(out), 500):
        sb("POST", "bets", out[i:i + 500], prefer="resolution=merge-duplicates,return=minimal")
    return len(out)

def get_bet(uid, bid):
    r = sb("GET", f"bets?user_id=eq.{uid}&id=eq.{urllib.parse.quote(bid)}&select=id,k,data")
    return r[0] if r else None

def patch_bet(uid, bid, changes, manual=True):
    b = get_bet(uid, bid)
    if not b:
        raise SystemExit(f"pari introuvable {bid}")
    d = dict(b["data"]); d.update(changes)
    if manual:
        d["man"] = True
    sb("PATCH", f"bets?user_id=eq.{uid}&id=eq.{urllib.parse.quote(bid)}",
       {"data": d, "st": d.get("st"), "d": d.get("d"), "updated_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")
    return d

def pnl(b):
    s, o = float(b.get("s") or 0), float(b.get("o") or 0); st = b.get("st")
    if b.get("fb"):
        return s * (o - 1) if st == "won" else (s * (o - 1) / 2 if st == "halfwon" else 0)
    return {"won": s * (o - 1), "lost": -s, "halfwon": s * (o - 1) / 2, "halflost": -s / 2,
            "cashout": float(b.get("ret") or 0) - s}.get(st, 0)

# ---------------- Telegram ----------------
def tg(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    last = None
    for attempt in range(5):
        try:
            return json.loads(http(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=45))
        except Exception as e:
            last = e
            time.sleep(6 * (attempt + 1))
    raise last

def tg_fetch(token, offset, outdir):
    os.makedirs(outdir, exist_ok=True)
    res = tg(token, "getUpdates", offset=int(offset), timeout=0)
    out, new_off = [], int(offset)
    for u in res.get("result", []):
        new_off = max(new_off, u["update_id"] + 1)
        m = u.get("message") or u.get("edited_message")
        if not m:
            continue
        item = {"chat": m["chat"]["id"], "chatType": m["chat"]["type"], "msg": m["message_id"],
                "from": (m.get("from") or {}).get("first_name"), "text": m.get("text") or m.get("caption") or "",
                "date": datetime.fromtimestamp(m["date"], timezone.utc).strftime("%Y-%m-%d"), "photos": []}
        fid = None
        if m.get("photo"):
            fid = m["photo"][-1]["file_id"]
        elif (m.get("document") or {}).get("mime_type", "").startswith("image/"):
            fid = m["document"]["file_id"]
        if fid:
            f = tg(token, "getFile", file_id=fid)["result"]
            ext = os.path.splitext(f["file_path"])[1] or ".jpg"
            p = os.path.join(outdir, f"{m['chat']['id']}_{m['message_id']}{ext}")
            for attempt in range(5):
                try:
                    open(p, "wb").write(http(f"https://api.telegram.org/file/bot{token}/{f['file_path']}", timeout=60)); break
                except Exception:
                    time.sleep(6 * (attempt + 1))
            item["photos"].append(p)
        out.append(item)
    return {"offset": new_off, "messages": out}

# ---------------- API-Football ----------------
API = "https://v3.football.api-sports.io"
_cache = {}
def af(key, path, **params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    if url in _cache:
        return _cache[url]
    for attempt in range(3):
        try:
            d = json.loads(http(url, headers={"x-apisports-key": key}))
            if d.get("errors") and "rateLimit" in json.dumps(d["errors"]):
                time.sleep(2); continue
            _cache[url] = d.get("response", [])
            return _cache[url]
        except Exception:
            time.sleep(2)
    return []

TRANSLIT = str.maketrans({"ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "æ": "ae", "Æ": "ae", "ø": "o", "Ø": "o",
                          "ß": "ss", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "œ": "oe", "ı": "i"})
def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").translate(TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s).split()

def name_match(bet_name, api_name):
    """'Hove' vs 'J. Hove' / 'Runar Ullaland Hove'; 'Mbappé' vs 'Kylian Mbappé'."""
    b, a = norm(bet_name), norm(api_name)
    if not b or not a:
        return False
    a_long = [t for t in a if len(t) > 1]
    if all(t in a_long for t in b if len(t) > 1):
        return True
    return b[-1] == a[-1] and len(b[-1]) > 2

def team_match(q, name):
    qn, nn = " ".join(norm(q)), " ".join(norm(name))
    return bool(qn) and (qn in nn or nn in qn)

FINISHED = {"FT", "AET", "PEN"}
VOIDED = {"CANC", "ABD", "AWD", "WO"}

def find_fixture(key, b):
    day = b["d"]
    days = [day]
    if b.get("ko"):
        days = [b["ko"][:10], day]
    days += [(date.fromisoformat(day) + timedelta(days=1)).isoformat()]  # timezone drift
    teams = af(key, "teams", search=" ".join(norm(b["tm"]))[:40]) if b.get("tm") and len(" ".join(norm(b["tm"]))) >= 3 else []
    q = " ".join(norm(b.get("tm")))
    youth = re.compile(r"\b(u\d\d|ii|iii|w|women|reserves?|b)$", re.I)
    teams.sort(key=lambda t: (" ".join(norm(t["team"]["name"])) != q, bool(youth.search(t["team"]["name"]))))
    for t in teams[:4]:
        tid = t["team"]["id"]
        for dday in dict.fromkeys(days):
            y = int(dday[:4])
            fx = af(key, "fixtures", team=tid, date=dday, season=y) or af(key, "fixtures", team=tid, date=dday, season=y - 1)
            for f in fx:
                other = f["teams"]["away"]["name"] if f["teams"]["home"]["id"] == tid else f["teams"]["home"]["name"]
                if not b.get("op") or team_match(b["op"], other):
                    return f, tid
    return None, None

def sides(b):
    if b.get("tm"):
        return [dict(b)]
    if b.get("mt"):
        parts = [p.strip() for p in re.split(r"\s+(?:–|-|vs\.?|v)\s+|\s*[–]\s*", b["mt"]) if p.strip()]
        if len(parts) == 2:
            return [dict(b, tm=parts[0], op=parts[1]), dict(b, tm=parts[1], op=parts[0])]
    return []

def settle_one(key, b):
    if not b.get("pl"):
        return None, "pas assez d'infos (joueur)"
    cands = sides(b)
    if not cands:
        return None, "pas assez d'infos (équipe ou match)"
    last = (None, "match introuvable")
    for c in cands:
        st, why = settle_side(key, c, len(cands) > 1)
        if st != "__wrongteam__":
            return st, why
        last = (None, why)
    return last

def settle_side(key, b, try_other):
    mk = b.get("mk") or {}
    f, tid = find_fixture(key, b)
    if not f:
        return None, "match introuvable"
    st = f["fixture"]["status"]["short"]
    fid = f["fixture"]["id"]
    if st in VOIDED:
        return "void", f"match {st}"
    if st not in FINISHED:
        return None, f"match pas terminé ({st})"
    # who played
    lineups = af(key, "fixtures/lineups", fixture=fid)
    starter = sub = False; pid = None
    for lu in lineups:
        if lu["team"]["id"] != tid:
            continue
        for p in lu.get("startXI", []):
            if name_match(b["pl"], p["player"]["name"]):
                starter, pid = True, p["player"]["id"]
        for p in lu.get("substitutes", []):
            if name_match(b["pl"], p["player"]["name"]):
                sub, pid = True, p["player"]["id"]
    played = starter
    if sub and not starter:
        for t in af(key, "fixtures/players", fixture=fid):
            if t["team"]["id"] != tid:
                continue
            for p in t.get("players", []):
                if p["player"]["id"] == pid:
                    mins = (p["statistics"][0].get("games") or {}).get("minutes")
                    played = bool(mins)
    # goals (90 min, no own goals, no missed penalties)
    events = af(key, "fixtures/events", fixture=fid)
    goals = []
    for e in events:
        if e.get("type") != "Goal" or e.get("detail") in ("Missed Penalty", "Own Goal"):
            continue
        el, ex = e["time"]["elapsed"] or 0, e["time"].get("extra")
        if el > 90 and not ex:
            continue  # extra time
        half = 1 if el <= 45 else 2
        a = e.get("assist") or {}
        goals.append({"pid": e["player"]["id"], "name": e["player"]["name"], "team": e["team"]["id"], "half": half, "t": el + (ex or 0) / 100,
                      "apid": a.get("id"), "aname": a.get("name")})
    goals.sort(key=lambda g: g["t"])
    mine = [g for g in goals if g["team"] == tid and ((pid and g["pid"] == pid) or name_match(b["pl"], g["name"]))]
    assists = [g for g in goals if g["team"] == tid and ((pid and g["apid"] == pid) or (g["aname"] and name_match(b["pl"], g["aname"])))]
    involved = mine + assists
    # void if the player does not start: Kambi and PMU (both users)
    kambi = (b.get("bm") or "").strip().lower() in ("kambi", "pmu")
    if lineups and not (starter or sub) and not involved:
        squad = af(key, "players/squads", team=tid)
        names = [p["name"] for sq in squad for p in sq.get("players", [])]
        if not any(name_match(b["pl"], n) for n in names):
            if try_other:
                return "__wrongteam__", f"{b['pl']} introuvable dans les deux équipes"
            return None, f"{b['pl']} introuvable dans l'effectif de {f['teams']['home']['name'] if f['teams']['home']['id']==tid else f['teams']['away']['name']}"
    if lineups and not starter and not involved:
        if kambi:
            return "void", f"pas titulaire (règle {b.get('bm')})" if sub else f"absent de la feuille de match (règle {b.get('bm')})"
        if not sub:
            return "lost", "absent de la feuille de match"
    elif kambi and lineups and not starter:
        return "void", f"pas titulaire (règle {b.get('bm')})"
    if not lineups and not involved:
        return None, "compos indisponibles"
    if mk.get("a"):
        ok = len([g for g in assists if not mk.get("h") or g["half"] == mk["h"]]) >= int(mk["a"])
    else:
        need = int(mk.get("g") or 1)
        ok = len([g for g in mine if not mk.get("h") or g["half"] == mk["h"]]) >= need
    if mk.get("first"):
        ok = ok and bool(goals) and goals[0] in mine
    if mk.get("t") in ("o", "u"):
        ft = f["score"]["fulltime"]
        tot = (ft.get("home") or 0) + (ft.get("away") or 0)
        ok = ok and (tot > mk["l"] if mk["t"] == "o" else tot < mk["l"])
    what = f"{len(assists)} passe(s) déc." if mk.get("a") else f"{len(mine)} but(s)"
    why = f'{f["teams"]["home"]["name"]} {f["goals"]["home"]}-{f["goals"]["away"]} {f["teams"]["away"]["name"]}, {what} de {b["pl"]}'
    return ("won" if ok else "lost"), why


def settle_all():
    rows = sb("GET", "bets?st=eq.pending&select=user_id,id,data&limit=2000") or []
    today = datetime.now(timezone.utc).date().isoformat()
    changes = []
    for r in rows:
        b = r["data"]
        if b.get("man") or b.get("lg") or not b.get("pl") or (b.get("d") or "9999") > today:
            continue
        try:
            st, why = settle_one(AFK, b)
        except Exception as e:
            st, why = None, f"erreur {e}"
        if st:
            patch_bet(r["user_id"], r["id"], {"st": st, "auto": why}, manual=False)
            changes.append({"user_id": r["user_id"], "id": r["id"], "sel": b.get("sel"), "bm": b.get("bm"),
                            "s": b.get("s"), "o": b.get("o"), "st": st, "why": why, "pnl": round(pnl(dict(b, st=st)), 2)})
        else:
            changes.append({"user_id": r["user_id"], "id": r["id"], "sel": b.get("sel"), "st": None, "why": why})
    return changes

def summary(uid, days=1):
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    rows = sb("GET", f"bets?user_id=eq.{uid}&d=gte.{since}&select=data") or []
    bets = [r["data"] for r in rows if not r["data"].get("lg")]
    settled = [b for b in bets if b.get("st") not in ("pending", None)]
    p = sum(pnl(b) for b in settled); staked = sum(float(b.get("s") or 0) for b in settled if not b.get("fb"))
    pend = [b for b in bets if b.get("st") == "pending"]
    return f"{len(settled)} réglés, bénéfice {p:+.2f} € (mises {staked:.2f} €), {len(pend)} en attente"

if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0]
    if cmd == "users":
        print(json.dumps(sb("GET", "users?select=id,name,tg_chat,config,token"), ensure_ascii=False, indent=1))
    elif cmd == "tg-fetch":
        off = kv_get("tg_offset", 0)
        print(json.dumps(tg_fetch(TGT, off, a[1]), ensure_ascii=False))
    elif cmd == "set-offset":
        kv_set("tg_offset", int(a[1])); print("ok")
    elif cmd == "add":
        print(upsert_bets(json.load(open(a[1]))))
    elif cmd == "find":
        q = " ".join(a[2:])
        rows = sb("GET", f"bets?user_id=eq.{a[1]}&data->>sel=ilike.*{urllib.parse.quote(q)}*&select=id,data&order=d.desc&limit=10") or []
        print(json.dumps([{"id": r["id"], **{k: r["data"].get(k) for k in ("d", "sel", "bm", "o", "s", "st")}} for r in rows], ensure_ascii=False, indent=1))
    elif cmd == "patch":
        print(json.dumps(patch_bet(a[1], a[2], json.loads(a[3])), ensure_ascii=False))
    elif cmd == "delete":
        sb("DELETE", f"bets?user_id=eq.{a[1]}&id=eq.{urllib.parse.quote(a[2])}", prefer="return=minimal"); print("ok")
    elif cmd == "settle":
        print(json.dumps(settle_all(), ensure_ascii=False, indent=1))
    elif cmd == "summary":
        print(summary(a[1], int(a[2]) if len(a) > 2 else 1))
    elif cmd == "send":
        print(tg(TGT, "sendMessage", chat_id=a[1], text=" ".join(a[2:]), disable_web_page_preview="true").get("ok"))
