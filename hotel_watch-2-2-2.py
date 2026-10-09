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
    "max_age_hours": 24,       # 올린 지(끌올 포함) 이 시간 넘은 글은 제외. 최근 하루 안에 올라온 글만 본다
    "headless": False,         # 사이트가 막으면 False(창이 보이게) 유지
    "regions": ["서울", "경기", "인천"],   # 이 지역만 알림 (필수 필터)
    "max_alerts_per_run": 10,             # 한 번에 너무 많이 쏟아지는 것 방지
    "allow_unknown_region": False,         # 글에 지역이 없는 매물은 알림에서 뺀다 (메일 맨 아래 '제외한 글'에만 표시)
    "price_lookup": False,     # 아고다 자동 조회. 지금까지 한 번도 성공하지 못해 꺼 둠 (메일의 아고다 버튼은 그대로)
    "interval_min": {"night": 150, "normal": 30, "busy": 20},
    "busy_hours": [12, 13, 18, 19, 20, 21, 22],
}
SITES = {
    "번개장터": {"url": "https://m.bunjang.co.kr/search/products?q={q}&order=date",
                 "item": "https://m.bunjang.co.kr/products/{id}", "pat": r"/products/(\d+)"},
    "중고나라": {"url": "https://web.joongna.com/search/{q}?sort=RECENT_SORT",
                 "item": "https://web.joongna.com/product/{id}", "pat": r"/product/(\w+)",
                 "dom_first": True, "sort_click": "최신순"},
}
TITLE = ("name", "title", "productName", "subject")
PRICE = ("price", "salePrice", "sellPrice")
IDK = ("pid", "seq", "productSeq", "id")
TIME = ("update_time", "updateTime", "sortDate", "regDate", "createdAt", "date")


REG = {
    "서울": "서울 강남 강북 강동 강서 서초 송파 마포 종로 용산 성수 성북 노원 동대문 서대문 은평 영등포 여의도 잠실 명동 홍대 이태원 신촌 건대 광진 금천 구로 동작 관악 양천 도봉 중랑 코엑스 롯데월드 삼성동 청담 압구정 논현 신사 한남 반포 서울숲 광화문 을지로 충무로 남대문 마곡 왕십리 상암 목동 신도림 합정 인사동 서울역 "
            "선릉 역삼 양재 사당 방배 신림 천호 석촌 문정 가락 수서 화곡 발산 김포공항 당산 문래 노량진 이촌 회현 혜화 대학로 안국 북촌 익선 삼청 종각 연남 망원 서교 연신내 뚝섬 청량리".split(),
    "경기": "경기 수원 성남 판교 용인 고양 일산 부천 안양 하남 파주 가평 양평 이천 화성 동탄 평택 김포 광명 시흥 남양주 안산 의정부 구리 포천 오산 과천 군포 의왕 여주 양주 안성 분당 킨텍스 광교 스타필드 에버랜드 "
            "영통 기흥 평촌 범계 인덕원 야탑 헤이리 운정 병점 송탄 구래 철산 배곧 산본 동두천 연천 대부도 제부도".split(),
    "인천": "인천 송도 영종 부평 청라 월미 계양 연수구 강화 검단 파라다이스시티 인천공항 인스파이어 운서 을왕리 구월동 주안 소래".split(),
}
# 다른 낱말 속에 섞여 나오는 지역 이름은 그 경우를 뺀다 (예: '성수기'의 성수, '경기장'의 경기)
REG_NOT = {"성수": "기", "경기": "장|가|를|는|에|\\s?관람|\\s?티켓|\\s?직관", "신사": "숙녀", "강화": "유리|필름", "이천": "\\s?원", "화성": "인",
           "고양": "이", "안성": "맞춤", "주안": "점", "망원": "경|렌즈", "대만": "족"}
# 본문에서는 뜻이 여러 가지라 믿기 어려운 낱말 (제목에 있을 때만 지역으로 본다)
BODY_RISKY = set("동작 양주 이천 여주 오산 강화 구리 화성 신사 발산 가락 전주 진주 진도 부여 고성 대전 대구 영도 남포 런던 발리".split())
OUT = ("부산 해운대 광안리 영도 남포 제주 서귀포 애월 강원 속초 강릉 평창 양양 춘천 원주 횡성 홍천 정선 삼척 태백 영월 철원 화천 양구 고성 "
       "경남 경북 경주 대구 울산 포항 거제 통영 창원 마산 진해 김해 밀양 진주 남해 안동 구미 김천 문경 울진 영덕 청송 울릉 "
       "전남 전북 전주 군산 익산 남원 무주 부안 변산 여수 순천 광양 목포 나주 담양 완도 진도 신안 자은도 광주광역시 "
       "충남 충북 대전 세종시 천안 아산 청주 충주 제천 단양 부여 보령 대천 태안 안면도 서산 당진 "
       "쏠비치 휘닉스 용평 알펜시아 하이원 비발디 오션월드 델피노 설악").split()
ABROAD = ("일본 도쿄 오사카 후쿠오카 하카타 교토 난바 텐진 나카스 신주쿠 신오쿠보 시부야 롯폰기 이케부쿠로 우에노 아사쿠사 긴자 삿포로 오키나와 나고야 고베 유후인 벳푸 가고시마 "
          "홍콩 마카오 대만 타이베이 타이페이 방콕 파타야 푸켓 치앙마이 다낭 나트랑 호치민 하노이 푸꾸옥 발리 보홀 마닐라 보라카이 사이판 싱가포르 쿠알라 코타키나발루 말레이시아 "
          "런던 바르셀로나 뉴욕 라스베가스 하와이 시드니 상하이 베이징 칭다오 베트남 태국 필리핀 "
          "tokyo osaka kyoto fukuoka hakata bangkok").split()


def _first(text, words, skip=()):
    """글에서 가장 먼저 나오는 낱말의 (위치, 낱말). 같은 위치면 긴 낱말. 없으면 None."""
    low = text.lower()
    best = None
    for w in words:
        if w in skip:
            continue
        pat = re.escape(w) + (f"(?!{REG_NOT[w]})" if w in REG_NOT else "")
        m = re.search(pat, low)
        if m and (best is None or (m.start(), -len(w)) < (best[0], -len(best[1]))):
            best = (m.start(), w)
    return best


def _region_in(text, skip=()):
    """한 덩어리 글에서 (지역, 근거 낱말). 수도권 밖 낱말이 먼저 나오면 'OUT'."""
    hits = [(h, r) for r, ks in REG.items() for h in [_first(text, ks, skip)] if h]
    out = _first(text, OUT + ABROAD, skip)
    if not out:
        m = re.search(r"(?<![가-힣])(괌|파리)(?![가-힣])", text)   # 짧아서 다른 낱말에 잘 섞이는 해외 지명
        out = (m.start(), m.group(1)) if m else None
    inn = min(hits, key=lambda x: (x[0][0], -len(x[0][1]))) if hits else None
    if inn and out:
        return ("OUT", out[1]) if (out[0], -len(out[1])) < (inn[0][0], -len(inn[0][1])) else (inn[1], inn[0][1])
    if out:
        return "OUT", out[1]
    if inn:
        return inn[1], inn[0][1]
    return None, ""


def region_of(title, core=""):
    """호텔 위치 기준 지역. (서울/경기/인천/'OUT'/None, 근거 낱말).
    제목을 먼저 보고, 제목에 지역이 없을 때만 본문 설명을 본다. 판매자 동네나 직거래 장소는 보지 않는다."""
    reg, hint = _region_in(title)
    if reg:
        return reg, hint
    body = re.sub(r"[^.\n!?]*(직거래|직거|택배|반택|끼택)[^.\n!?]*", " ", core)   # 거래 장소를 말하는 문장은 뺀다
    return _region_in(body, BODY_RISKY)


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
    m = re.search(r"(?<!\d)(\d{1,2})\s*박", text)
    if m and 0 < int(m.group(1)) <= 30:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?\s*(?:\([월화수목금토일]\))?\s*[~\-–]\s*(?:(\d{1,2})\s*[./월]\s*)?(\d{1,2})\s*일?", text)
    if m and (m.group(3) is None or int(m.group(3)) == int(m.group(1))):
        n = int(m.group(4)) - int(m.group(2))
        if 0 < n <= 14:
            return n
    return None


def _mkdate(y, mo, d):
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def stay_dates(text, today=None, strict=False):
    """글 속 이용 날짜를 나온 순서대로 전부. 못 찾으면 빈 목록 (가짜 날짜를 만들지 않는다).
    strict=True(본문용)면 '4.9' 같은 평점·가격 숫자를 날짜로 착각하지 않도록 '10월 9일', '10/9', '2026.10.9'만 읽는다.
    연도가 없는 날짜가 8달 넘게 지났으면 내년으로 보고, 그보다 덜 지났으면 지난 날짜(오래된 글)로 본다."""
    today = today or dt.datetime.now(KST).date()
    found = []
    full = r"(?<!\d)(?:20)?(2\d)\s*[./년]\s*(\d{1,2})\s*[./월]\s*(\d{1,2})(?!\d)"       # 2026.10.09, 26.11.24, 27년1월11일
    for m in re.finditer(full, text):
        d = _mkdate(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    rest = re.sub(full, lambda m: " " * len(m.group(0)), text)
    pat = r"(?<![\d.,+])(\d{1,2})\s*(?:/|월\s*)\s*(\d{1,2})(?!\d)(?!\s*[만원%점])" if strict else \
          r"(?<![\d.,+])(\d{1,2})\s*(?:[./]|월\s*)\s*(\d{1,2})(?!\d)(?!\s*[만원%점])"
    for m in re.finditer(pat, rest):
        d = _mkdate(today.year, int(m.group(1)), int(m.group(2)))
        if not d:
            continue
        if d < today and (today - d).days > 240:
            d = d.replace(year=today.year + 1)
        found.append((m.start(), d))
    return [d for _, d in sorted(found)]


def stay_date(text, today=None, strict=False):
    """글 속 이용 날짜 하나. 아직 안 지난 날짜가 있으면 그중 처음 것, 전부 지났으면 마지막 날짜."""
    today = today or dt.datetime.now(KST).date()
    ds = stay_dates(text, today, strict)
    return next((d for d in ds if d >= today), ds[-1] if ds else None)


def use_by(meta):
    """번개장터 글 정보의 '사용기한 2026.10.12' 날짜. 없거나 '표시 없음'이면 None."""
    m = re.search(r"사용기한\s*(20\d\d)\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})", meta or "")
    return _mkdate(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


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


STAY_ANY = r"호텔|리조트|펜션|모텔|스테이|숙박|숙소|레지던스|게스트하우스|에어비앤비|풀빌라"
STAY_STRONG = r"체크인|체크아웃|객실|투숙|\d\s*박|조식|숙박권|입실|퇴실"
# 제목에 있으면 숙박이 아니라 물건(굿즈) 글로 본다
GOODS = (r"포카|포토카드|트레카|카드|키링|아크릴|스탠드|뱃지|배지|굿즈|인형|누이|피규어|아트북|엽서|콜라보|콜카|가챠|쿠지|코롯토|코스터|"
         r"유니폼|앨범|포스터|스티커|키트|텀블러|베개|이불|어메니티|어매니티|디퓨저|수건|샤워가운|배스로브|교재|전공책|해즈빈|호텔틴|델루나|프리마베라")
BUY = r"삽니다|구합니다|구해요|구함|양도\s*받(?!으실)|구매합니다|매입"       # 파는 글이 아니라 구하는 글
SHIP = r"반택|끼택|편택|반값택배|준등기"                             # 물건 배송 이야기 = 굿즈


def is_stay(title, text):
    """요청하신 규칙: '양도'가 '호텔'·'숙박'·'숙소' 중 하나와 함께 있어야 하고, 굿즈·구매 글이 아니어야 한다."""
    if re.search(BUY, title) or re.search(GOODS, title.lower()):
        return False
    return "양도" in text and bool(re.search(r"호텔|숙박|숙소", text))


def judge(it, cfg, today):
    """글 하나의 판정. 'alert'면 알림 대상, 아니면 제외 이유.
    it에 n(박수), pn(1박 가격), tier, date(이용일), region, hint(지역 근거)를 채운다."""
    title, core, meta = it["title"], it.get("core") or "", it.get("meta") or ""
    text = f"{title}\n{core}"
    it.update(n=None, pn=None, tier=None, date=None, region=None, hint="")
    if re.search(BUY, title):
        return "buy"
    if not is_stay(title, text):
        return "goods" if re.search(GOODS, title.lower()) else "not_stay"
    strong = bool(re.search(STAY_STRONG, text)) or "사용기한" in meta
    if not strong and ("상품상태" in meta or re.search(SHIP, core)):
        return "goods"          # 번개장터에서 '상품상태'가 적힌 글은 물건, 숙박 글은 '사용기한'이 적혀 있다
    n = nights(text)
    pn = it["price"] // (n or 1)
    t = tier(pn, cfg["tiers"])
    d = stay_date(title, today) or stay_date(core, today, strict=True)
    exp = use_by(meta)
    it.update(n=n, pn=pn, tier=t, date=d or exp)
    if (d and d < today) or (exp and exp < today):
        return "past"
    if not t:
        return "price"
    reg, hint = region_of(title, core)
    it.update(region=reg, hint=hint)
    if reg is None:
        return "region_unknown"
    if reg == "OUT" or reg not in cfg["regions"]:
        return "region_out"
    return "alert"


def split_detail(rest):
    """글 본문(제목 뒤)을 '글 정보(가격·상품상태·사용기한·수량)'와 '판매자가 쓴 설명'으로 나눈다.
    설명 뒤의 배송비·상점 이름·후기는 버린다. 지역과 날짜는 설명에서만 읽기 위해서다."""
    m = re.search(r"수량\s*\d+\s*개", rest)
    if m:
        meta, core = rest[:m.end()], rest[m.end():]
    else:
        j = rest.find("상품 정보")
        meta, core = (rest[:j], rest[j + 5:]) if j >= 0 else ("", rest)
    ends = [j for j in (core.find(k) for k in ("배송비", "구매하기", "상점정보", "결제 혜택")) if j > 10]
    if ends:
        core = core[:min(ends)]
    return meta, core


def parse_tile(href, text, pat, leaves=None):
    """목록의 글 한 칸(링크 + 글자)에서 번호, 제목, 가격, 'N시간 전'을 읽는다."""
    m = re.search(pat, href or "")
    if leaves:
        text = "\n".join(leaves)           # 칸 안의 글자를 하나씩 따로 읽어 '찜 3 채팅 1 2시간 전'이 붙지 않게 한다
    if not m or "판매완료" in (text or ""):
        return None
    lines = [x.strip() for x in (text or "").split("\n") if x.strip()]
    price_line = next((x for x in lines if re.fullmatch(r"[\d,]+원", x)), None)
    pr = to_int(price_line) if price_line else won(text or "")
    if not pr or not lines:
        return None
    return {"id": m.group(1), "title": lines[0], "price": pr, "ts": None, "desc": text, "age": age_minutes(text or "")}


DIAG = []   # 사이트·검색어별 수집 결과 (시험 실행 메일에 그대로 실린다)


def next_data(html):
    """페이지 안에 들어 있는 목록 데이터(__NEXT_DATA__)에서 글을 찾는다. 화면에서 못 읽었을 때의 예비 방법."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    out = []
    if m:
        try:
            walk(json.loads(m.group(1)), out)
        except Exception:
            pass
    return out


def collect_all(cfg):
    from playwright.sync_api import sync_playwright
    items = []
    DIAG.clear()
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
                    if s.get("sort_click"):      # 화면의 '최신순' 탭을 눌러 최신 글부터 보게 한다
                        try:
                            page.get_by_text(s["sort_click"], exact=True).first.click(timeout=3000)
                            page.wait_for_timeout(1500)
                        except Exception:
                            pass
                    for _ in range(3):
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(1200)
                    dom, seen_ids = [], set()
                    for a in page.eval_on_selector_all("a", "e=>e.map(x=>({h:x.href,t:x.innerText,l:Array.from(x.querySelectorAll('*')).filter(c=>!c.children.length).map(c=>c.textContent.trim()).filter(Boolean)}))"):
                        d = parse_tile(a["h"], a["t"], s["pat"], a.get("l"))
                        if d and d["id"] not in seen_ids:
                            seen_ids.add(d["id"])
                            dom.append(d)
                    if dom and (s.get("dom_first") or not got):
                        got[:] = dom             # 화면에서 읽은 목록을 우선 사용
                except Exception as e:
                    print(f"[경고] {site} '{kw}' 수집 실패: {e}")
                try:
                    title = page.title()
                except Exception:
                    title = ""
                how = "화면"
                if not got:                      # 화면에서 못 읽으면 페이지에 들어 있는 목록 데이터를 본다
                    try:
                        got[:] = next_data(page.content())
                        how = "페이지 데이터"
                    except Exception:
                        pass
                uniq = {}
                for g in got:
                    uniq.setdefault(str(g["id"]), g)
                got[:] = list(uniq.values())
                print(f"  [{site}] '{kw}': {len(got)}건 (페이지 제목: {title[:30]!r})")
                line = f"{site} '{kw}': {len(got)}건" + (f" ({how})" if got else "")
                if not got:
                    try:
                        first = re.sub(r"\s+", " ", page.inner_text("body"))[:120]
                    except Exception:
                        first = "(화면을 못 읽음)"
                    print("    화면 첫 부분:", first)
                    line += f" / 페이지 제목: {title[:30]} / 화면 첫 부분: {first}"
                DIAG.append(line)
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
    marks = ("이 상품과 비슷해요", "이 상품을 추천해요", "비슷한 상품", "연관 상품", "연관상품", "추천 상품", "판매자의 다른", "상점의 다른",
             "결제 혜택", "CONTACT", "(주)중고나라", "고객센터운영시간", "셀러 회원 신청")

    def cut(t):                              # 그 아래 '비슷한 상품' 목록의 지역·호텔 이름이 섞이지 않게 자른다
        for mark in marks:
            j = t.find(mark)
            if j > 0:
                t = t[:j]
        return t
    k = body.find(it["title"][:15])
    seg = cut(body[k:k + 2500] if k >= 0 else body[:2500])
    head = cut(body[:1200])
    it["detail_ok"] = len(seg.strip()) > 60
    it["desc"] = f'{it["desc"]} {seg} {head[:600]}'
    it["meta"], it["core"] = split_detail(seg[len(it["title"]):] if k >= 0 else seg)
    it["excerpt"] = re.sub(r"\s+", " ", it["core"]).strip()[:160]
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
    if "core" not in it:
        it["core"] = it.get("desc") or ""
    text = f'{it["title"]} {it["core"]}'
    judge(it, cfg, now.date())
    n, pn, t, d = it["n"], it["pn"], it["tier"], it["date"]
    if not t:
        return None
    label, color, bg = TIERS[t]
    reg = it.get("region") or "지역 미확인"
    if it.get("hint"):
        reg = f'{reg} (근거: {it["hint"]})'
    p = posted_at(it, now)
    if n:
        price = f"1박 {pn:,}원"
        note = f'총 {it["price"]:,}원 / {n}박' if n > 1 else ""
    else:
        price = f'{it["price"]:,}원'
        note = "몇 박인지 글에 안 적혀 있어 1박으로 가정했습니다"
    when = f"이용일 {d.isoformat()}" if d else "이용일 글에서 못 읽음"
    posted = f"게시 {fmt_posted(p, now)} (끌올 포함일 수 있음)" if p else "게시 시각 읽지 못함"
    name = it.get("hotel") or hotel_name(text)
    ag = agoda_url(name or hotel_query(it["title"]), d, n)
    if ref:
        pct = round((1 - pn / ref) * 100)
        ref_line = (f"아고다 {ref:,}원 ({name}, 성인 2명 · 그 호텔 최저가 기준이라 방 종류는 다를 수 있음) -> "
                    + (f"{pct}% 저렴" if pct >= 0 else f"{-pct}% 비쌈"))
    else:
        why = it.get("ref_why") or ""
        ref_line = ("아고다 가격 자동 조회 못 함" + (f" (이유: {why})" if why else "") + " -> 아래 버튼으로 직접 확인") \
            if cfg.get("price_lookup") else "정가는 아래 아고다 버튼으로 직접 확인"
    extras = [f"추정 호텔: {name}" if name else "호텔 이름을 글에서 못 찾음"]
    if reg.startswith("지역 미확인"):
        extras.insert(0, "⚠ 글에서 서울·경기·인천 단어를 못 찾았습니다. 수도권인지 직접 확인하세요")
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


def skipped_note(items):
    """지역을 못 찾아 알림에서 뺀 글을 메일 아래에 보여준다 (직접 확인용)."""
    if not items:
        return ""
    lines = ["", f"※ 지역을 못 찾아 제외한 글 {len(items)}건 (필요하면 직접 확인하세요)"]
    for i in items[:6]:
        lines.append(f'- {i["title"]} / {i["price"]:,}원 / {i["url"]}')
        lines.append("  읽은 내용: " + (i.get("excerpt") or "(본문을 못 읽음)")[:100])
    return "\n".join(lines)


def notify(cfg, items, footer=""):
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
        if all(x["tier"] == "x" for x in items):
            sub = "[호텔 양도] 시험 실행 결과"
        html = ('<div style="font-family:-apple-system,\'Apple SD Gothic Neo\',sans-serif;background:#f4f6f9;padding:14px">'
                + "".join(x["html"] for x in items) + "</div>")
        if footer:
            html = html + f'<div style="white-space:pre-wrap;font-size:13px;color:#5d6879;padding:0 14px 14px">{html_escape(footer)}</div>'
        send_mail(cfg, sub, "\n\n----------\n\n".join(x["text"] for x in items) + "\n" + footer, html)
    if not use_tg and not use_mail:
        for x in items:
            print(x["text"], "\n---")


REASONS = {"buy": "구하는 글", "goods": "굿즈·물건", "not_stay": "숙박 양도 글 아님", "past": "이용일 지남", "price": "가격 조건 밖",
           "region_out": "수도권 밖", "region_unknown": "지역 미확인", "old": "올린 지 하루 넘음", "sold": "판매완료"}


def run_once(cfg):
    from playwright.sync_api import sync_playwright
    now = dt.datetime.now(KST)
    today = now.date()
    seen = set(json.loads(SEEN.read_text(encoding="utf-8") or "[]")) if SEEN.exists() else set()
    items = collect_all(cfg)
    new, dup = {}, 0
    for i in items:                       # 세 검색어에 같은 글이 걸리므로 글 번호로 한 번만 남긴다
        key = f'{i["site"]}:{i["id"]}'
        if key in seen:
            continue
        if key in new:
            dup += 1
        else:
            new[key] = i
    count = {k: 0 for k in REASONS}
    cand = []
    for key, i in new.items():
        d = stay_date(i["title"], today)
        why = ("old" if not fresh(i, cfg, now) else "past" if d and d < today else "buy" if re.search(BUY, i["title"])
               else "goods" if re.search(GOODS, i["title"].lower()) else "not_stay" if not re.search(STAY_ANY, i["title"])
               else "price" if i["price"] > cfg["detail_price_cap"] else "")
        if why:
            seen.add(key)                 # 제목만 봐도 아닌 글은 본문을 열지 않는다
            count[why] += 1
        else:
            cand.append(i)
    cand.sort(key=lambda i: posted_at(i, now) or now, reverse=True)   # 최신 글부터
    cand = cand[:cfg["max_detail_per_run"]]       # 못 연 글은 다음 실행에서 다시 본다
    alerts, skip_items, sent, opened, read_ok, ref_ok = [], [], [], 0, 0, 0
    if cand:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg["headless"])
            ctx = browser.new_context(locale="ko-KR")
            for i in cand:
                opened += 1
                if fetch_detail(ctx, i):
                    read_ok += 1
                else:
                    i["meta"], i["core"] = "", ""      # 본문을 못 읽으면 제목만으로 판단한다
            for i in cand:
                seen.add(f'{i["site"]}:{i["id"]}')
                why = "sold" if i.get("sold") else "old" if not fresh(i, cfg, now) else judge(i, cfg, today)
                if why == "alert":
                    alerts.append(i)
                    continue
                count[why] += 1
                if why == "region_unknown":
                    if cfg["allow_unknown_region"]:
                        count[why] -= 1
                        alerts.append(dict(i, region="지역 미확인"))
                    else:
                        skip_items.append(i)   # 메일 맨 아래 '제외한 글'로만 보여준다
            order = list(TIERS)
            alerts.sort(key=lambda i: (order.index(i["tier"]), i["pn"]))
            sent = alerts[:cfg["max_alerts_per_run"]]
            out = []
            for i in sent:
                i["hotel"] = hotel_name(f'{i["title"]} {i["core"]}')
                ref, i["ref_why"] = lookup_price(ctx, i["hotel"], i["date"], i["n"]) if cfg["price_lookup"] else (None, "")
                ref_ok += 1 if ref else 0
                a = build_alert(i, cfg, ref)
                if a:
                    out.append(a)
            if out:
                notify(cfg, out, skipped_note(skip_items))
            browser.close()
    SEEN.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False), encoding="utf-8")
    by_site = " ".join(f"{k} {sum(1 for x in items if x['site'] == k)}건" for k in SITES)
    drop = ", ".join(f"{REASONS[k]} {v}건" for k, v in count.items() if v) or "없음"
    stats = (f"수집 {len(items)}건 (사이트별: {by_site}, 검색어 사이 중복 {dup}건) / 처음 보는 글 {len(new)}건 / 본문 열어봄 {opened}건 (읽음 {read_ok}건)\n"
             f"제외: {drop}\n알림 {len(sent)}건 보냄 (대상 {len(alerts)}건)"
             + (f" / 아고다 가격 조회 성공 {ref_ok}/{len(sent)}건" if cfg["price_lookup"] else "")
             + "\n\n[사이트·검색어별 수집]\n" + "\n".join(DIAG))
    print(f"[{now:%H:%M}] {stats}")
    return stats + "\n" + skipped_note(skip_items)


def wait_minutes(cfg):
    h = dt.datetime.now(KST).hour      # 서버 시각이 아니라 한국 시각 기준
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
    # 날짜: 평점·가격 숫자를 날짜로 읽지 않고, 못 찾으면 None
    assert stay_date("10/12 체크인", today) == dt.date(2026, 10, 12) and stay_date("10.9~10일", today) == dt.date(2026, 10, 9)
    assert stay_date("9.5~9.6 메이플레이스", today) < today and stay_date("12.5만원 양도", today) is None
    assert stay_date("까무니 4.9 ・ 후기 12 배송 +0.2", today, strict=True) is None
    assert stay_date("(27.1.11-12, 1박)", today) == dt.date(2027, 1, 11) and stay_date("26.11.24 화~26.12.01 화", today) == dt.date(2026, 11, 24)
    assert stay_date("5월24일 숙박", today) < today and stay_date("1/2 체크인", today) == dt.date(2027, 1, 2)
    assert stay_date("10/1일, 10/9금, 10/10토", today) == dt.date(2026, 10, 9)
    assert use_by("신고하기 사용기한 2026.10.12 수량 1개") == dt.date(2026, 10, 12) and use_by("사용기한 사용기한 표시 없음") is None
    assert age_minutes("3시간 전") == 180 and age_minutes("2일 전") == 2880 and age_minutes("없음") is None
    now = dt.datetime(2026, 10, 3, 21, 0, tzinfo=KST)
    assert fresh({"age": 60}, c, now) and not fresh({"age": 60 * 24 * 2}, c, now) and fresh({}, c, now)
    assert posted_at({"ts": 1790000000}, now).tzinfo is not None
    assert hotel_name("엑소 중콘 숙소양도 잠실 스테이 호텔이라고 써 있어") == "잠실 스테이 호텔"
    assert hotel_name("호텔 양도합니다") == "" and hotel_name("호텔나루 9/26") == "호텔나루" and hotel_name("롯데호텔 월드 2박") == "롯데호텔"
    out = []
    walk({"data": {"list": [{"pid": 1, "name": "롯데호텔 2박 양도", "price": "240000"},
                            {"seq": "ab1", "title": "숙소 양도", "salePrice": 90000}]}}, out)
    assert len(out) == 2 and out[0]["price"] == 240000
    assert len(next_data('<script id="__NEXT_DATA__" type="application/json">{"a":[{"seq":7,"title":"잠실 호텔 양도","price":80000}]}</script>')) == 1
    # 지역: 호텔 위치 기준. 제목 우선, 수도권 밖·해외는 OUT, 다른 낱말 속 지명은 무시
    assert region_of("강남 롯데호텔 양도")[0] == "서울" and region_of("송도 호텔 1박")[0] == "인천" and region_of("평택고덕 숙소 양도")[0] == "경기"
    assert region_of("부산 해운대 호텔 양도")[0] == "OUT" and region_of("호텔 양도합니다")[0] is None
    assert region_of("강릉세인트존스호텔 성수기 숙박양도")[0] == "OUT" and region_of("호텔 2박 예약/양도", "성수기 주말 문의")[0] is None
    assert region_of("경주 10월9일 숙박 숙소 양도", "직거래는 강남역에서 합니다")[0] == "OUT"
    assert region_of("호텔 양도", "야구 경기 관람하러 잡은 방입니다")[0] is None and region_of("호텔 양도", "직거래 CU관악점 앞")[0] is None
    assert region_of("the b hakata 호텔 (더 비 하카타 호텔) 양도")[0] == "OUT" and region_of("토요코인 영등포 호텔 양도합니다") == ("서울", "영등포")
    assert region_of("롯데시티호텔 김포공항 숙박권 양도") == ("서울", "김포공항") and region_of("세인트존스 호텔 숙박 양도", "위치 : 강원특별자치도 강릉시")[0] == "OUT"
    assert region_of("롯데월드 근처 호텔 양도")[0] == "서울" and region_of("파라다이스시티 숙박")[0] == "인천"
    # 숙박 글 판정: 양도 + (호텔·숙박·숙소), 굿즈·구하는 글 제외
    assert not is_stay("[가격변동x] 해즈빈 호텔 찰리 알라스터 핀", "") and not is_stay("세븐틴 호텔 멤버십 포카 양도", "")
    assert not is_stay("영화 그랜드부다페스트호텔 뱃지 2종 일괄 양도", "") and not is_stay("서울 호텔 양도 받습니다", "서울 호텔 양도 받습니다")
    assert is_stay("호텔 양도합니다", "호텔 양도합니다") and not is_stay("잠실 호텔", "잠실 호텔 10/17 1박 체크인 가능")
    assert is_stay("잠실새내 노블스테이 패밀리룸 숙박(정가 24)", "잠실새내 노블스테이 패밀리룸 숙박(정가 24) 숙박권 양도합니다")

    def J(title, price, rest="", day=today):
        it = {"title": title, "price": price}
        it["meta"], it["core"] = split_detail(rest) if rest else ("", "")
        return judge(it, c, day), it
    assert J("토요코인 영등포 호텔 양도합니다 당일 (10/8-10/9)", 50000, "50,000원 39분 전 6 1 1 신고하기 사용기한 2026.10.09 수량 1개 토요코인 서울영등포점 1박 숙박권 양도합니다.")[0] == "alert"
    v, it = J("10/7~10/9 2박3일 평택고덕(테라스) 숙소 양도", 100000)
    assert v == "alert" and it["pn"] == 50000 and it["tier"] == "strong" and it["region"] == "경기" and it["date"] == dt.date(2026, 10, 7)
    assert J("해즈빈호텔 이치방쿠지 찰리 아크릴 스탠드 양도", 4000)[0] == "goods"
    assert J("호텔 양도", 9000, "9,000원 1시간 전 신고하기 상품상태 새 상품 (미사용) 수량 1개 호텔 양도합니다 배송비 일반 4,000원")[0] == "goods"
    assert J("후쿠오카 숙소 양도 10/22-10/24", 200000)[0] == "region_out" and J("10/8 부여 숙박 숙소 모텔 호텔 펜션 양도합니다", 100000, day=dt.date(2026, 10, 8))[0] == "region_out"
    assert J("화곡 프롬에이치 어스 호텔 숙박 1박 양도", 53190, "53,190원 1달 전 신고하기 사용기한 2026.08.16 수량 1개 객실 양도해요")[0] == "past"
    assert J("웨스틴 서울 파르나스 호텔 숙박 양도", 450000)[0] == "price" and J("호텔 숙박 양도합니다", 90000)[0] == "region_unknown"
    assert J("서울 호텔 양도 받습니다~(10/3~10/5)", 100000)[0] == "buy"
    m, co = split_detail("120,000원 1달 전 110 2 2 신고하기 사용기한 2026.08.16 수량 1개 16~17일 1박이구요 연락주세요 ~ 배송비 무료배송 구매하기 팔껏만팜 3.9 ・ 후기 4")
    assert "사용기한" in m and "팔껏만팜" not in co and "1박이구요" in co
    it = dict(out[0], site="번개장터", url="https://m.bunjang.co.kr/products/1", core="강남 10/12 체크인", age=120)
    a = build_alert(it, c, ref=200000)
    assert a["tier"] == "normal" and "40% 저렴" in a["text"] and "서울 (근거: 강남)" in a["text"] and "2026-10-12" in a["text"] and a["html"].count("<a href") == 2
    class FP:
        def __init__(s, body): s.b = body
        def goto(s, *a, **k): pass
        def wait_for_timeout(s, ms): pass
        def inner_text(s, sel): return s.b
        def close(s): pass

    class FC:
        def __init__(s, body): s.b = body
        def new_page(s): return FP(s.b)
    d2 = {"title": "잠실 호텔 양도", "desc": "", "price": 100000, "url": "u"}
    assert fetch_detail(FC("판매완료\n잠실 호텔 양도\n" + "내용 " * 40), d2) and d2["sold"]
    d3 = {"title": "x 호텔", "desc": "", "price": 1, "url": "u"}
    assert not fetch_detail(FC("짧음"), d3) and d3["detail_ok"] is False
    assert lookup_price(FC("captcha"), "잠실 스테이 호텔", today, 1)[1] == "아고다가 자동 접속을 막음"
    t1 = parse_tile("https://web.joongna.com/product/232926605",
                    "내일숙박 경주 에톤 한화 리조트 숙박 예약 양도\n290,000원\n1분 전\n무료배송", r"/product/(\w+)")
    assert t1["id"] == "232926605" and t1["price"] == 290000 and t1["age"] == 1
    t3 = parse_tile("https://web.joongna.com/product/5", "제목 2시간 전 312시간 전", r"/product/(\w+)", ["제목 호텔 양도", "120,000원", "3", "1", "2시간 전"])
    assert t3["age"] == 120 and t3["price"] == 120000
    assert parse_tile("https://web.joongna.com/product/1", "제목\n판매완료\n10,000원", r"/product/(\w+)") is None
    # 실제 글 화면 모양: 아래 '비슷한 상품'과 상점 정보의 지역 이름이 섞이지 않아야 한다
    page_body = ("홈\n10/4 일 속초 2인 숙박 호텔 양도\n140,000원\n찜 2개, 조회수 104회\n10시간 전\n사용기한\n2026.10.04\n수량\n1개\n"
                 "속초 마리 비스타 호텔 2인 숙박권 양도합니다. 조식 포함.\n배송비\n무료배송\n구매하기\n강남상점 5 ・ 후기 3\n\n이 상품과 비슷해요\n서울역 마그넷 60,000원\n롯데호텔 서울 키링")
    d4 = {"title": "10/4 일 속초 2인 숙박 호텔 양도", "desc": "", "price": 140000, "url": "u"}
    assert fetch_detail(FC(page_body), d4) and d4["age"] == 600 and "강남상점" not in d4["core"] and "서울역" not in d4["core"]
    assert judge(d4, c, today) == "region_out" and d4["date"] == dt.date(2026, 10, 4)
    jn = ("이벤트\n최근 본 상품\n홈\n티켓/쿠폰\n인천 오라카이 송도파크호텔 숙박 양도 10월14~15일(1박)\n80,000원\n직거래\n아라동\n상품 정보\n"
          "오라카이 송도파크호텔 숙박 양도 10월14~15일(1박)\n디럭스더블룸. 조식미포함이며 날짜변경 불가능합니다.\n결제 혜택 모아보기\nCONTACT\n"
          "(주)중고나라\n주소: 서울 강남구 테헤란로 415 L7 호텔 강남타워 4층")
    d5 = {"title": "인천 오라카이 송도파크호텔 숙박 양도 10월14~15일(1박)", "desc": "", "price": 80000, "url": "u", "age": 60}
    assert fetch_detail(FC(jn), d5) and "강남" not in d5["core"]
    assert judge(d5, c, today) == "alert" and d5["region"] == "인천" and d5["n"] == 1 and d5["date"] == dt.date(2026, 10, 14) and d5["tier"] == "strong"
    print("selftest 통과")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    if "--next-wait" in sys.argv:        # 다음 확인까지 기다릴 분(한국 시각 기준). hotel.yml이 사용
        print(wait_minutes(DEFAULT))
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
