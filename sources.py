"""
LivePocket / TicketDive から「Falench.出演イベントの販売開始日時」を集める処理。

戻り値はどちらも次の形の dict のリスト:
    {
      "url": "...", "source": "LivePocket" | "TicketDive",
      "title": "...", "event_date": "2026/12/05(土)" など（表示用）,
      "event_day": "2026-12-05" または None（開催済み判定用）,
      "sales": [{"name": "券種名 or ''", "start": "2026-11-24T20:00:00+09:00", "text": "元の表記"}],
    }
"""
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja-JP,ja;q=0.9"}


# =====================================================================
# 日付・時刻の読み取り
# =====================================================================
DATE_PART = r'(?:(20\d{2})\s*(?:[/.\-]|年)\s*)?(\d{1,2})\s*(?:[/.\-]|月)\s*(\d{1,2})\s*日?'
WEEKDAY = r'(?:\s*[\(（][^\)）]{1,6}[\)）])?'
TIME_PART = r'(\d{1,2})\s*(?:[:：]\s*(\d{2})|時\s*(?:(\d{2})\s*分)?)'
DT_RE = re.compile(rf'{DATE_PART}{WEEKDAY}\s*(?:{TIME_PART})?')

SALES_LABELS = [
    "先着販売受付期間", "抽選販売受付期間", "一般販売受付期間", "販売受付期間", "申込受付期間",
    "チケット販売期間", "販売期間", "受付期間", "申込期間", "申し込み期間", "抽選受付期間",
    "先着受付期間", "発売期間", "販売日程", "受付日程", "販売開始日時", "販売開始",
    "受付開始日時", "受付開始", "発売開始", "一般発売", "発売日",
]
GENERIC_SALES_LABEL = re.compile(r'^[^\d\s]{0,10}(?:販売|受付|申込|申し込み|発売)[^\d\s]{0,6}(?:期間|開始日時|開始)')
DATE_LABELS = ["開催日時", "公演日時", "開催日", "公演日", "日時", "日程", "開催期間"]
CONT_RE = re.compile(r'^(?:[\(（]|\d{1,2}\s*[:：]\s*\d{2}|20\d{2}\s*[/.\-年]|[～〜~–—])')


def _guess_year(month, day, today):
    """年が書かれていない日付は、今日に一番近い年とみなす"""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_first_datetime(text, today=None):
    """
    文字列中の最初の日付(+時刻)を読む。
    戻り値: (datetime[JST] または None, 時刻が書かれていたか)
    """
    today = today or datetime.now(JST).date()
    for m in DT_RE.finditer(text or ""):
        y, mo, d, hh, mm1, mm2 = m.groups()
        mo, d = int(mo), int(d)
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        # 「10:00」のような時刻だけを日付と誤読しないよう、区切りが「:」のものは除外
        if re.match(r'^\d{1,2}\s*[:：]', m.group(0)):
            continue
        try:
            dd = date(int(y), mo, d) if y else _guess_year(mo, d, today)
        except ValueError:
            continue
        if dd is None:
            continue
        if hh is not None and int(hh) <= 24:
            h = int(hh)
            mi = int(mm1 or mm2 or 0)
            dt = datetime(dd.year, dd.month, dd.day, tzinfo=JST) + timedelta(hours=h, minutes=mi)
            return dt, True
        return datetime(dd.year, dd.month, dd.day, tzinfo=JST), False
    return None, False


def parse_any_datetime_value(v):
    """JSON内の日時値（ISO文字列 / UNIX秒・ミリ秒 / {seconds: ...}）を datetime[JST] にする"""
    try:
        if isinstance(v, dict):
            sec = v.get("seconds", v.get("_seconds"))
            return datetime.fromtimestamp(int(sec), JST) if sec is not None else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            if 1.5e9 < ts < 2.5e9:
                return datetime.fromtimestamp(ts, JST)
            return None
        if isinstance(v, str) and re.match(r'^\d{4}-\d{2}-\d{2}', v):
            s = v.strip().replace("Z", "+00:00")
            if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
                return None  # 日付だけ（時刻なし）は販売開始として扱わない
            dt = datetime.fromisoformat(s.replace(" ", "T", 1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            return dt.astimezone(JST)
    except Exception:
        return None
    return None


# =====================================================================
# ページのテキストから販売期間を拾う（両サイト共通）
# =====================================================================
def clean_lines(text):
    out = []
    for raw in (text or "").splitlines():
        line = re.sub(r'[ \t　\xa0]+', ' ', raw).strip()
        if line:
            out.append(line)
    return out


def _labeled_values(lines, labels):
    """「ラベル 値」「ラベル\\n値」の形から、日付を含む値を取り出す"""
    generic = labels is SALES_LABELS
    for i, line in enumerate(lines):
        cands_labels = list(labels)
        if generic:
            m = GENERIC_SALES_LABEL.match(line)
            if m and m.group(0) not in cands_labels:
                cands_labels.insert(0, m.group(0))
        for label in cands_labels:
            if not line.startswith(label):
                continue
            rest = line[len(label):].lstrip(" :：")
            cands = ([(rest, i)] if rest else []) + [(lines[j], j) for j in range(i + 1, min(i + 4, len(lines)))]
            for text, j in cands:
                if len(text) < 160 and DT_RE.search(text) and re.search(r'\d{1,2}\s*[/.\-月]\s*\d{1,2}', text):
                    parts = [text]
                    for k in range(j + 1, min(j + 4, len(lines))):
                        if len(lines[k]) < 40 and CONT_RE.match(lines[k]):
                            parts.append(lines[k])
                        else:
                            break
                    yield i, re.sub(r'\s+', ' ', " ".join(parts)).strip()
                    break
            break


LOTTERY_RE = re.compile(r'抽選|ロッテリー|lottery|エントリー受付', re.IGNORECASE)
FIRST_COME_RE = re.compile(r'先着|first[\s_\-]?come|firstcome|早い者勝ち', re.IGNORECASE)


def classify_sale(*texts):
    """販売方式: "lottery"（抽選） / "first_come"（先着） / "unknown"（不明）"""
    t = " ".join(x for x in texts if x)
    if LOTTERY_RE.search(t):
        return "lottery"
    if FIRST_COME_RE.search(t):
        return "first_come"
    return "unknown"


TICKET_NAME_HINT = re.compile(r'チケット|券|先行|一般|抽選|先着|前売|当日|VIP|S席|A席|整理番号|優先|入場|物販|特典|枠')


def sales_from_lines(lines, today=None):
    """テキスト行から [{"name","start","text","has_time"}] を作る"""
    results = []
    seen = set()
    for idx, value in _labeled_values(lines, SALES_LABELS):
        dt, has_time = parse_first_datetime(value, today)
        if not dt:
            continue
        # 直前の数行から券種名らしきものを探す
        name = ""
        for k in range(idx - 1, max(-1, idx - 4), -1):
            cand = lines[k]
            if len(cand) <= 60 and TICKET_NAME_HINT.search(cand) and not DT_RE.search(cand) \
                    and not any(cand.startswith(l) for l in SALES_LABELS):
                name = cand
                break
        key = (dt.isoformat(), name)
        if key in seen:
            continue
        seen.add(key)
        sale_type = classify_sale(lines[idx][:40], name)
        results.append({"name": name, "start": dt.isoformat(), "text": value,
                        "has_time": has_time, "type": sale_type})
    return results


def event_day_from_lines(lines, today=None):
    """開催日（最終日）と表示用文字列"""
    for _, value in _labeled_values(lines, DATE_LABELS):
        dates = []
        for m in DT_RE.finditer(value):
            dt, _ = parse_first_datetime(m.group(0), today)
            if dt:
                dates.append(dt.date())
        if dates:
            return max(dates), _short(value, 60)
    return None, ""


def _short(s, n):
    s = re.sub(r'\s+', ' ', s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def is_keyword_in(text, keyword):
    return re.search(re.escape(keyword).replace(r'\.', r'(?:\.|\b)'), text or "", re.IGNORECASE) is not None


# =====================================================================
# TicketDive
# =====================================================================
TD_START_KEY = re.compile(r'(?:sale|sales|reception|accept|apply|application|entry|lottery|selling|order).*start'
                          r'|start.*(?:sale|sales|reception|accept|apply|application|entry|lottery|selling|order)',
                          re.IGNORECASE)
TD_NAME_KEYS = ("name", "title", "ticketName", "ticket_name", "label", "displayName")


def td_sales_from_next_data(html):
    """Next.js の __NEXT_DATA__ から販売開始日時を拾う（キー名は推測で幅広く対応）"""
    sales = []
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return sales, []
    try:
        data = json.loads(tag.string)
    except Exception:
        return sales, []

    found_keys = set()

    def hints(obj):
        """dict の中から販売方式の手がかりを文字列で集める（例: isLottery=True, salesType="LOTTERY"）"""
        out = []
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            if isinstance(v, bool):
                if v and ("lottery" in kl or "抽選" in k):
                    out.append("lottery")
                if v and ("firstcome" in kl.replace("_", "") or "先着" in k):
                    out.append("先着")
            elif isinstance(v, str) and len(v) < 40 and any(w in kl for w in
                                                           ("type", "method", "kind", "format", "style")):
                out.append(v)
        return " ".join(out)

    def walk(obj, parent_hint=""):
        if isinstance(obj, dict):
            here = hints(obj)
            for k, v in obj.items():
                if isinstance(k, str) and TD_START_KEY.search(k):
                    dt = parse_any_datetime_value(v)
                    if dt:
                        found_keys.add(k)
                        name = next((str(obj[n]) for n in TD_NAME_KEYS
                                     if isinstance(obj.get(n), str) and obj.get(n).strip()), "")
                        sale_type = classify_sale(here, name, k if "lottery" in k.lower() else "") \
                            if (here or name or "lottery" in k.lower()) else "unknown"
                        if sale_type == "unknown":
                            sale_type = classify_sale(parent_hint)
                        sales.append({"name": _short(name, 60), "start": dt.isoformat(),
                                      "text": f"{k}={v}", "has_time": True, "type": sale_type})
            for v in obj.values():
                walk(v, (parent_hint + " " + here).strip()[-200:])
        elif isinstance(obj, list):
            for v in obj:
                walk(v, parent_hint)

    walk(data)
    uniq, seen = [], set()
    for s in sales:
        key = (s["start"], s["name"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq, sorted(found_keys)


def td_title(soup):
    for sel in [("meta", {"property": "og:title"})]:
        t = soup.find(*sel)
        if t and t.get("content"):
            return _clean_td_title(t["content"])
    h1 = soup.find("h1")
    if h1:
        return _clean_td_title(h1.get_text(" ", strip=True))
    return "イベント名称未設定"


def _clean_td_title(t):
    t = re.sub(r'\s+', ' ', t or "").strip()
    t = re.split(r'\s*[｜|]\s*TicketDive|\s+-\s+TicketDive', t)[0]
    return _short(t, 70) or "イベント名称未設定"


def td_list_event_urls(list_urls):
    urls = []
    for list_url in list_urls:
        try:
            res = requests.get(list_url, headers=HEADERS, timeout=30)
            res.raise_for_status()
        except Exception as e:
            print(f"[WARN] TicketDive一覧の取得失敗 ({list_url}): {e}")
            continue
        soup = BeautifulSoup(res.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/event/" not in href and "/events/" not in href:
                continue
            full = href if href.startswith("http") else f"https://ticketdive.com{href}"
            full = full.split("?")[0].split("#")[0].rstrip("/")
            if full not in urls:
                urls.append(full)
    print(f"[TicketDive] 一覧から {len(urls)} 件のイベントURL")
    return urls


def td_parse_event(url, html, body_text, keyword, today=None):
    soup = BeautifulSoup(html, "html.parser")
    text = body_text or soup.get_text("\n", strip=True)
    if not is_keyword_in(text + html[:200000], keyword):
        return None
    lines = clean_lines(text)
    sales, keys = td_sales_from_next_data(html)
    if keys:
        print(f"    [TicketDive] JSONの販売開始キー: {keys}")
    if not sales:
        sales = sales_from_lines(lines, today)
    event_day, event_date = event_day_from_lines(lines, today)
    return {
        "url": url, "source": "TicketDive", "title": td_title(soup),
        "event_date": event_date, "event_day": event_day.isoformat() if event_day else None,
        "sales": sales,
    }


def collect_ticketdive(cfg, get_rendered=None, today=None):
    """get_rendered(url) -> (html, body_text): Playwrightでの描画取得（requestsで取れない時の予備）"""
    keyword = cfg["keyword"]
    events = []
    for url in td_list_event_urls(cfg["ticketdive_list_urls"]):
        html, body = "", ""
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            if res.status_code == 200:
                html = res.text
        except Exception as e:
            print(f"[WARN] TicketDive取得失敗 ({url}): {e}")
        ev = td_parse_event(url, html, "", keyword, today) if html else None
        if (ev is None or not ev["sales"]) and get_rendered:
            try:
                html2, body2 = get_rendered(url)
                ev = td_parse_event(url, html2, body2, keyword, today) or ev
            except Exception as e:
                print(f"[WARN] TicketDive描画取得失敗 ({url}): {e}")
        if ev:
            print(f"  [TicketDive] {ev['title']} 販売開始 {len(ev['sales'])} 件 ({url})")
            events.append(ev)
        else:
            print(f"  [TicketDive] {keyword} の記載なし/取得失敗: {url}")
    return events


# =====================================================================
# LivePocket（Playwrightが必要）
# =====================================================================
LP_BLOCK_TYPES = {"image", "media", "font"}
LP_BLOCK_URLS = ("googletagmanager.com", "google-analytics.com", "doubleclick.net",
                 "googlesyndication.com", "facebook.net", "wovn.io", "clarity.ms", "hotjar.com",
                 "ads-twitter.com", "analytics.twitter.com", "adsrvr.org", "criteo")


def _lp_route(route):
    req = route.request
    if req.resource_type in LP_BLOCK_TYPES or any(k in req.url for k in LP_BLOCK_URLS):
        route.abort()
    else:
        route.continue_()


def lp_is_performing(soup, keyword):
    """本文（おすすめ・他イベント欄を除く）に出演者として載っているか"""
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s.select(".recommend, .other-events, .related-events, footer, header, #header, .sidebar, "
                      ".other-event-list, .recommend-event, .seller-event, [class*='recommend'], "
                      "[class*='other'], [id*='recommend'], [id*='other']"):
        t.decompose()
    title = s.find("h1") or s.find("title")
    if title and is_keyword_in(title.get_text(), keyword):
        return True
    main = s.select_one("#event-detail, .event-detail, .main-content, #main") or s
    for block in main.find_all(["div", "p", "li", "td", "span", "dd", "dt"]):
        bt = block.get_text(strip=True)
        if len(bt) < 300 and is_keyword_in(bt, keyword):
            return True
    return False


def lp_parse_event(url, html, body_text, keyword, today=None):
    """出演ありなら event dict、なしなら (None, 開催日)"""
    soup = BeautifulSoup(html, "html.parser")
    lines = clean_lines(body_text) or clean_lines(soup.get_text("\n", strip=True))
    event_day, event_date = event_day_from_lines(lines, today)
    if not lp_is_performing(soup, keyword):
        return None, event_day
    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else "Falench. 出演ライブ"
    title = title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
    sales = sales_from_lines(lines, today)
    if not sales:  # 非表示部分も含めて再挑戦
        s2 = BeautifulSoup(html, "html.parser")
        for t in s2(["script", "style", "noscript", "header", "footer"]):
            t.decompose()
        sales = sales_from_lines(clean_lines(s2.get_text("\n", strip=True)), today)
    return {
        "url": url, "source": "LivePocket", "title": _short(title, 70),
        "event_date": event_date, "event_day": event_day.isoformat() if event_day else None,
        "sales": sales,
    }, event_day


class Browser:
    """Playwrightを1つだけ起動して使い回す"""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 800}, locale="ja-JP",
            timezone_id="Asia/Tokyo", extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"})
        self._ctx.route("**/*", _lp_route)
        self.page = self._ctx.new_page()

    def get(self, url, wait_selector="h1", extra_wait_ms=0):
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            self.page.wait_for_selector(wait_selector, timeout=8000)
        except Exception:
            pass
        if extra_wait_ms:
            self.page.wait_for_timeout(extra_wait_ms)
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""
        return self.page.content(), body

    def close(self):
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass


def collect_livepocket(cfg, browser, excluded, known_urls, today=None):
    """
    excluded: {url: 開催日iso}  … 出演なしと判定済み（開催日が過ぎるまで再確認しない…ではなく、
              出演追加の可能性があるので開催前は毎回確認する。開催済みのみスキップ）
    known_urls: 既に出演ありと分かっているURL（一覧から消えていても再確認する）
    """
    keyword = cfg["keyword"]
    today = today or datetime.now(JST).date()
    cand = set(known_urls)
    for list_url in cfg["livepocket_list_urls"]:
        try:
            html, _ = browser.get(list_url, wait_selector='a[href*="/e/"], a[href*="/event/detail/"]')
        except Exception as e:
            print(f"[WARN] LivePocket一覧の取得失敗 ({list_url}): {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/e/" not in href and "/event/detail/" not in href:
                continue
            full = (href if href.startswith("http") else f"https://livepocket.jp{href}").split("?")[0]
            if "/event/search" in full:
                continue
            d = excluded.get(full)
            if d and d < (today - timedelta(days=1)).isoformat():
                continue
            cand.add(full)
    print(f"[LivePocket] 確認対象 {len(cand)} 件")

    events = []
    for url in sorted(cand):
        try:
            html, body = browser.get(url)
            ev, event_day = lp_parse_event(url, html, body, keyword, today)
        except Exception as e:
            print(f"[WARN] LivePocket取得失敗 ({url}): {e}")
            continue
        if ev:
            excluded.pop(url, None)
            print(f"  [LivePocket] {ev['title']} 販売開始 {len(ev['sales'])} 件 ({url})")
            events.append(ev)
        elif event_day:
            excluded[url] = event_day.isoformat()
    return events
