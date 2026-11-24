"""
Falench. チケット発売リマインダー

  - 朝: 「今日発売のチケット一覧」（＋翌日0:00〜朝までの発売分）を通知
  - 発売の 60分前 / 30分前 / 5分前 に通知（config.json の remind_offsets_minutes で変更可）

GitHub Actions から1時間ごとに起動される想定。
起動時に「lookahead_minutes 以内に送る通知」があれば、その時刻までこのジョブの中で待ってから送る
（GitHub の起動遅れに左右されず、ほぼ時刻ぴったりに届く）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import sources

JST = ZoneInfo("Asia/Tokyo")
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
STATE_FILE = os.path.join(BASE, "state.json")

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_TO = os.environ.get("LINE_TO") or os.environ.get("LINE_GROUP_ID") or os.environ.get("LINE_USER_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GIT_PUSH = os.environ.get("STATE_GIT_PUSH") == "1"
CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY")
CRONJOB_API = "https://api.cron-job.org"

WD = "月火水木金土日"

DEFAULT_CONFIG = {
    "keyword": "Falench",
    "group_name": "Falench.",
    "remind_offsets_minutes": [60, 30, 5],
    "morning_time": "06:45",
    "morning_include_until_hour_next_day": 7,
    "morning_send_when_empty": False,
    "collect_every_minutes": 180,
    "lookahead_minutes": 90,
    "max_run_minutes": 330,
    "remind_sale_types": ["first_come", "unknown"],
    "line_offsets_minutes": [30, 5],
    "line_protected_offsets_minutes": [5],
    "line_morning": True,
    "line_reserve_extra_sends": 3,
    "line_group_size": 5,
    "discord_mention": "",
    "cronjob_offsets_minutes": [5],
    "cronjob_verify_delay_seconds": 40,
    "ticketdive_list_urls": ["https://ticketdive.com/search?q=Falench"],
    "livepocket_list_urls": ["https://livepocket.jp/event/search?search_word=Falench"],
    "organizers_file": "organizers.json",
}


# ---------------------------------------------------------------- 時刻（テストで差し替えられるように関数化）
def now():
    return datetime.now(JST)


def sleep(seconds):
    time.sleep(max(0, seconds))


# ---------------------------------------------------------------- 設定・状態
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    org_path = os.path.join(BASE, cfg.get("organizers_file") or "")
    if cfg.get("organizers_file") and os.path.exists(org_path):
        with open(org_path, encoding="utf-8") as f:
            for org in json.load(f):
                if org.get("url") and org["url"] not in cfg["livepocket_list_urls"]:
                    cfg["livepocket_list_urls"].append(org["url"])
    return cfg


def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f) or {}
        except Exception as e:
            print(f"[WARN] state.json の読み込み失敗（空で開始）: {e}")
    st.setdefault("events", {})
    st.setdefault("lp_excluded", {})
    st.setdefault("sent", {})
    st.setdefault("morning_sent", "")
    st.setdefault("last_collect", "")
    st.setdefault("cron_jobs", {})
    return st


def save_state(st, message="Update reminder state"):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)
    if not GIT_PUSH:
        return
    try:
        subprocess.run(["git", "add", "state.json"], cwd=BASE, check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=BASE).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message, "-q"], cwd=BASE, check=True)
        for _ in range(3):
            subprocess.run(["git", "pull", "--rebase", "-q"], cwd=BASE)
            if subprocess.run(["git", "push", "-q"], cwd=BASE).returncode == 0:
                return
            sleep(3)
        print("[WARN] state.json の push に失敗しました")
    except Exception as e:
        print(f"[WARN] state.json の保存(git)失敗: {e}")


# ---------------------------------------------------------------- 収集
def collect(st, cfg):
    t0 = time.time()
    today = now().date()
    browser = None
    try:
        try:
            browser = sources.Browser()
        except Exception as e:
            print(f"[WARN] Playwrightを起動できません（LivePocketは取得不可）: {e}")

        def rendered(url):
            return browser.get(url, extra_wait_ms=2500)

        found = []
        try:
            found += sources.collect_ticketdive(cfg, rendered if browser else None, today)
        except Exception as e:
            print(f"[ERROR] TicketDive収集エラー: {e}")
        if browser:
            known_lp = [u for u, e in st["events"].items() if e.get("source") == "LivePocket"]
            try:
                found += sources.collect_livepocket(cfg, browser, st["lp_excluded"], known_lp, today)
            except Exception as e:
                print(f"[ERROR] LivePocket収集エラー: {e}")
    finally:
        if browser:
            browser.close()

    for ev in found:
        old = st["events"].get(ev["url"])
        # 今回なぜか販売情報が取れなかった場合は、前回の情報を残す
        if old and not ev["sales"] and old.get("sales"):
            ev["sales"] = old["sales"]
        ev["updated"] = now().isoformat(timespec="seconds")
        st["events"][ev["url"]] = ev

    prune(st)
    st["last_collect"] = now().isoformat(timespec="seconds")
    n_sales = sum(len(e["sales"]) for e in st["events"].values())
    print(f"[COLLECT] イベント {len(st['events'])} 件 / 販売開始 {n_sales} 件  [{time.time() - t0:.0f}s]")
    save_state(st, "Collect ticket sales")


def prune(st):
    t = now()
    limit_day = (t.date() - timedelta(days=2)).isoformat()
    for url in list(st["events"]):
        ev = st["events"][url]
        future_sale = any(datetime.fromisoformat(s["start"]) > t for s in ev.get("sales", []))
        if ev.get("event_day") and ev["event_day"] < limit_day and not future_sale:
            del st["events"][url]
    for k, v in list(st["sent"].items()):
        if v < (t - timedelta(days=3)).isoformat():
            del st["sent"][k]
    for u, d in list(st["lp_excluded"].items()):
        if d < (t.date() - timedelta(days=60)).isoformat():
            del st["lp_excluded"][u]


def collect_due(st, cfg):
    if not st["last_collect"]:
        return True
    last = datetime.fromisoformat(st["last_collect"])
    return now() - last >= timedelta(minutes=cfg["collect_every_minutes"])


# ---------------------------------------------------------------- 予定の組み立て
def all_sales(st):
    """[(sale_start, event, sale)]（時刻つきのもの）"""
    out = []
    for ev in st["events"].values():
        for s in ev.get("sales", []):
            if s.get("has_time", True):
                out.append((datetime.fromisoformat(s["start"]), ev, s))
    out.sort(key=lambda x: (x[0], x[1]["title"]))
    return out


def reminder_key(ev, s, offset):
    return f"{ev['url']}|{s['start']}|{s.get('name', '')}|{offset}"


def wants_reminder(s, cfg):
    return s.get("type", "unknown") in cfg["remind_sale_types"]


def reminders(st, cfg):
    """
    未送信のリマインド [(action_at, offset, start, ev, s, key)]
    action_at: このジョブが処理する時刻。cron-job.org に任せている通知は、
               届いたかを確認するため少し後ろ（verify_delay 秒後）にずらす
    """
    out = []
    delay = timedelta(seconds=cfg["cronjob_verify_delay_seconds"])
    for start, ev, s in all_sales(st):
        if not wants_reminder(s, cfg):
            continue
        for off in sorted(cfg["remind_offsets_minutes"], reverse=True):
            key = reminder_key(ev, s, off)
            if key in st["sent"]:
                continue
            fire_at = start - timedelta(minutes=off)
            if cron_job_for(st, key, "discord") or cron_job_for(st, key, "line"):
                fire_at += delay
            out.append((fire_at, off, start, ev, s, key))
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------- メッセージ
def fmt_day(d):
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def fmt_hm(dt):
    return f"{dt.hour}:{dt.minute:02d}"


TYPE_LABEL = {"first_come": "【先着】", "lottery": "【抽選】", "unknown": ""}


def sale_block(ev, s, start=None, show_day=False):
    lines = [f"📌 {ev['title']}"]
    if s.get("name"):
        lines.append(f"🎫 {s['name']}")
    if start is not None and show_day:
        lines.append(f"🕒 発売: {fmt_day(start)} {fmt_hm(start)}")
    if ev.get("event_date"):
        lines.append(f"📅 公演: {ev['event_date']}")
    lines.append(f"🛒 {ev.get('source', '')}  {ev['url']}")
    return "\n".join(lines)


def build_morning(st, cfg):
    t = now()
    today = t.date()
    end = datetime(today.year, today.month, today.day, tzinfo=JST) + timedelta(
        days=1, hours=cfg["morning_include_until_hour_next_day"])
    today_items, night_items, notime_items = [], [], []
    for ev in st["events"].values():
        for s in ev.get("sales", []):
            start = datetime.fromisoformat(s["start"])
            if not s.get("has_time", True):
                if start.date() == today:
                    notime_items.append((start, ev, s))
                continue
            if start.date() == today:
                today_items.append((start, ev, s))
            elif today < start.date() and start < end:
                night_items.append((start, ev, s))
    if not (today_items or night_items or notime_items):
        return None

    g = cfg["group_name"]
    parts = [f"🌅【{g}】本日 {fmt_day(today)} のチケット発売"]
    if today_items:
        for start, ev, s in sorted(today_items, key=lambda x: x[0]):
            mark = "（発売済み）" if start <= t else ""
            parts.append(f"⏰ {fmt_hm(start)} {_kind(s, cfg)}{mark}\n{sale_block(ev, s)}")
    else:
        parts.append("本日発売のチケットはありません。")
    for start, ev, s in notime_items:
        parts.append(f"⏰ 本日発売（時刻はページで確認）\n{sale_block(ev, s)}")
    if night_items:
        parts.append(f"🌙 明日 {fmt_day(today + timedelta(days=1))} の深夜・早朝の発売")
        for start, ev, s in sorted(night_items, key=lambda x: x[0]):
            parts.append(f"⏰ {fmt_hm(start)} {_kind(s, cfg)}\n{sale_block(ev, s)}")
    offs = "・".join(_off_label(o) for o in sorted(cfg["remind_offsets_minutes"], reverse=True))
    parts.append(f"🔔 先着販売は{offs}前にもお知らせします（抽選はお知らせしません）")
    return "\n\n".join(parts)


def _kind(s, cfg):
    label = {"first_come": "先着販売開始", "lottery": "抽選受付開始"}.get(s.get("type"), "発売")
    if not wants_reminder(s, cfg):
        label += "（リマインドなし）"
    return label


def _off_label(o):
    return f"{o // 60}時間" if o % 60 == 0 and o >= 60 else f"{o}分"


def build_reminder(items, ref=None):
    """items: [(offset, start, ev, s)]  同じタイミングで送るものをまとめて1通に"""
    t = ref or now()
    offsets = {o for o, *_ in items}
    if len(offsets) == 1:
        o = next(iter(offsets))
        head = f"⏰【あと{_off_label(o)}】チケット発売"
    else:
        head = "⏰【まもなく】チケット発売"
    parts = [head]
    for o, start, ev, s in sorted(items, key=lambda x: x[1]):
        left = max(1, round((start - t).total_seconds() / 60))
        day = "" if start.date() == t.date() else f"{fmt_day(start)} "
        parts.append(f"🕒 {day}{fmt_hm(start)} {TYPE_LABEL.get(s.get('type'), '')}発売（あと約{left}分）\n"
                     f"{sale_block(ev, s)}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 送信
DRY_RUN = False


def send(text, to_line=True, mention=False, discord=True):
    """Discord と（to_line なら）LINE に送る"""
    print(f"----- 送信内容（Discord={discord and bool(DISCORD_WEBHOOK_URL)} LINE={to_line}） -----\n"
          + text + "\n--------------------")
    if DRY_RUN:
        return True
    ok = []
    if discord and DISCORD_WEBHOOK_URL:
        ok.append(send_discord(text, mention))
    if to_line:
        if LINE_TOKEN and LINE_TO:
            ok.append(send_line(text))
        else:
            print("[INFO] LINEの設定が未設定")
    return any(ok) if ok else False


def _chunks(text, limit):
    out, cur = [], ""
    for block in text.split("\n\n"):
        cand = (cur + "\n\n" + block) if cur else block
        if len(cand) > limit and cur:
            out.append(cur)
            cur = block[:limit]
        else:
            cur = cand[:limit]
    if cur:
        out.append(cur)
    return out


def send_discord(text, mention=False, cfg_mention=None):
    m = (CFG or {}).get("discord_mention", "") if cfg_mention is None else cfg_mention
    all_ok = True
    for i, chunk in enumerate(_chunks(text, 1900)):
        content = f"{m}\n{chunk}" if (mention and m and i == 0) else chunk
        payload = {"content": content,
                   "allowed_mentions": {"parse": ["everyone", "roles", "users"] if (mention and m) else []}}
        for attempt in range(3):
            try:
                r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            except Exception as e:
                print(f"[WARN] Discord送信エラー: {e}")
                sleep(2)
                continue
            if r.status_code == 429:
                sleep(min(float(r.json().get("retry_after", 1)), 10))
                continue
            break
        else:
            all_ok = False
            continue
        if r.status_code not in (200, 204):
            print(f"[ERROR] Discord送信失敗: {r.status_code} {r.text[:200]}")
            all_ok = False
    return all_ok


def send_line(text):
    all_ok = True
    for chunk in _chunks(text, 4500):
        try:
            r = requests.post("https://api.line.me/v2/bot/message/push",
                              headers={"Authorization": f"Bearer {LINE_TOKEN}"},
                              json={"to": LINE_TO, "messages": [{"type": "text", "text": chunk}]},
                              timeout=20)
        except Exception as e:
            print(f"[WARN] LINE送信エラー: {e}")
            all_ok = False
            continue
        if r.status_code != 200:
            print(f"[ERROR] LINE送信失敗: {r.status_code} {r.text[:200]}")
            all_ok = False
    return all_ok


# ---------------------------------------------------------------- 処理
def morning_due(st, cfg):
    t = now()
    hh, mm = map(int, cfg["morning_time"].split(":"))
    return st["morning_sent"] != t.date().isoformat() and (t.hour, t.minute) >= (hh, mm)


def do_morning(st, cfg):
    msg = build_morning(st, cfg)
    if not msg and cfg["morning_send_when_empty"]:
        msg = f"🌅【{cfg['group_name']}】本日 {fmt_day(now().date())} 発売のチケットはありません。"
    if msg:
        to_line = cfg["line_morning"] and line_budget_ok(st, cfg, protected=False, label="朝の一覧")
        send(msg, to_line=to_line)
    else:
        print("[MORNING] 本日の発売なし（通知しない設定）")
    st["morning_sent"] = now().date().isoformat()
    save_state(st, "Morning list sent")


def process_due(st, cfg):
    """時刻が来たリマインドを送る。遅れて複数たまった場合は一番直近のものだけ送る"""
    t = now()
    due = [r for r in reminders(st, cfg) if r[0] <= t + timedelta(seconds=1)]
    if not due:
        return
    latest = {}  # 販売ごとに一番小さい offset（＝直近）のもの
    for fire_at, off, start, ev, s, key in due:
        st["sent"][key] = t.isoformat(timespec="seconds")
        if start <= t:
            continue  # もう発売開始している
        sid = reminder_key(ev, s, "")
        if sid not in latest or off < latest[sid][0]:
            latest[sid] = (off, start, ev, s, key)

    discord_items, line_items = [], []
    for off, start, ev, s, key in latest.values():
        item = (off, start, ev, s)
        job = cron_job_for(st, key, "discord")
        if job and cron_delivered(job):
            print(f"[CRON] Discord: cron-job.org から送信済み ({ev['title']} {off}分前)")
        else:
            if job:
                print(f"[CRON] Discord: cron-job.org の送信を確認できず → こちらから送ります")
            discord_items.append(item)
        if off in cfg["line_offsets_minutes"]:
            job = cron_job_for(st, key, "line")
            if job and cron_delivered(job):
                print(f"[CRON] LINE: cron-job.org から送信済み ({ev['title']} {off}分前)")
            elif line_budget_ok(st, cfg, protected=off in cfg["line_protected_offsets_minutes"],
                                label=f"{off}分前"):
                if job:
                    print(f"[CRON] LINE: cron-job.org の送信を確認できず → こちらから送ります")
                line_items.append(item)

    if discord_items:
        mention = min(i[0] for i in discord_items) == min(cfg["remind_offsets_minutes"])
        send(build_reminder(discord_items), to_line=False, mention=mention)
    if line_items:
        send(build_reminder(line_items), to_line=True, discord=False)
    save_state(st, "Reminder sent")


# ---------------------------------------------------------------- LINEの残り通数の管理
def _line_get(path):
    r = requests.get("https://api.line.me" + path, timeout=15,
                     headers={"Authorization": f"Bearer {LINE_TOKEN}"})
    r.raise_for_status()
    return r.json()


_MEMBERS = {}


def line_members(cfg):
    """送り先の人数（1回の送信で減る通数）"""
    if "n" in _MEMBERS:
        return _MEMBERS["n"]
    n = cfg["line_group_size"]
    try:
        if LINE_TO.startswith("C"):
            n = int(_line_get(f"/v2/bot/group/{LINE_TO}/members/count")["count"])
        elif LINE_TO.startswith("R"):
            n = int(_line_get(f"/v2/bot/room/{LINE_TO}/members/count")["count"])
        elif LINE_TO.startswith("U"):
            n = 1
    except Exception as e:
        print(f"[WARN] LINEの人数を取得できず、設定値 {n} 人で計算します: {e}")
    _MEMBERS["n"] = n
    return n


def protected_line_left_this_month(st, cfg):
    """今月これから送る予定の「必ず送るLINE通知」（5分前）の回数"""
    t = now()
    nxt = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fires = set()  # 同じ時刻の発売は1通にまとまるので、時刻の数で数える
    for start, ev, s in all_sales(st):
        if not wants_reminder(s, cfg):
            continue
        for off in cfg["line_protected_offsets_minutes"]:
            if off not in cfg["line_offsets_minutes"]:
                continue
            fire = start - timedelta(minutes=off)
            if t <= fire < nxt and reminder_key(ev, s, off) not in st["sent"]:
                fires.add(fire.replace(second=0, microsecond=0))
    return len(fires)


def line_budget_ok(st, cfg, protected=False, label=""):
    """
    LINEに送ってよいか。5分前（protected）は常に送る。
    それ以外は「今月残りの5分前通知＋予備」の分を残せるときだけ送る。
    """
    if not (LINE_TOKEN and LINE_TO):
        return False
    if protected or DRY_RUN:
        return True
    try:
        q = _line_get("/v2/bot/message/quota")
        if q.get("type") != "limited":
            return True
        limit = int(q["value"])
        used = int(_line_get("/v2/bot/message/quota/consumption")["totalUsage"])
    except Exception as e:
        print(f"[WARN] LINEの残り通数を確認できませんでした（送ります）: {e}")
        return True
    members = line_members(cfg)
    reserve = protected_line_left_this_month(st, cfg) + cfg["line_reserve_extra_sends"]
    left = limit - used
    need = members * (reserve + 1)
    ok = left >= need
    print(f"[LINE] 残り {left}/{limit} 通・{members}人宛て・5分前用に確保 {members * reserve} 通 → "
          f"{label} は{'LINEにも送る' if ok else 'Discordだけ（LINE節約）'}")
    return ok


# ---------------------------------------------------------------- cron-job.org（時刻ちょうどにDiscordへ直接送る予備ルート）
def _cron_req(method, path, body=None):
    r = requests.request(method, CRONJOB_API + path, json=body, timeout=20,
                         headers={"Authorization": f"Bearer {CRONJOB_API_KEY}",
                                  "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
    return r.json() if r.text else {}


def cron_delivered(job):
    """cron-job.org の実行履歴で、予定時刻ごろに成功しているか。確認できなければ False"""
    if DRY_RUN or not CRONJOB_API_KEY:
        return False
    try:
        fire_ts = datetime.fromisoformat(job["fire_at"]).timestamp()
        hist = _cron_req("GET", f"/jobs/{job['id']}/history").get("history", [])
        for h in hist:
            if h.get("status") == 1 and h.get("date", 0) >= fire_ts - 120 and h.get("httpStatus") in (200, 204):
                return True
        print(f"[CRON] 履歴: {[(h.get('status'), h.get('httpStatus'), h.get('statusText')) for h in hist[:3]]}")
    except Exception as e:
        print(f"[WARN] cron-job.org 履歴の取得失敗: {e}")
    return False


def cron_job_for(st, key, channel):
    """そのリマインドを含む cron-job.org の予約（同じ時刻の発売は1つの予約にまとめている）"""
    for gkey, job in st["cron_jobs"].items():
        if gkey.endswith(f"|{channel}") and key in job.get("keys", []):
            return job
    return None


def _cron_request(channel, items, fire, cfg):
    """(url, headers, body) … cron-job.org が予定時刻に送るリクエスト。items: [(off, start, ev, s)]"""
    off = items[0][0]
    text = build_reminder(items, ref=fire)
    if channel == "line":
        return ("https://api.line.me/v2/bot/message/push",
                {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"},
                {"to": LINE_TO, "messages": [{"type": "text", "text": text[:4900]}]})
    m = cfg.get("discord_mention", "")
    is_min = off == min(cfg["remind_offsets_minutes"])
    content = (f"{m}\n{text}" if (m and is_min) else text)[:1990]
    return (DISCORD_WEBHOOK_URL, {"Content-Type": "application/json"},
            {"content": content,
             "allowed_mentions": {"parse": ["everyone", "roles", "users"] if (m and is_min) else []}})


def _cron_channels(off, cfg):
    ch = []
    if DISCORD_WEBHOOK_URL:
        ch.append("discord")
    if LINE_TOKEN and LINE_TO and off in cfg["line_offsets_minutes"] \
            and off in cfg["line_protected_offsets_minutes"]:
        ch.append("line")
    return ch


def sync_cronjobs(st, cfg):
    """
    先着販売の 5分前（cronjob_offsets_minutes）通知を、LINE・Discord それぞれ cron-job.org に予約しておく。
    同じ時刻に送るものは1つの予約（＝1通）にまとめる。
    """
    if not CRONJOB_API_KEY or not cfg["cronjob_offsets_minutes"]:
        return
    t = now()
    jobs = st["cron_jobs"]
    desired = {}  # gkey -> {"fire","off","ch","items":[...],"keys":[...]}
    for start, ev, s in all_sales(st):
        if not wants_reminder(s, cfg):
            continue
        for off in cfg["cronjob_offsets_minutes"]:
            key = reminder_key(ev, s, off)
            fire = start - timedelta(minutes=off)
            if key in st["sent"] or not (t + timedelta(minutes=3) < fire < t + timedelta(days=2)):
                continue
            for ch in _cron_channels(off, cfg):
                g = desired.setdefault(f"{fire.isoformat()}|{off}|{ch}",
                                       {"fire": fire, "off": off, "ch": ch, "items": [], "keys": []})
                g["items"].append((off, start, ev, s))
                g["keys"].append(key)

    changed = False
    # 不要になった予約・中身が変わった予約（発売時刻の変更や同時刻の追加）・終わった予約を消す
    for gkey, job in list(jobs.items()):
        fire = datetime.fromisoformat(job["fire_at"])
        finished = fire < t - timedelta(minutes=15)
        want = desired.get(gkey)
        stale = fire > t + timedelta(minutes=3) and (want is None or sorted(want["keys"]) != sorted(job["keys"]))
        if not (finished or stale):
            continue
        if finished:
            unsent = [k for k in job["keys"] if k not in st["sent"]]
            if unsent:
                # GitHub側が動いていなかった場合: もう遅いので記録だけ残す
                ok = cron_delivered(job)
                for k in unsent:
                    st["sent"][k] = t.isoformat(timespec="seconds")
                print(f"[CRON] 後から確認: 送信{'成功' if ok else '失敗'} ({gkey})")
        try:
            if not DRY_RUN:
                _cron_req("DELETE", f"/jobs/{job['id']}")
        except Exception as e:
            print(f"[WARN] cron-job.org 予約の削除失敗: {e}")
        del jobs[gkey]
        changed = True

    created = 0
    for gkey, g in sorted(desired.items(), key=lambda x: x[1]["fire"]):
        if gkey in jobs:
            continue
        if created >= 4:  # 作成は1分に5回までの制限があるので、残りは次回
            break
        fire, off, ch = g["fire"], g["off"], g["ch"]
        url, headers, payload = _cron_request(ch, g["items"], fire, cfg)
        title = g["items"][0][2]["title"][:30] + (f" ほか{len(g['items']) - 1}件" if len(g["items"]) > 1 else "")
        body = {"job": {
            "url": url, "enabled": True, "saveResponses": False,
            "title": f"{cfg['group_name']} {ch} {off}分前 {fire:%m/%d %H:%M} {title}",
            "requestTimeout": 30, "requestMethod": 1,
            "schedule": {"timezone": "Asia/Tokyo",
                         "expiresAt": int((fire + timedelta(minutes=10)).strftime("%Y%m%d%H%M%S")),
                         "hours": [fire.hour], "mdays": [fire.day], "minutes": [fire.minute],
                         "months": [fire.month], "wdays": [-1]},
            "extendedData": {"headers": headers, "body": json.dumps(payload, ensure_ascii=False)},
            "notification": {"onFailure": False, "onSuccess": False, "onDisable": False},
        }}
        try:
            if DRY_RUN:
                print(f"[CRON] (dry-run) 予約: {ch} {fire:%m/%d %H:%M} {title}")
                continue
            job_id = _cron_req("PUT", "/jobs", body)["jobId"]
            jobs[gkey] = {"id": job_id, "fire_at": fire.isoformat(), "keys": g["keys"]}
            print(f"[CRON] 予約しました: {ch} {fire:%m/%d %H:%M} {title} (job {job_id})")
            created += 1
            changed = True
            sleep(1.5)
        except Exception as e:
            print(f"[WARN] cron-job.org への予約失敗（GitHub側だけで送ります）: {e}")
            break
    if changed:
        save_state(st, "Sync cron-job.org reservations")


CFG = None


def run(args):
    global CFG
    cfg = load_config()
    CFG = cfg
    st = load_state()
    started = now()
    deadline = started + timedelta(minutes=cfg["max_run_minutes"])
    lookahead = timedelta(minutes=args.lookahead or cfg["lookahead_minutes"])

    if args.test:
        send(f"✅【{cfg['group_name']}】発売リマインダーのテスト通知です（{fmt_day(started)} {fmt_hm(started)}）")
        return

    def next_fire():
        r = reminders(st, cfg)
        future = [x for x in r if x[2] > now()]
        return future[0][0] if future else None

    first = True
    while True:
        t = now()
        nf = next_fire()
        # 収集は時間がかかるので、次の通知まで20分以上あるときだけ行う
        forced = args.collect and first
        # 朝の一覧の直前は、30分以内に収集していなければ収集し直す
        want = collect_due(st, cfg) or (morning_due(st, cfg) and _stale(st, 30))
        if forced or (want and (nf is None or nf - t > timedelta(minutes=20))):
            collect(st, cfg)
        first = False
        sync_cronjobs(st, cfg)
        if morning_due(st, cfg):
            do_morning(st, cfg)
        process_due(st, cfg)

        nf = next_fire()
        t = now()
        if args.no_wait or nf is None or nf - t > lookahead or nf > deadline:
            if nf:
                print(f"[INFO] 次の通知は {fmt_day(nf)} {fmt_hm(nf)}。今回はここで終了します。")
            else:
                print("[INFO] 予定されている通知はありません。終了します。")
            break
        wait = (nf - t).total_seconds()
        print(f"[WAIT] {fmt_hm(nf)} の通知まで待機（残り {wait / 60:.1f} 分）")
        sleep(min(wait, 300))


def _stale(st, minutes):
    if not st["last_collect"]:
        return True
    return now() - datetime.fromisoformat(st["last_collect"]) > timedelta(minutes=minutes)


def main(argv=None):
    global DRY_RUN
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="送信せずログに出すだけ")
    p.add_argument("--collect", action="store_true", help="最初に必ず収集する")
    p.add_argument("--test", action="store_true", help="テスト通知を1通送る")
    p.add_argument("--no-wait", action="store_true", help="待機せずに終了する")
    p.add_argument("--lookahead", type=int, default=0)
    args = p.parse_args(argv)
    DRY_RUN = args.dry_run
    run(args)


if __name__ == "__main__":
    sys.exit(main())
