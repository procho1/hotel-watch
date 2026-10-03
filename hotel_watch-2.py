#!/usr/bin/env python3
"""호텔·숙박 양도 매물 감시기 (번개장터 + 중고나라 -> 텔레그램 알림)

설치(1회):  pip install playwright   그리고   playwright install chromium
첫 실행:    python hotel_watch.py      -> config.json 생성됨. 텔레그램 토큰/챗ID 입력 후 다시 실행
계속 감시:  python hotel_watch.py
1회만 확인: python hotel_watch.py --once
오프라인 점검(인터넷 불필요): python hotel_watch.py --selftest
GitHub 자동 실행: hotel.yml 과 함께 사용 (토큰·비밀번호는 GitHub Secrets에 저장)
알림 방법: 텔레그램, 지메일 중 하나 또는 둘 다 (설정한 것만 사용)
"""
import datetime as dt
from html import escape as html_escape
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
HERE = Path(__file__).parent
CFG = HERE / "config.json"
SEEN = HERE / "seen.json"
DEFAULT = {
    "telegram_token": "", "telegram_chat_id": "",
    "gmail_user": "", "gmail_app_password": "", "mail_to": "",   # 지메일 알림(선택). 텔레그램과 같이 써도 됨
    "keywords": ["호텔 양도", "숙박 양도", "숙소 양도"],
    "tiers": {"strong": 100000, "normal": 130000, "max": 150000},  # 1박 기준(원)
    "detail_price_cap": 450000,  # 글 본문을 열어볼 최대 글 가격(몇 박인지 모르는 글 대비)
    "max_detail_per_run": 25,    # 한 번 실행에 열어볼 글 수
    "max_age_hours": 48,       # 올린 지 이 시간 넘은 글은 제외 (게시 시각을 읽을 수 있을 때만)
    "headless": False,         # 사이트가 막으면 False(창이 보이게) 유지
    "regions": ["서울", "경기", "인천"],   # 이 지역만 알림 (필수 필터)
    "max_alerts_per_run": 10,             # 한 번에 너무 많이 쏟아지는 것 방지
    "allow_unknown_region": False,         # 글에 지역이 안 적힌 매물: False면 알림 안 보내고 보드에만 "미확인"으로 저장
    "price_lookup": True,      # 아고다 자동 조회 시도 (실패해도 알림은 감)
    "interval_min": {"night": 150, "normal": 30, "busy": 20},
    "busy_hours": [12, 13, 18, 19, 20, 21, 22],
}
SITES = {
    "번개장터": {"url": "https://m.bunjang.co.kr/search/products?q={q}&order=date",
                 "item": "https://m.bunjang.co.kr/products/{id}", "pat": r"/products/(\d+)"},
    "중고나라": {"url": "https://web.joongna.com/search/{q}?sort=RECENT_SORT",
                 "item": "https://web.joongna.com/product/{id}", "pat": r"/product/(\w+)"},
}
TITLE = ("name", "title", "productName", "subject")
PRICE = ("price", "salePrice", "sellPrice")
IDK = ("pid", "seq", "productSeq", "id")
TIME = ("update_time", "updateTime", "sortDate", "regDate", "createdAt", "date")


REG = {
    "서울": "서울 강남 강북 강동 강서 서초 송파 마포 종로 용산 성수 성북 노원 동대문 서대문 은평 영등포 여의도 잠실 명동 홍대 이태원 신촌 건대 광진 금천 구로 동작 관악 양천 도봉 중랑 코엑스 롯데월드 삼성동 청담 압구정 논현 신사 한남 반포 서울숲 광화문 을지로 충무로 남대문 마곡 왕십리 상암 목동 신도림 합정 인사동 서울역".split(),
    "경기": "경기 수원 성남 판교 용인 고양 일산 부천 안양 하남 파주 가평 양평 이천 화성 동탄 평택 김포 광명 시흥 남양주 안산 의정부 구리 포천 오산 과천 군포 의왕 여주 양주 안성 분당 킨텍스 광교 스타필드 에버랜드 아난티".split(),
    "인천": "인천 송도 영종 부평 청라 월미 계양 연수 강화 검단 파라다이스시티 인천공항".split(),
}
OUT = "부산 해운대 광안리 제주 서귀포 강원 속초 강릉 평창 양양 춘천 경남 경북 경주 대구 울산 포항 거제 통영 여수 전주 전남 전북 충남 충북 대전 세종 천안 청주 목포 남해 안동".split()


def region_of(text):
    """서울/경기/인천 중 하나, 수도권 밖이면 'OUT', 글에 지역이 없으면 None."""
    score = {r: sum(text.count(k) for k in ks) for r, ks in REG.items()}
    best = max(score, key=score.get)
    if score[best]:
        return best
    return "OUT" if any(k in text for k in OUT) else None


def region_hint(text, region):
    """지역 판정의 근거가 된 단어(글에서 가장 먼저 나온 것)."""
    hits = [(text.find(k), k) for k in REG.get(region, []) if k in text]
    return min(hits)[1] if hits else ""


# ---------- 글 해석 ----------
def to_int(v):
    d = re.sub(r"[^\d]", "", str(v))
    return int(d) if d else None


def won(text):
    m = re.search(r"(\d[\d,]*)\s*원", text)
    if m:
        return to_int(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*만", text)
    return int(float(m.group(1)) * 10000) if m else None


def nights(text):
    """'2박' 표기, 없으면 '10.9~10일' 같은 날짜 범위에서 계산."""
    m = re.search(r"(\d+)\s*박", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?\s*[~\-–]\s*(?:(\d{1,2})\s*[./월]\s*)?(\d{1,2})\s*일?", text)
    if m and (m.group(3) is None or int(m.group(3)) == int(m.group(1))):
        n = int(m.group(4)) - int(m.group(2))
        if 0 < n <= 14:
            return n
    return None


def stay_date(text, today=None):
    """글 속 첫 날짜(10/9, 10.9, 10월 9일). 이미 지난 날짜는 지난 그대로 돌려준다."""
    today = today or dt.datetime.now(KST).date()
    for m in re.finditer(r"(\d{1,2})\s*[./월]\s*(\d{1,2})(?!\d)(?!\s*[만원%])", text):
        try:
            d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        if d < today and (today - d).days > 120:
            d = d.replace(year=today.year + 1)
        return d
    return None


def tier(per_night, t):
    if per_night <= t["strong"]:
        return "strong"
    if per_night <= t["normal"]:
        return "normal"
    if per_night <= t["max"]:
        return "ref"  # 13~15만: 소리 없는 참고 알림
    return None


UNIT = {"초": 1 / 60, "분": 1, "시간": 60, "일": 1440, "주": 10080, "개월": 43200, "달": 43200, "년": 525600}


def age_minutes(text):
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|달|년)\s*전", text)
    return int(int(m.group(1)) * UNIT[m.group(2)]) if m else None


def posted_at(it, now=None):
    """게시(끌올 포함) 시각. JSON의 시각 -> 목록의 'N시간 전' 순으로 찾고, 없으면 None."""
    now = now or dt.datetime.now(KST)
    ts = it.get("ts")
    if ts not in (None, ""):
        try:
            if isinstance(ts, (int, float)) or str(ts).isdigit():
                v = float(ts)
                return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, KST)
            d = dt.datetime.fromisoformat(str(ts))
            return d if d.tzinfo else d.replace(tzinfo=KST)
        except Exception:
            pass
    if it.get("age") is not None:
        return now - dt.timedelta(minutes=it["age"])
    return None


def fresh(it, cfg, now=None):
    now = now or dt.datetime.now(KST)
    p = posted_at(it, now)
    return p is None or (now - p) <= dt.timedelta(hours=cfg["max_age_hours"])


def fmt_posted(p, now=None):
    now = now or dt.datetime.now(KST)
    mins = max(0, int((now - p).total_seconds() // 60))
    rel = f"{mins}분 전" if mins < 60 else f"{mins // 60}시간 전" if mins < 1440 else f"{mins // 1440}일 전"
    return f"{p:%m/%d %H:%M} ({rel})"


def hotel_query(title):
    t = re.sub(r"\d{1,2}\s*[./월]\s*\d{1,2}\s*일?(?:\s*[~\-–]\s*(?:\d{1,2}\s*[./월]\s*)?\d{1,2}\s*일?)?", " ", title)
    t = re.sub(r"양도|숙박|숙소|급처|합니다|해요|성인\s*\d*|아이\s*\d*|\d+\s*박|[()\[\].,!~]", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:30] or title[:30]


GENERIC = set("양도 숙박 숙소 급처 합니다 해요 최저가 국내 서울 경기 인천 수도권 강남 강북 강동 강서 도심 시내 특급 프리미엄 럭셔리 신축 숙박권 객실 오늘 내일 주말 성인 아이 이용 가능".split())
JOSA = ("이라고", "이에요", "입니다", "이고", "이며", "에서", "이다", "에", "은", "는", "이", "가", "을", "를", "로", "의", "도", "만", "랑")


def hotel_name(text):
    """글에서 호텔 이름을 찾는다. 예: '잠실 스테이 호텔이라고' -> '잠실 스테이 호텔'. 못 찾으면 ''."""
    toks = re.findall(r"[가-힣A-Za-z0-9&]+", text)
    for idx, t in enumerate(toks):
        if "호텔" not in t and "리조트" not in t:
            continue
        key = "호텔" if "호텔" in t else "리조트"
        if t.endswith(key) and t != key:
            return t if len(t) - len(key) >= 2 else ""
        if t == key or t[len(key):] in JOSA:
            prev = [x for x in toks[max(0, idx - 2):idx] if x not in GENERIC and not re.fullmatch(r"\d+", x)]
            core = "".join(prev)
            if len(core) >= 2:
                return " ".join(prev + [key])
            continue
        if t.startswith(key):
            return t
    return ""


def agoda_url(q, date, n, adults=2):
    u = "https://www.agoda.com/ko-kr/search?textToSearch=" + urllib.parse.quote(q)
    if date:
        out = date + dt.timedelta(days=n or 1)
        u += f"&checkIn={date.isoformat()}&checkOut={out.isoformat()}&los={n or 1}&adults={adults}&rooms=1"
    return u


# ---------- 수집 ----------
def walk(o, out):
    """네트워크로 받은 JSON 안에서 '제목+가격+번호'가 있는 덩어리를 전부 찾는다."""
    if isinstance(o, dict):
        t = next((o[k] for k in TITLE if isinstance(o.get(k), str)), None)
        p = next((o[k] for k in PRICE if o.get(k) not in (None, "")), None)
        i = next((o[k] for k in IDK if o.get(k) not in (None, "")), None)
        if t and p is not None and i is not None and to_int(p):
            out.append({"id": str(i), "title": t, "price": to_int(p),
                        "ts": next((o[k] for k in TIME if k in o), None),
                        "desc": str(o.get("description") or "")})
        for v in o.values():
            walk(v, out)
    elif isinstance(o, list):
        for v in o:
            walk(v, out)


def collect_all(cfg):
    from playwright.sync_api import sync_playwright
    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg["headless"])
        ctx = browser.new_context(locale="ko-KR")
        for site, s in SITES.items():
            for kw in cfg["keywords"]:
                page = ctx.new_page()
                got = []

                def on_resp(r, got=got):
                    try:
                        if "json" in (r.headers.get("content-type") or ""):
                            walk(r.json(), got)
                    except Exception:
                        pass
                page.on("response", on_resp)
                try:
                    page.goto(s["url"].format(q=urllib.parse.quote(kw)),
                              wait_until="networkidle", timeout=45000)
                    for _ in range(3):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(1200)
                    if not got:  # 대비책: 화면의 링크에서 직접 읽기
                        for a in page.eval_on_selector_all(
                                "a", "e=>e.map(x=>({h:x.href,t:x.innerText}))"):
                            m = re.search(s["pat"], a["h"] or "")
                            pr = won(a["t"] or "")
                            if m and pr:
                                got.append({"id": m.group(1), "title": a["t"].split("\n")[0],
                                            "price": pr, "ts": None, "desc": a["t"],
                                            "age": age_minutes(a["t"] or "")})
                except Exception as e:
                    print(f"[경고] {site} '{kw}' 수집 실패: {e}")
                try:
                    title = page.title()
                except Exception:
                    title = ""
                print(f"  [{site}] '{kw}': {len(got)}건 (페이지 제목: {title[:30]!r})")
                if not got:
                    try:
                        print("    화면 첫 부분:", page.inner_text("body")[:150].replace("\n", " "))
                    except Exception:
                        pass
                for g in got:
                    g.update(site=site, url=s["item"].format(id=g["id"]))
                    items.append(g)
                page.close()
        browser.close()
    return items


def lookup_price(ctx, query, date, n):
    """아고다에서 호텔 이름 + 이용일 + 성인 2명으로 검색해 1박 가격을 읽는다. (가격 또는 None, 실패 이유)"""
    if not query or not date:
        return None, "호텔 이름이나 이용일을 글에서 못 찾음"
    page = ctx.new_page()
    try:
        page.goto(agoda_url(query, date, n), wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(7000)
        body = page.inner_text("body")
    except Exception as e:
        return None, f"아고다 접속 실패 ({str(e)[:40]})"
    finally:
        page.close()
    low = body.lower()
    if any(k in low for k in ("captcha", "access denied", "unusual traffic", "are you a robot")) or "로봇" in body:
        return None, "아고다가 자동 접속을 막음"
    lines = [ln for ln in body.splitlines() if "판매 완료" not in ln and "판매완료" not in ln]   # 팔린 객실 가격은 제외
    nums = [to_int(x) for x in re.findall(r"₩\s?([\d,]{5,})", "\n".join(lines))]
    nums = [x for x in nums if x and 30000 <= x <= 2000000]
    if not nums:
        return None, "아고다 화면에서 가격을 못 찾음"
    return nums[0], ""


def fetch_detail(ctx, it):
    """글을 직접 열어 본문, 게시 시각, 판매 상태를 읽는다. 읽으면 True."""
    page = ctx.new_page()
    try:
        page.goto(it["url"], wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(2500)
        body = page.inner_text("body")
    except Exception as e:
        print(f"  [상세] {it['url']} 열기 실패: {str(e)[:60]}")
        it["detail_ok"] = False
        return False
    finally:
        page.close()
    k = body.find(it["title"][:15])
    seg = body[k:k + 1500] if k >= 0 else body[:1500]
    head = body[:1200]
    it["detail_ok"] = len(seg.strip()) > 60
    it["desc"] = f'{it["desc"]} {seg} {head[:600]}'
    it["excerpt"] = re.sub(r"\s+", " ", seg[len(it["title"]):] if k >= 0 else seg).strip()[:160]
    a = age_minutes(head)
    if a is not None:
        it["age"] = a
    it["sold"] = "판매완료" in head
    it["reserved"] = "예약중" in head
    return it["detail_ok"]


TIERS = {"strong": ("🔴 강한 알림", "#d6322f", "#fdeceb"), "normal": ("🟡 일반 알림", "#a56a00", "#fff3d1"),
         "ref": ("⚪ 참고", "#5d6879", "#eceff4")}


def build_alert(it, cfg, ref=None):
    now = dt.datetime.now(KST)
    text = f'{it["title"]} {it["desc"]}'
    n = nights(text)
    pn = it["price"] // (n or 1)
    t = tier(pn, cfg["tiers"])
    if not t:
        return None
    label, color, bg = TIERS[t]
    reg = it.get("region") or "지역 미확인"
    hint = region_hint(text, reg)
    if hint:
        reg = f"{reg} (근거: {hint})"
    d = stay_date(text, now.date())
    p = posted_at(it, now)
    if n:
        price = f"1박 {pn:,}원"
        note = f'총 {it["price"]:,}원 / {n}박' if n > 1 else ""
    else:
        price = f'{it["price"]:,}원'
        note = "몇 박인지 글에 안 적혀 있어 1박으로 가정했습니다"
    when = f"이용일 {d.isoformat()}" if d else "이용일 글에서 못 읽음"
    posted = f"게시 {fmt_posted(p, now)} (끌올 포함일 수 있음)" if p else "게시 시각 읽지 못함"
    q_src = it["title"] if len(hotel_query(it["title"])) > 5 else f'{it["title"]} {it.get("excerpt") or ""}'
    ag = agoda_url(q_src, d, n)
    name = it.get("hotel") or hotel_name(text)
    ag = agoda_url(name or hotel_query(it["title"]), d, n)
    if ref:
        pct = round((1 - pn / ref) * 100)
        ref_line = (f"아고다 {ref:,}원 ({name}, 성인 2명 · 그 호텔 최저가 기준이라 방 종류는 다를 수 있음) -> "
                    + (f"{pct}% 저렴" if pct >= 0 else f"{-pct}% 비쌈"))
    else:
        why = it.get("ref_why") or ""
        ref_line = "아고다 가격 자동 조회 못 함" + (f" (이유: {why})" if why else "") + " -> 아래 버튼으로 직접 확인"
    extras = [f"추정 호텔: {name}" if name else "호텔 이름을 글에서 못 찾음"]
    if it.get("reserved"):
        extras.append("⚠ 예약중 표시가 있습니다")
    if it.get("detail_ok") is False:
        extras.append("⚠ 글 본문을 못 읽어 제목만으로 판단했습니다")
    if it.get("excerpt"):
        extras.append("글 내용: " + it["excerpt"])
    plain = "\n".join([f'{label}  [{it["site"]} · {reg}]', it["title"], price + (f"  ({note})" if note else ""), f"{when}  /  {posted}", ref_line, *extras,
                       "", "▶ 원문 보기", it["url"], "", "▶ 가격 확인 (아고다)", ag])
    e = html_escape
    btn = "display:block;text-align:center;text-decoration:none;font-weight:700;padding:12px;border-radius:8px;margin-top:10px;"
    html = (f'<div style="background:#fff;border:1px solid #dde2ea;border-radius:12px;padding:16px;margin:0 0 14px">'
            f'<div><span style="background:{bg};color:{color};font-weight:700;border-radius:999px;padding:2px 10px;font-size:13px">{label}</span> '
            f'<span style="color:#5d6879;font-size:13px">{e(it["site"])} · {e(reg)}</span></div>'
            f'<div style="font-weight:700;font-size:16px;margin:8px 0 2px">{e(it["title"])}</div>'
            f'<div style="font-size:22px;font-weight:800;margin:4px 0">{e(price)}</div>'
            f'<div style="color:#5d6879;font-size:13.5px">{e(note) + "<br>" if note else ""}{e(when)}<br>{e(posted)}<br>{e(ref_line)}{"".join("<br>" + e(x) for x in extras)}</div>'
            f'<a href="{e(it["url"])}" style="{btn}background:#16233a;color:#fff">원문 보기</a>'
            f'<a href="{e(ag)}" style="{btn}background:#eceff4;color:#16233a;border:1px solid #dde2ea">{e(("아고다에서 " + name + " 확인") if name else "아고다에서 가격 확인")}</a></div>')
    return {"tier": t, "pn": pn, "text": plain, "html": html}


def send(cfg, text, silent=False):
    if not cfg["telegram_token"]:
        print(text, "\n---")
        return
    body = json.dumps({"chat_id": cfg["telegram_chat_id"], "text": text,
                       "disable_notification": silent}).encode()
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{cfg["telegram_token"]}/sendMessage',
        body, {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def send_mail(cfg, subject, plain, html=None):
    msg = EmailMessage()
    msg["From"], msg["To"] = cfg["gmail_user"], cfg["mail_to"] or cfg["gmail_user"]
    msg["Subject"] = subject
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["gmail_user"], cfg["gmail_app_password"])
        s.send_message(msg)


def notify(cfg, items):
    """items = build_alert 결과 목록. 텔레그램은 건건이, 지메일은 한 통(HTML)으로 묶어서 보낸다."""
    use_tg = bool(cfg["telegram_token"])
    use_mail = bool(cfg["gmail_user"] and cfg["gmail_app_password"])
    for x in items:
        x.setdefault("html", f'<div style="white-space:pre-wrap;font-size:14px">{html_escape(x["text"])}</div>')
    if use_tg:
        for x in items:
            send(cfg, x["text"], silent=(x["tier"] == "ref"))
    if use_mail:
        cnt = {k: sum(1 for x in items if x["tier"] == k) for k in TIERS}
        sub = f"[호텔 양도] 새 매물 {len(items)}건" + "".join(
            f" {TIERS[k][0][0]}{v}" for k, v in cnt.items() if v)
        html = ('<div style="font-family:-apple-system,\'Apple SD Gothic Neo\',sans-serif;background:#f4f6f9;padding:14px">'
                + "".join(x["html"] for x in items) + "</div>")
        send_mail(cfg, sub, "\n\n----------\n\n".join(x["text"] for x in items), html)
    if not use_tg and not use_mail:
        for x in items:
            print(x["text"], "\n---")


def run_once(cfg):
    from playwright.sync_api import sync_playwright
    now = dt.datetime.now(KST)
    seen = set(json.loads(SEEN.read_text(encoding="utf-8"))) if SEEN.exists() else set()
    items = collect_all(cfg)
    new = [i for i in items if f'{i["site"]}:{i["id"]}' not in seen]
    cand, stale = [], 0
    for i in new:
        d = stay_date(f'{i["title"]} {i["desc"]}', now.date())
        if not fresh(i, cfg, now) or (d and d < now.date()):
            seen.add(f'{i["site"]}:{i["id"]}')
            stale += 1                    # 너무 오래됐거나 이용일이 이미 지난 글
        elif i["price"] > cfg["detail_price_cap"]:
            seen.add(f'{i["site"]}:{i["id"]}')   # 몇 박이어도 너무 비싼 글
        else:
            cand.append(i)
    cand.sort(key=lambda i: posted_at(i, now) or now, reverse=True)   # 최신 글부터
    cand = cand[:cfg["max_detail_per_run"]]       # 못 연 글은 다음 실행에서 다시 본다
    alerts, skipped, opened, read_ok, sold, ref_ok = [], 0, 0, 0, 0, 0
    sent = []
    if cand:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg["headless"])
            ctx = browser.new_context(locale="ko-KR")
            for i in cand:
                opened += 1
                read_ok += 1 if fetch_detail(ctx, i) else 0
            for i in cand:
                seen.add(f'{i["site"]}:{i["id"]}')
                if i.get("sold"):
                    sold += 1
                    continue
                text = f'{i["title"]} {i["desc"]}'
                d = stay_date(text, now.date())
                if d and d < now.date():
                    stale += 1
                    continue
                t = tier(i["price"] // (nights(text) or 1), cfg["tiers"])
                reg = region_of(text)
                if not t or reg == "OUT" or (reg and reg not in cfg["regions"]):
                    continue                  # 가격 밖이거나 수도권 밖 -> 완전 제외
                if reg is None and not cfg["allow_unknown_region"]:
                    skipped += 1              # 지역이 안 적힌 글은 알림 안 보냄
                    continue
                alerts.append(dict(i, region=reg or "지역 미확인"))
            order = list(TIERS)
            alerts.sort(key=lambda i: (order.index(tier(i["price"] // (nights(f'{i["title"]} {i["desc"]}') or 1), cfg["tiers"])), i["price"]))
            sent = alerts[:cfg["max_alerts_per_run"]]
            out = []
            for i in sent:
                text = f'{i["title"]} {i["desc"]}'
                i["hotel"] = hotel_name(text)
                ref, i["ref_why"] = lookup_price(ctx, i["hotel"], stay_date(text, now.date()), nights(text)) \
                    if cfg["price_lookup"] else (None, "")
                ref_ok += 1 if ref else 0
                out.append(build_alert(i, cfg, ref))
            if out:
                notify(cfg, out)
            browser.close()
    SEEN.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False), encoding="utf-8")
    stats = (f"수집 {len(items)}건 / 새 매물 {len(new)}건 / 오래됨·날짜 지남 제외 {stale}건 / "
             f"본문 열어봄 {opened}건 (읽음 {read_ok}건, 판매완료 제외 {sold}건) / "
             f"조건 통과 {len(alerts)}건 (알림 {len(sent)}건, 지역 미확인으로 제외 {skipped}건) / 아고다 가격 조회 성공 {ref_ok}/{len(sent)}건")
    print(f"[{now:%H:%M}] {stats}")
    return stats


def wait_minutes(cfg):
    h = dt.datetime.now().hour
    k = "night" if 1 <= h < 7 else "busy" if h in cfg["busy_hours"] else "normal"
    return cfg["interval_min"][k]


def selftest():
    c = DEFAULT
    today = dt.date(2026, 10, 3)
    assert won("하루 12만원 급처") == 120000 and won("95,000원") == 95000
    assert nights("2박 양도") == 2 and nights("양도합니다") is None
    assert nights("10.9~10일 서울 숙소") == 1 and nights("9.5 ~9.6 호텔") == 1 and nights("10/9~10/11") == 2
    assert tier(100000, c["tiers"]) == "strong" and tier(120000, c["tiers"]) == "normal"
    assert tier(140000, c["tiers"]) == "ref" and tier(160000, c["tiers"]) is None
    assert stay_date("10/12 체크인", today) == dt.date(2026, 10, 12)
    assert stay_date("10.9~10일", today) == dt.date(2026, 10, 9)
    assert stay_date("9.5~9.6 메이플레이스", today) < today and stay_date("12.5만원 양도", today) is None
    assert age_minutes("3시간 전") == 180 and age_minutes("2일 전") == 2880 and age_minutes("없음") is None
    now = dt.datetime(2026, 10, 3, 21, 0, tzinfo=KST)
    assert fresh({"age": 60}, c, now) and not fresh({"age": 60 * 24 * 5}, c, now) and fresh({}, c, now)
    assert posted_at({"ts": 1790000000}, now).tzinfo is not None
    assert hotel_query("10.9~10일 서울 숙소 양도(에어비앤비) 성인2. 아이 2") == "서울 에어비앤비"
    assert hotel_name("엑소 중콘 숙소양도 잠실 스테이 호텔이라고 써 있어") == "잠실 스테이 호텔"
    assert hotel_name("호텔 양도합니다") == "" and hotel_name("강남 호텔 1박 양도") == "" and hotel_name("호텔나루 9/26") == "호텔나루"
    assert hotel_name("롯데호텔 월드 2박") == "롯데호텔"
    out = []
    walk({"data": {"list": [{"pid": 1, "name": "롯데호텔 2박 양도", "price": "240000"},
                            {"seq": "ab1", "title": "숙소 양도", "salePrice": 90000}]}}, out)
    assert len(out) == 2 and out[0]["price"] == 240000
    assert region_of("강남 롯데호텔 양도") == "서울" and region_of("송도 호텔 1박") == "인천"
    assert region_of("부산 해운대 호텔 양도") == "OUT" and region_of("호텔 양도합니다") is None
    it = dict(out[0], site="번개장터", url="https://m.bunjang.co.kr/products/1", desc="10/12", age=120)
    a = build_alert(it, c, ref=200000)
    assert a["tier"] == "normal" and "40% 저렴" in a["text"] and "게시" in a["text"] and a["html"].count("<a href") == 2
    class FP:
        def __init__(s, body): s.b = body
        def goto(s, *a, **k): pass
        def wait_for_timeout(s, ms): pass
        def inner_text(s, sel): return s.b
        def close(s): pass

    class FC:
        def __init__(s, body): s.b = body
        def new_page(s): return FP(s.b)
    body = "홈 > 숙박\n3시간 전\n잠실 호텔 2박 양도\n10/20~10/22 체크인입니다. 서울 잠실 호텔이고 정가보다 싸게 양도해요. 직거래 가능합니다. " * 3
    d1 = {"title": "잠실 호텔 2박 양도", "desc": "", "price": 240000, "url": "u"}
    assert fetch_detail(FC(body), d1) and d1["age"] == 180 and not d1["sold"]
    assert nights(d1["title"] + d1["desc"]) == 2 and stay_date(d1["desc"], today) == dt.date(2026, 10, 20)
    d2 = {"title": "잠실 호텔 양도", "desc": "", "price": 100000, "url": "u"}
    assert fetch_detail(FC("판매완료\n잠실 호텔 양도\n" + "내용 " * 40), d2) and d2["sold"]
    sold_page = "잠실 스테이 호텔\n당사 객실 판매 완료(판매 완료가: ₩ 105,974)\n송파 - 도심까지 10.32 km\n₩ 278,018~"
    assert lookup_price(FC(sold_page), "잠실 스테이 호텔", today, 1) == (278018, "")
    assert lookup_price(FC("captcha"), "잠실 스테이 호텔", today, 1)[1] == "아고다가 자동 접속을 막음"
    assert lookup_price(FC("내용 없음"), "", today, 1)[0] is None
    d3 = {"title": "x 호텔", "desc": "", "price": 1, "url": "u"}
    assert not fetch_detail(FC("짧음"), d3) and d3["detail_ok"] is False
    assert region_of("롯데월드 근처 호텔 양도") == "서울" and region_of("파라다이스시티 숙박") == "인천"
    assert region_of("부산 호텔 양도 (서울 직거래 가능)") == "서울"
    assert region_hint("호텔 잠실 롯데월드", "서울") == "잠실"
    print("selftest 통과")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    ci = bool(os.environ.get("TELEGRAM_TOKEN") or os.environ.get("GMAIL_USER"))
    if CFG.exists():
        config = {**DEFAULT, **json.loads(CFG.read_text(encoding="utf-8"))}
    elif ci:
        config = dict(DEFAULT)
    else:
        CFG.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2), encoding="utf-8")
        print("config.json을 만들었습니다. 텔레그램 토큰/챗ID를 채우고 다시 실행하세요.")
        sys.exit()
    config["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", config["telegram_token"])
    config["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", config["telegram_chat_id"])
    config["gmail_user"] = os.environ.get("GMAIL_USER", config["gmail_user"])
    config["gmail_app_password"] = os.environ.get("GMAIL_APP_PASSWORD", config["gmail_app_password"])
    config["mail_to"] = os.environ.get("MAIL_TO", config["mail_to"])
    if os.environ.get("HEADLESS") == "1":
        config["headless"] = True
    while True:
        try:
            stats = run_once(config)
            if "--ping" in sys.argv:      # 수동 시험 실행: 결과 숫자를 텔레그램으로 보냄
                notify(config, [{"tier": "x", "text": "🔧 시험 실행 결과\n" + stats}])
        except Exception as e:
            print("[오류]", e)
            if "--ping" in sys.argv:
                notify(config, [{"tier": "x", "text": f"🔧 시험 실행 오류: {str(e)[:300]}"}])
        if "--once" in sys.argv:
            break
        time.sleep(wait_minutes(config) * 60)
