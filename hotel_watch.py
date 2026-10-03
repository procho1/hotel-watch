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

HERE = Path(__file__).parent
CFG = HERE / "config.json"
SEEN = HERE / "seen.json"
DEFAULT = {
    "telegram_token": "", "telegram_chat_id": "",
    "gmail_user": "", "gmail_app_password": "", "mail_to": "",   # 지메일 알림(선택). 텔레그램과 같이 써도 됨
    "keywords": ["호텔 양도", "숙박 양도", "숙소 양도"],
    "tiers": {"strong": 100000, "normal": 130000, "max": 150000},  # 1박 기준(원)
    "today_only": True,        # 등록일을 읽을 수 있을 때 오늘 올라온 것만
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
    "서울": "서울 강남 강북 강동 강서 서초 송파 마포 종로 용산 성수 성북 노원 동대문 서대문 은평 영등포 여의도 잠실 명동 홍대 이태원 신촌 건대 광진 금천 구로 동작 관악 양천 도봉 중랑".split(),
    "경기": "경기 수원 성남 판교 용인 고양 일산 부천 안양 하남 파주 가평 양평 이천 화성 동탄 평택 김포 광명 시흥 남양주 안산 의정부 구리 포천 오산 과천 군포 의왕 여주 양주 안성".split(),
    "인천": "인천 송도 영종 부평 청라 월미 계양 연수 강화 검단".split(),
}
OUT = "부산 해운대 광안리 제주 서귀포 강원 속초 강릉 평창 양양 춘천 경남 경북 경주 대구 울산 포항 거제 통영 여수 전주 전남 전북 충남 충북 대전 세종 천안 청주 목포 남해 안동".split()


def region_of(text):
    """서울/경기/인천 중 하나, 수도권 밖이면 'OUT', 글에 지역이 없으면 None."""
    score = {r: sum(text.count(k) for k in ks) for r, ks in REG.items()}
    best = max(score, key=score.get)
    if score[best]:
        return best
    return "OUT" if any(k in text for k in OUT) else None


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
    m = re.search(r"(\d+)\s*박", text)
    return int(m.group(1)) if m else None


def stay_date(text, today=None):
    today = today or dt.date.today()
    m = re.search(r"(\d{1,2})\s*[/월]\s*(\d{1,2})", text)
    if not m:
        return None
    try:
        d = dt.date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return d if d >= today else d.replace(year=today.year + 1)


def tier(per_night, t):
    if per_night <= t["strong"]:
        return "strong"
    if per_night <= t["normal"]:
        return "normal"
    if per_night <= t["max"]:
        return "ref"  # 13~15만: 소리 없는 참고 알림
    return None


def posted_today(ts, today=None):
    today = today or dt.date.today()
    if ts in (None, ""):
        return True  # 등록일을 못 읽으면 통과시킨다
    try:
        if isinstance(ts, (int, float)) or str(ts).isdigit():
            v = float(ts)
            v = v / 1000 if v > 1e11 else v
            return dt.date.fromtimestamp(v) == today
        return dt.date.fromisoformat(str(ts)[:10]) == today
    except Exception:
        return True


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
                                            "price": pr, "ts": None, "desc": a["t"]})
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


def lookup_price(ctx, title, date, n):
    """아고다 검색 화면에서 보이는 1박 가격을 읽어 본다. 못 읽으면 None (오차 큼)."""
    if not date:
        return None
    name = re.sub(r"(호텔|숙박|숙소|양도|\d+\s*박|\d+[/월]\s*\d+일?)", " ", title).strip()[:30]
    url = ("https://www.agoda.com/ko-kr/search?textToSearch=" + urllib.parse.quote(name) +
           f"&checkIn={date.isoformat()}&los={n or 1}")
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(6000)
        nums = [to_int(x) for x in re.findall(r"₩\s?([\d,]{5,})", page.inner_text("body"))]
        nums = [x for x in nums if x and 30000 <= x <= 2000000]
        return nums[0] if nums else None
    except Exception:
        return None
    finally:
        page.close()


# ---------- 판정 · 알림 ----------
def build_alert(it, cfg, ref=None):
    text = f'{it["title"]} {it["desc"]}'
    n = nights(text)
    pn = it["price"] // (n or 1)
    t = tier(pn, cfg["tiers"])
    if not t:
        return None
    icon = {"strong": "🔴 강한 알림", "normal": "🟡 일반 알림", "ref": "⚪ 참고"}[t]
    d = stay_date(text)
    lines = [f'{icon}  [{it["site"]} · {it.get("region") or "지역 미확인"}]', it["title"],
             f'1박 약 {pn:,}원 (글 가격 {it["price"]:,}원, ' + (f"{n}박)" if n else "박수 못 읽음, 1박으로 계산)")]
    if d:
        lines.append(f"이용일 추정: {d.isoformat()}")
    if ref:
        lines.append(f"아고다 조회 참고가 {ref:,}원 -> 약 {round((1 - pn / ref) * 100)}% 저렴 (오차 큼, 직접 확인)")
    else:
        lines.append("정가 자동 조회 실패: 아래 링크로 직접 확인")
        q = urllib.parse.quote(re.sub(r"양도|숙박|숙소", "", it["title"])[:30])
        lines.append(f"https://www.agoda.com/ko-kr/search?textToSearch={q}")
    lines.append(it["url"])
    return t, "\n".join(lines)


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


def send_mail(cfg, subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"] = cfg["gmail_user"], cfg["mail_to"] or cfg["gmail_user"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["gmail_user"], cfg["gmail_app_password"])
        s.send_message(msg)


def notify(cfg, items):
    """items = [(단계, 메시지)]. 텔레그램은 건건이, 지메일은 한 통으로 묶어서 보낸다."""
    use_tg = bool(cfg["telegram_token"])
    use_mail = bool(cfg["gmail_user"] and cfg["gmail_app_password"])
    if use_tg:
        for t, m in items:
            send(cfg, m, silent=(t == "ref"))
    if use_mail:
        send_mail(cfg, f"[호텔 양도] 새 매물 {len(items)}건",
                  "\n\n----------\n\n".join(m for _, m in items))
    if not use_tg and not use_mail:
        for t, m in items:
            print(m, "\n---")


def run_once(cfg):
    from playwright.sync_api import sync_playwright
    seen = set(json.loads(SEEN.read_text(encoding="utf-8"))) if SEEN.exists() else set()
    items = collect_all(cfg)
    new = [i for i in items if f'{i["site"]}:{i["id"]}' not in seen]
    alerts, skipped = [], 0
    for i in new:
        seen.add(f'{i["site"]}:{i["id"]}')
        if cfg["today_only"] and not posted_today(i["ts"]):
            continue
        text = f'{i["title"]} {i["desc"]}'
        t = tier(i["price"] // (nights(text) or 1), cfg["tiers"])
        reg = region_of(text)
        if not t or reg == "OUT" or (reg and reg not in cfg["regions"]):
            continue                      # 가격 밖이거나 수도권 밖 -> 완전 제외
        if reg is None and not cfg["allow_unknown_region"]:
            skipped += 1                  # 지역이 안 적힌 글은 알림 안 보냄
            continue
        alerts.append(dict(i, region=reg or "지역 미확인"))
    SEEN.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False), encoding="utf-8")
    sent = alerts[:cfg["max_alerts_per_run"]]
    if sent:
        with sync_playwright() as p:
            ctx = p.chromium.launch(headless=cfg["headless"]).new_context(locale="ko-KR")
            out = []
            for i in sent:
                text = f'{i["title"]} {i["desc"]}'
                ref = lookup_price(ctx, i["title"], stay_date(text), nights(text)) \
                    if cfg["price_lookup"] else None
                out.append(build_alert(i, cfg, ref))
            notify(cfg, out)
    stats = (f"수집 {len(items)}건 / 새 매물 {len(new)}건 / 조건 통과 {len(alerts)}건 "
             f"(알림 {len(sent)}건, 지역 미확인으로 제외 {skipped}건)")
    print(f"[{dt.datetime.now():%H:%M}] {stats}")
    return stats


def wait_minutes(cfg):
    h = dt.datetime.now().hour
    k = "night" if 1 <= h < 7 else "busy" if h in cfg["busy_hours"] else "normal"
    return cfg["interval_min"][k]


def selftest():
    c = DEFAULT
    assert won("하루 12만원 급처") == 120000 and won("95,000원") == 95000
    assert nights("2박 양도") == 2 and nights("양도합니다") is None
    assert tier(100000, c["tiers"]) == "strong" and tier(120000, c["tiers"]) == "normal"
    assert tier(140000, c["tiers"]) == "ref" and tier(160000, c["tiers"]) is None
    assert stay_date("10/12 체크인", dt.date(2026, 10, 3)) == dt.date(2026, 10, 12)
    assert posted_today(None) and not posted_today("2000-01-01")
    out = []
    walk({"data": {"list": [{"pid": 1, "name": "롯데호텔 2박 양도", "price": "240000"},
                            {"seq": "ab1", "title": "숙소 양도", "salePrice": 90000}]}}, out)
    assert len(out) == 2 and out[0]["price"] == 240000
    it = dict(out[0], site="번개장터", url="https://m.bunjang.co.kr/products/1", desc="10/12")
    t, msg = build_alert(it, c, ref=200000)
    assert t == "normal" and "40% 저렴" in msg  # 24만/2박 = 12만 -> 일반
    assert region_of("강남 롯데호텔 양도") == "서울" and region_of("송도 호텔 1박") == "인천"
    assert region_of("부산 해운대 호텔 양도") == "OUT" and region_of("호텔 양도합니다") is None
    assert region_of("수원 호텔, 서울역 근처") in ("경기", "서울")
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
                notify(config, [("x", "🔧 시험 실행 결과\n" + stats)])
        except Exception as e:
            print("[오류]", e)
            if "--ping" in sys.argv:
                notify(config, [("x", f"🔧 시험 실행 오류: {str(e)[:300]}")])
        if "--once" in sys.argv:
            break
        time.sleep(wait_minutes(config) * 60)
