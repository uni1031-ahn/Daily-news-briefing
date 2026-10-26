#!/usr/bin/env python3
"""
보험/의료 뉴스 일일 브리핑 v3 (무료 도구만 사용)

v3 변경점
 - 분류별 리스팅: 기사 페이지의 '신문사 자체 섹션'(메타태그/브레드크럼/RSS 카테고리)을 읽어
   GA·상품·정책 등 통합 카테고리로 묶고, 카테고리마다 화제성 상위 N건을 보여줌 (LAYOUT=category)
 - 섹션을 못 읽은 기사는 제목 키워드로 '자동분류' (메일에 표시됨)
 - 공모전·봉사·콘서트·[포토]·[인사] 같은 홍보/단신성 제목 제외
 - --sections : 신문사별 원본 섹션명을 집계해 보여주는 진단 모드 (분류표 튜닝용)

v2 변경점
 - 신문사 자체 RSS(/rss/allArticle.xml)를 먼저 시도 → 직접 링크 + 기사 요약문 확보
 - 자체 RSS가 없으면 Google News RSS(도메인 검색) → 그래도 0건이면 '신문사 이름'으로 재검색
 - 보험 분야에서 종합 금융지(한국금융신문/DFT대한금융신문/더벨)는 보험 관련 기사만 채택
 - 이메일 설정이 비어 있으면 어떤 값이 비었는지 로그에 표시

사용:
  python news_briefing.py --check      # 신문사별 수집 건수만 확인
  python news_briefing.py --dry-run    # 발송 없이 결과 확인 (briefing.html 생성)
  python news_briefing.py              # 실제 발송
"""
import argparse
import base64
import collections
import concurrent.futures
import difflib
import hashlib
import html
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

import feedparser
import requests

try:
    import extra_news          # 2차 수집(네이버 뉴스 검색). 실패해도 1차 브리핑은 계속 진행
except Exception as _e:
    extra_news = None
    print(f"[2차] extra_news 모듈을 불러오지 못했습니다: {_e}", file=sys.stderr)

# ───────────────────────── 설정 ─────────────────────────
TOP_N = int(os.getenv("TOP_N", "10") or "10")      # 분야별 상위 기사 수 (10 또는 20)
HOURS = int(os.getenv("HOURS", "24") or "24")      # 최근 몇 시간 기사 수집
SIM_THRESHOLD = 0.8                                # 제목 유사도 기준(0~1)
LAYOUT = (os.getenv("LAYOUT", "category") or "category").strip()   # category | buzz_recent
PER_CATEGORY = int(os.getenv("PER_CATEGORY", "5") or "5")          # 카테고리별 표시 건수
SUMMARY_CHARS = int(os.getenv("SUMMARY_CHARS", "130") or "130")    # 요약문 최대 글자 수 (줄이면 메일이 촘촘해짐)
ROW_PAD = int(os.getenv("ROW_PAD", "4") or "4")                    # 표 행의 위아래 여백(px). 작을수록 촘촘함
RUN_INFO = {"note": "", "warn": False}     # 메일 상단에 표시할 수집 현황(2차 수집 모듈이 채움)
BUILD = "v3.11-portal"                                             # 메일 맨 아래에 표시되는 버전 (새 코드 적용 확인용)

SOURCES = {
    "보험": {
        "한국보험신문": "insnews.co.kr",
        "보험저널": "insjournal.co.kr",
        "보험매일": "dailyinsu.com",
        "한국금융신문": "fntimes.com",
        "DFT대한금융신문": "kbanker.co.kr",
        "더벨": "thebell.co.kr",
    },
    "의료": {
        "의학신문": "bosa.co.kr",
        "의협신문": "doctorsnews.co.kr",
        "의사신문": "doctorstimes.com",
        "청년의사": "docdocdoc.co.kr",
        "메디게이트뉴스": "medigatenews.com",
        "데일리메디": "dailymedi.com",
        "메디파나신문": "medipana.com",
    },
}

# 도메인으로 0건일 때, 구글뉴스 '출처 이름'으로 재검색할 때 쓰는 이름 표기들
NAME_ALIASES = {
    "메디컬타임즈": ["메디컬타임즈", "메디칼타임즈"],
    "DFT대한금융신문": ["대한금융신문", "DFT"],
    "더벨": ["더벨", "thebell"],
    "더메디컬": ["더메디컬", "themedical"],
}

# 종합 금융지: 보험 분야에서는 아래 키워드가 들어간 기사만 채택
GENERAL_FINANCE = {"한국금융신문", "DFT대한금융신문", "더벨"}
INSURANCE_KEYWORDS = [
    "보험", "손보", "생보", "손해보험", "생명", "화재", "라이프", "금감원", "금융감독원",
    "보장", "연금", "실손", "설계사", "GA", "언더라이팅", "재보험", "보험료", "IFRS17",
    "K-ICS", "지급여력", "삼성화재", "현대해상", "DB손보", "KB손보", "메리츠", "한화생명",
    "교보", "신한라이프", "농협생명", "농협손보", "라이나", "동양생명", "흥국", "롯데손보",
    "방카", "보험사",
]

# 제목에 아래 단어가 있는데 보험 키워드는 없는 기사는 보험 분야에서 제외 (은행·증권 홍보성 기사 등)
NON_INSURANCE_HINTS = ["은행", "증권", "카드", "저축은행", "캐피탈", "코스피", "코스닥", "주가",
                       "가상자산", "비트코인", "대출금리", "예금금리", "펀드"]


def is_off_topic_insurance(title: str) -> bool:
    """제목이 은행·증권 등 타 업권 중심이고 보험 관련 단어가 없으면 True."""
    if any(k in title for k in INSURANCE_KEYWORDS):
        return False
    return any(h in title for h in NON_INSURANCE_HINTS)

# ───────────── 분류표 (순서대로 검사, 먼저 맞는 카테고리로 분류) ─────────────
# 신문사 섹션명(예: "보험상품", "GA", "정책/제도")과 기사 제목 둘 다에 같은 키워드를 적용합니다.
# 실제 섹션명을 --sections 로 확인한 뒤 키워드를 추가/수정하세요.
CATEGORY_MAP = {
    "보험": [
        ("정책·감독", ["정책", "제도", "금융당국", "금감원", "금융감독원", "금융위", "보험업법", "규제", "감독",
                    "법안", "개정안", "국회", "국정감사", "국감", "제재", "과징금", "시행령", "가이드라인", "당국"]),
        ("GA·판매채널", ["GA", "법인보험대리점", "대리점", "설계사", "판매채널", "방카", "판매조직", "제판분리", "모집"]),
        ("상품·신상품", ["상품", "신상품", "특약", "출시", "실손", "펫보험", "종신", "암보험", "어린이보험",
                     "운전자보험", "건강보험", "연금보험", "저축보험", "변액", "치아보험", "간병"]),
        ("경영·실적", ["실적", "순이익", "당기순", "지급여력", "K-ICS", "킥스", "IFRS17", "인수", "M&A", "합병",
                    "지분", "CEO", "사장", "경영", "손해율", "적자", "흑자", "신용등급", "자본", "배당", "주가"]),
        ("디지털·AI", ["AI", "인공지능", "디지털", "인슈어테크", "플랫폼", "데이터", "챗봇", "빅데이터", "자동화", "핀테크"]),
        ("소비자·분쟁", ["민원", "분쟁", "소송", "판결", "대법원", "불완전판매", "소비자", "피해", "보험금",
                     "지급거절", "조정", "사기"]),
        ("보험사 동향", ["생보", "손보", "생명", "손해보험", "화재", "보험사", "보험업계", "보험업"]),
    ],
    "의료": [
        ("정책·제도", ["정책", "제도", "복지부", "의료법", "법안", "국회", "국감", "국정감사", "건보", "건강보험",
                    "수가", "심평원", "급여", "식약처", "시행", "개정", "의료개혁", "필수의료", "공공의료", "지역의료"]),
        ("의료계·단체", ["의협", "의사회", "대한의사", "전공의", "의대생", "의정", "의료계", "병협", "협회",
                     "집단", "파업", "사직", "증원"]),
        ("제약·바이오", ["제약", "바이오", "신약", "약가", "백신", "의약품", "품목허가", "임상시험", "임상3상",
                     "치료제", "제네릭", "CMO", "CDMO"]),
        ("의료기기·디지털헬스", ["의료기기", "디지털헬스", "AI", "인공지능", "원격", "헬스케어", "웨어러블",
                         "로봇수술", "스마트병원"]),
        ("병원·의료기관", ["병원", "의료원", "의원", "개원", "협진", "수술", "진료", "의료기관", "상급종합", "센터"]),
        ("학술·학회", ["학회", "학술", "논문", "연구", "세미나", "심포지엄", "학술대회", "저널"]),
    ],
}

# 제목에 아래 문구가 있으면 제외 (홍보·동정·단신성 기사). 필요하면 추가/삭제하세요.
EXCLUDE_TITLE_PATTERNS = [
    "[포토]", "[사진]", "[부고]", "[부음]", "[인사]", "[동정]", "[게시판]", "[알림]", "[화보]", "[광고]",
    "[카드뉴스]", "[오늘의", "공모전", "봉사활동", "봉사 활동", "후원금", "기부금", "콘서트", "장학금",
    "골프대회", "체육대회", "송년회",
]

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ───────────────────────── 수집 ─────────────────────────
def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


_TZ_RE = re.compile(r"(Z|GMT|UTC|[+-]\d{2}:?\d{2})\s*$", re.I)


def to_dt(e):
    """발행 시각(UTC). 원본 문자열에 시간대 표기가 없으면 국내 신문사 RSS이므로 KST로 간주."""
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if not t:
        return None
    dt = datetime(*t[:6], tzinfo=timezone.utc)
    raw = (e.get("published") or e.get("updated") or "").strip() if hasattr(e, "get") else ""
    if raw and not _TZ_RE.search(raw):
        dt -= timedelta(hours=9)
    return dt


def fetch_direct(name: str, domain: str, cutoff):
    """신문사 자체 RSS (많은 국내 인터넷신문이 /rss/allArticle.xml 사용)."""
    for host in (f"https://www.{domain}", f"https://{domain}"):
        try:
            r = requests.get(f"{host}/rss/allArticle.xml", headers=UA, timeout=15)
            if r.status_code != 200 or "<item" not in r.text:
                continue
            feed = feedparser.parse(r.content)
            items = []
            if feed.entries:      # 시간대 해석 점검용 (원본 pubDate가 어떻게 오는지 로그로 확인)
                e0 = feed.entries[0]
                raw0 = e0.get("published") or e0.get("updated") or ""
                dt0 = to_dt(e0)
                print(f"[시간진단] {name}: 원본 pubDate={raw0!r} → KST {dt0.astimezone(KST):%m-%d %H:%M}"
                      if dt0 else f"[시간진단] {name}: 원본 pubDate={raw0!r} → 해석 불가")
            for e in feed.entries:
                pub = to_dt(e)
                if pub and pub < cutoff:
                    continue
                items.append({
                    "title": strip_html(e.get("title", "")),
                    "link": e.get("link", ""),
                    "source": name,
                    "pub": pub,
                    "desc": strip_html(e.get("summary", "") or e.get("description", "")),
                    "direct": True,
                    "cat_raw": [strip_html(t.get("term", "")) for t in (e.get("tags") or []) if t.get("term")],
                })
            if items:
                return items
        except Exception:
            continue
    return []


def google_items(query: str, name: str, cutoff, hours: int, name_filter=None):
    days = max(1, -(-hours // 24))
    q = quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url, request_headers=UA)
    items = []
    for e in feed.entries:
        pub = to_dt(e)
        if pub and pub < cutoff:
            continue
        src_title = (e.get("source", {}) or {}).get("title", "")
        if name_filter:
            key = src_title.replace(" ", "").lower()
            if not any(a.replace(" ", "").lower() in key for a in name_filter):
                continue
        title = html.unescape(e.title)
        title = re.sub(r"\s+-\s+[^-]{1,30}$", "", title).strip()
        items.append({"title": title, "link": e.link, "source": name, "pub": pub,
                      "desc": "", "direct": False})
    return items


def fetch_source(name: str, domain: str, hours: int):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, how = fetch_direct(name, domain, cutoff), "자체RSS"
    if not items:
        items, how = google_items(f"site:{domain}", name, cutoff, hours), "구글뉴스(도메인)"
    if not items:
        aliases = NAME_ALIASES.get(name, [name])
        items, how = google_items(f'"{aliases[0]}"', name, cutoff, hours, aliases), "구글뉴스(이름)"
    return items, (how if items else "수집실패")


# ───────────────────────── 중복 병합 ─────────────────────────
def norm(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", t)
    t = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


JOSA = ("으로", "에서", "에게", "까지", "부터", "이나", "에는", "에도",
        "와", "과", "은", "는", "이", "가", "을", "를", "의", "에", "로", "도")


def stem(w: str) -> str:
    """단어 끝의 조사를 떼어 '65만명에'와 '65만명', '지원에'와 '지원'을 같은 단어로 취급."""
    if len(w) >= 3:
        for j in JOSA:
            if w.endswith(j) and len(w) - len(j) >= 2:
                return w[: -len(j)]
    return w


def tokens(title: str) -> set:
    return {stem(w) for w in norm(title).split() if len(w) >= 2}


def quoted_phrases(title: str) -> frozenset:
    """제목 속 따옴표 안 문구(상품명·인용문)를 정규화해서 반환 (너무 짧은 것은 제외)."""
    out = set()
    for m in re.finditer(r"['‘“\"「《]([^'’”\"」》]{2,40})['’”\"」》]", title or ""):
        q = norm(m.group(1))
        if len(q.replace(" ", "")) >= 4:
            out.add(q)
    return frozenset(out)


def quote_conflict(q1, q2) -> bool:
    """양쪽 제목에 따옴표 문구가 모두 있는데 서로 겹치지(포함하지) 않으면 True (서로 다른 상품명)."""
    if not q1 or not q2:
        return False
    return not any(a in b or b in a for a in q1 for b in q2)


def desc_ok(d1, d2) -> bool:
    """안전장치: 두 기사의 본문 앞부분(RSS 설명)이 있으면 단어가 조금이라도 겹쳐야 같은 이슈로 인정."""
    if len(d1) < 5 or len(d2) < 5:
        return True                      # 설명이 부족하면 제목 판단만 사용
    return len(d1 & d2) / min(len(d1), len(d2)) >= 0.15


def similar(n1, t1, n2, t2, q1=frozenset(), q2=frozenset(), d1=frozenset(), d2=frozenset()) -> bool:
    """같은 이슈의 기사인지 판단: (0) 회사명 확인 (1) 문자열 유사도 (2) 핵심 단어 겹침 (3) 같은 상품명 인용 (4) 본문 안전장치."""
    if n1 == n2:
        return True
    # 제목 맨 앞 회사명이 서로 다르면(예: 삼성생명 vs 한화생명) 다른 이슈로 판단
    co_suffix = ("생명", "화재", "손보", "생보", "해상", "라이프", "은행", "카드", "증권", "캐피탈", "금융", "보험")
    f1, f2 = n1.split()[0], n2.split()[0]
    if f1 != f2 and f1.endswith(co_suffix) and f2.endswith(co_suffix):
        return False
    if difflib.SequenceMatcher(None, n1, n2).ratio() >= SIM_THRESHOLD:
        return True
    common = t1 & t2
    if not common:
        return False
    overlap = len(common) / min(len(t1), len(t2))
    long_common = any(len(w) >= 6 for w in common)      # 상품명·고유명사처럼 긴 공통 단어
    loose = (
        # (0') 같은 따옴표 문구(상품명 등) + 다른 공통 단어 3개 이상
        ((q1 & q2) and len(common) >= 3)
        # (a) 긴 공통 단어가 있고 공통 단어 3개 이상, 짧은 쪽 제목의 60% 이상 겹침
        or (len(common) >= 3 and overlap >= 0.6 and long_common)
        # (b) 긴 공통 단어가 없어도 공통 단어 5개 이상, 70% 이상 겹침 (한글/영문 표기 차이 등)
        or (len(common) >= 5 and overlap >= 0.7)
        # (c) 같은 회사 기사이고 공통 단어(회사명 포함) 4개 이상, 50% 이상 겹침
        #     단, 양쪽 제목에 서로 다른 상품명(따옴표 문구)이 있으면 다른 상품 기사로 보고 제외
        or (f1 == f2 and len(common) >= 4 and overlap >= 0.5 and not quote_conflict(q1, q2))
    )
    return bool(loose) and desc_ok(d1, d2)


def cluster(items):
    """제목이 같거나 같은 이슈로 보이는 기사를 하나로 병합."""
    clusters = []
    for it in sorted(items, key=lambda x: x["pub"] or EPOCH, reverse=True):
        n = norm(it["title"])
        if not n:
            continue
        tk = tokens(it["title"])
        qs = quoted_phrases(it["title"])
        ds = frozenset(tokens((it.get("desc") or "")[:200]))
        for c in clusters:
            if any(similar(n, tk, m, tm, qs, qm, ds, dm) for m, tm, qm, dm in c["keys"]):
                c["articles"].append(it)
                c["keys"].append((n, tk, qs, ds))
                break
        else:
            clusters.append({"norm": n, "keys": [(n, tk, qs, ds)], "articles": [it]})
    return clusters


# 화제성 점수에 가산점을 주는 키워드 (필요하면 자유롭게 수정/추가하세요)
IMPORTANT_KEYWORDS = {
    "보험": ["금감원", "금융위", "금융감독원", "보험업법", "제재", "과징금", "인상", "인하",
            "개정", "도입", "시행", "소송", "대법원", "판결", "실손", "IFRS17", "K-ICS",
            "인수", "합병", "M&A", "손해율", "적자", "흑자", "민원", "불완전판매"],
    "의료": ["복지부", "보건복지부", "의대", "정원", "전공의", "수가", "건강보험", "건보",
            "심평원", "식약처", "허가", "의료법", "파업", "개정", "시행", "소송", "판결",
            "필수의료", "응급", "임상", "승인", "급여", "약가", "리베이트"],
}


def external_buzz(title: str) -> int:
    """같은 이슈를 최근 2일간 보도한 '서로 다른 언론사' 수 (구글뉴스 검색, 15개 지정 신문사 밖 포함)."""
    tokens = [t for t in norm(title).split() if len(t) >= 2][:6]
    if not tokens:
        return 0
    try:
        q = quote(" ".join(tokens) + " when:2d")
        feed = feedparser.parse(f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko",
                                request_headers=UA)
        pubs = {(e.get("source", {}) or {}).get("title", "") for e in feed.entries}
        pubs.discard("")
        return len(pubs)
    except Exception:
        return 0


def rank(clusters, top_n, field=""):
    """TOP_N 중 BUZZ_RATIO(기본 0.5) 만큼은 '화제성 순', 나머지는 '최신 순'으로 채움.
    반환: (화제성 목록, 최신 목록)  — 두 목록은 서로 겹치지 않음."""
    ratio = min(max(float(os.getenv("BUZZ_RATIO", "0.5") or "0.5"), 0.0), 1.0)
    buzz_n = round(top_n * ratio)
    recent_n = top_n - buzz_n
    use_buzz = os.getenv("USE_EXTERNAL_BUZZ", "1") != "0"
    kws = IMPORTANT_KEYWORDS.get(field, [])

    def newest(c):
        return max((a["pub"] for a in c["articles"] if a["pub"]), default=EPOCH)

    def base(c):
        srcs = {a["source"] for a in c["articles"]}
        return len(srcs) * 3 + min(len(c["articles"]), 6) * 0.5

    buzz_top = []
    if buzz_n:
        ranked = sorted(clusters, key=lambda c: (base(c), newest(c)), reverse=True)
        cand = ranked[: buzz_n * 3]      # 상위 후보만 외부 보도량 조회 (호출 수 절약)
        for c in cand:
            title = c["articles"][0]["title"]
            c["buzz"] = external_buzz(title) if use_buzz else 0
            if use_buzz:
                time.sleep(0.7)
            hit = sum(1 for k in kws if k in title)
            c["score"] = base(c) + min(c["buzz"], 15) * 1.0 + min(hit, 2) * 1.5
        buzz_top = sorted(cand, key=lambda c: (c["score"], newest(c)), reverse=True)[:buzz_n]

    chosen = {id(c) for c in buzz_top}
    rest = [c for c in clusters if id(c) not in chosen]
    recent_top = sorted(rest, key=lambda c: (newest(c), base(c)), reverse=True)[:recent_n]
    return buzz_top, recent_top


# ───────────────────────── 분류 (신문사 자체 섹션) ─────────────────────────
PAGE_CACHE = {}


def get_page(url: str) -> str:
    """기사 페이지 HTML (캐시). 인코딩(EUC-KR 등)을 직접 판별."""
    if url in PAGE_CACHE:
        return PAGE_CACHE[url]
    text = ""
    try:
        r = requests.get(url, headers=UA, timeout=12)
        if r.status_code == 200:
            raw = r.content
            m = re.search(rb"charset=[\"']?([A-Za-z0-9_\-]+)", raw[:4000])
            enc = m.group(1).decode("ascii", "ignore") if m else (r.encoding or "utf-8")
            try:
                text = raw.decode(enc, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    PAGE_CACHE[url] = text
    return text


GENERIC_LABELS = {"홈", "home", "뉴스", "news", "기사", "전체", "all", "article", "신문"}


def extract_sections(page: str):
    """기사 페이지에서 신문사 자체 섹션명 후보를 추출 (메타태그 → JSON-LD → 브레드크럼 순)."""
    labels = []
    head = page[:80000]
    for m in re.finditer(r"<meta\s[^>]*>", head, flags=re.I):
        tag = m.group(0)
        k = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        v = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", tag, re.I)
        if k and v and k.group(1).strip().lower() in (
                "article:section", "og:article:section", "article:section2", "section", "category"):
            labels.append(html.unescape(v.group(1)).strip())
    for m in re.finditer(r'"articleSection"\s*:\s*(\[[^\]]*\]|"[^"]*")', page[:200000]):
        labels += [html.unescape(x).strip() for x in re.findall(r'"([^"]+)"', m.group(1))]
    if not labels:
        m = re.search(
            r"<(?:ul|ol|div|nav|p|span|h\d)[^>]*class=[\"'][^\"']*(?:breadcrumb|article-category|view-category|"
            r"cate-name|sec-name|section-name)[^\"']*[\"'][^>]*>(.{0,400}?)</(?:ul|ol|div|nav|p|span|h\d)>",
            page[:200000], flags=re.I | re.S)
        if m:
            parts = [x.strip() for x in re.split(r"[>›»|/]", strip_html(m.group(1))) if x.strip()]
            parts = [x for x in parts if x.lower() not in GENERIC_LABELS]
            if parts and len(parts[-1]) <= 15:
                labels.append(parts[-1])
    out = []
    for lab in labels:
        if lab and len(lab) <= 30 and lab.lower() not in GENERIC_LABELS and lab not in out:
            out.append(lab)
    return out[:3]


def classify(raw_labels, title: str, field: str):
    """(카테고리, 근거) 반환. 근거: '섹션'=신문사 분류 사용, '키워드'=제목 자동분류, '없음'."""
    rules = CATEGORY_MAP.get(field, [])
    for lab in raw_labels:
        lab_u = lab.upper()
        for cat, keys in rules:
            if any(k.upper() in lab_u for k in keys):
                return cat, "섹션", lab
    for cat, keys in rules:
        if any(k in title for k in keys):
            return cat, "키워드", ""
    return "기타", "없음", ""


def filter_excluded(items):
    kept = [it for it in items if not any(p in it["title"] for p in EXCLUDE_TITLE_PATTERNS)]
    return kept, len(items) - len(kept)


def assign_categories(clusters, field):
    """클러스터마다 기사 페이지의 섹션명을 읽어 카테고리를 정함 (대표 기사 페이지만 조회)."""
    todo = []
    for c in clusters:
        arts = sorted(c["articles"], key=lambda a: not a.get("direct"))
        c["articles"] = arts
        a = arts[0]
        if a.get("direct") and not a.get("cat_raw") and a.get("link"):
            todo.append(a)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda a: get_page(a["link"]), todo))
    for a in todo:
        a["sec_raw"] = extract_sections(PAGE_CACHE.get(a["link"], ""))

    for c in clusters:
        votes = collections.Counter()
        how_by_cat, label_by_cat = {}, {}
        for a in c["articles"]:
            labels = a.get("cat_raw") or a.get("sec_raw") or []
            cat, how, lab = classify(labels, a["title"], field)
            votes[cat] += 2 if how == "섹션" else (1 if how == "키워드" else 0)
            if how == "섹션" or cat not in how_by_cat:
                how_by_cat[cat], label_by_cat[cat] = how, lab
        if votes and votes.most_common(1)[0][1] > 0:
            cat = votes.most_common(1)[0][0]
        else:
            cat = "기타"
        c["cat"], c["cat_how"], c["cat_label"] = cat, how_by_cat.get(cat, "없음"), label_by_cat.get(cat, "")


def _newest(c):
    return max((a["pub"] for a in c["articles"] if a["pub"]), default=EPOCH)


def _base(c):
    srcs = {a["source"] for a in c["articles"]}
    return len(srcs) * 3 + min(len(c["articles"]), 6) * 0.5


def select_by_category(clusters, field, per_cat):
    """카테고리별로 화제성 점수 상위 per_cat건 선정. 반환: [(카테고리, [클러스터...]), ...]"""
    use_buzz = os.getenv("USE_EXTERNAL_BUZZ", "1") != "0"
    kws = IMPORTANT_KEYWORDS.get(field, [])
    buckets = collections.defaultdict(list)
    for c in clusters:
        buckets[c["cat"]].append(c)
    order = [cat for cat, _ in CATEGORY_MAP.get(field, [])] + ["기타"]
    result = []
    for cat in order:
        lst = buckets.get(cat, [])
        if not lst:
            continue
        cand = sorted(lst, key=lambda c: (_base(c), _newest(c)), reverse=True)[: per_cat * 2]
        for c in cand:
            title = c["articles"][0]["title"]
            c["buzz"] = external_buzz(title) if use_buzz else 0
            if use_buzz:
                time.sleep(0.7)
            hit = sum(1 for k in kws if k in title)
            c["score"] = _base(c) + min(c["buzz"], 15) * 1.0 + min(hit, 2) * 1.5
        top = sorted(cand, key=lambda c: (c["score"], _newest(c)), reverse=True)[:per_cat]
        result.append((cat, top))
    return result


def print_diagnostics(field, items, clusters):
    """신문사별 원본 섹션명 집계 + 분류 결과 요약 (분류표 튜닝용 로그)."""
    per_src = collections.defaultdict(collections.Counter)
    total = collections.Counter()
    got = collections.Counter()
    for c in clusters:
        for a in c["articles"]:
            labs = a.get("cat_raw") or a.get("sec_raw")
            if labs is None:
                continue
            total[a["source"]] += 1
            if labs:
                got[a["source"]] += 1
                for lab in labs:
                    per_src[a["source"]][lab] += 1
    print(f"[섹션진단:{field}] 신문사별 원본 섹션명 (섹션 확보 기사/조회 기사)")
    for src in sorted(total):
        top = ", ".join(f"{k}×{v}" for k, v in per_src[src].most_common(8)) or "(섹션명 없음)"
        print(f"   - {src}: {got[src]}/{total[src]} · {top}")
    cnt = collections.Counter((c["cat"], c["cat_how"]) for c in clusters)
    line = ", ".join(f"{cat}({how}){n}" for (cat, how), n in sorted(cnt.items()))
    print(f"[분류결과:{field}] {line}")


# ───────────────────────── 요약 ─────────────────────────
def resolve_url(link: str) -> str:
    if "news.google.com" not in link:
        return link
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(link, interval=1)
        if r.get("status"):
            return r["decoded_url"]
    except Exception:
        pass
    return link


def fetch_body(url: str) -> str:
    if "news.google.com" in url:
        return ""
    page = get_page(url)
    if not page:
        return ""
    try:
        import trafilatura
        return trafilatura.extract(page) or ""
    except Exception:
        return ""


BOILERPLATE = ["글씨 크기", "글씨크기", "글자 크기", "글자크기", "크기 설정", "크기로 변경", "본문 내용은",
               "기사 공유", "무단전재", "무단 전재", "저작권", "ⓒ", "구독", "카카오톡", "페이스북",
               "스크랩", "프린트", "URL 복사", "URL복사", "공유하기", "기사입력", "기사 입력",
               "좋아요", "댓글", "광고 문의", "로그인", "회원가입"]


def clean_text(text: str) -> str:
    """기사 본문에서 '글자크기 설정' 같은 사이트 공통 문구가 든 문장만 제거."""
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        sents = re.split(r"(?<=[.!?다])\s+", ln)
        kept = [x for x in sents if not any(b in x for b in BOILERPLATE)]
        if kept:
            out.append(" ".join(kept))
    return "\n".join(out)


def extractive_summary(text: str, n=2, max_chars=240) -> str:
    text = clean_text(text)
    sents = re.split(r"(?<=[.!?다])\s+", text.replace("\n", " "))
    sents = [s.strip() for s in sents if len(s.strip()) > 15 and not any(b in s for b in BOILERPLATE)]
    return " ".join(sents[:n])[:max_chars].strip()


def gemini_summary(title: str, text: str) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key or not text:
        return ""
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.0-flash:generateContent?key={key}")
        prompt = (f"다음 기사를 한국어로 1~2문장(100자 내외)으로 핵심만 요약해줘. "
                  f"제목: {title}\n본문: {text[:4000]}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""


def shorten(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    m = max(cut.rfind(". "), cut.rfind("다 "), cut.rfind(" "))
    if m >= n * 0.6:
        cut = cut[:m]
    return cut.rstrip(" ,.·") + "…"


def summarize(cl):
    # 직접 링크가 있는 기사를 대표로 우선 사용
    arts = sorted(cl["articles"], key=lambda a: not a.get("direct"))
    a = arts[0]
    url = resolve_url(a["link"])
    cl["url"] = url
    desc = next((x["desc"] for x in arts if len(x.get("desc", "")) > 30), "")
    body = clean_text(fetch_body(url)) or clean_text(desc)
    cl["summary"] = shorten(
        gemini_summary(a["title"], body) or extractive_summary(body, max_chars=400) or clean_text(desc),
        SUMMARY_CHARS)
    time.sleep(1)


# ───────────────────────── 포맷 ─────────────────────────
def drop_repeated_summaries(clusters):
    """서로 다른 기사인데 요약 앞부분이 같으면(사이트 공통 홍보 문구 등) 요약을 비움."""
    def key(c):
        return (c.get("summary") or "").lstrip("| ").strip()[:30]
    cnt = collections.Counter(key(c) for c in clusters if c.get("summary"))
    removed = 0
    for c in clusters:
        k = key(c)
        if k and cnt[k] >= 2:
            c["summary"] = ""
            removed += 1
    if removed:
        print(f"[요약] 반복되는 공통 문구 요약 {removed}건 제외")


def build(sections):
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    text_lines = [f"📰 보험·의료 뉴스 브리핑 {today}\n"]
    html_parts = [f"<h2>📰 보험·의료 뉴스 브리핑 {today}</h2>"]
    if RUN_INFO["note"]:
        text_lines.append(f"ℹ {RUN_INFO['note']}\n")
    for field, top in sections.items():
        text_lines.append(f"━━ {field} ({len(top)}건) ━━")
        html_parts.append(f"<h3>{field} ({len(top)}건)</h3><ol>")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            src_txt = ", ".join(sorted({x["source"] for x in c["articles"]}))
            if c.get("buzz"):
                src_txt += f" · 🔥관련보도 {c['buzz']}곳"
            if c.get("cat_how") == "섹션" and c.get("cat_label"):
                src_txt += f" · 📂{c['cat_label']}"
            elif c.get("cat_how") == "키워드":
                src_txt += " · 자동분류"
            link = c.get("url", a["link"])
            text_lines.append(f"{i}. {a['title']}  [{src_txt}]")
            if c.get("summary"):
                text_lines.append(f"   └ {c['summary']}")
            text_lines.append(f"   {link}")
            html_parts.append(
                f"<li style='margin-bottom:10px'><b><a href='{html.escape(link)}'>{html.escape(a['title'])}</a></b>"
                f" <small>[{html.escape(src_txt)}]</small>"
                + (f"<br><span style='color:#444'>{html.escape(c['summary'])}</span>" if c.get("summary") else "")
                + "</li>")
        html_parts.append("</ol>")
        text_lines.append("")
    return "\n".join(text_lines), "\n".join(html_parts)


def build_markdown(sections):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    owner = os.getenv("GITHUB_REPOSITORY_OWNER", "")
    lines = [f"@{owner} 오늘의 보험·의료 뉴스 브리핑입니다 ({today})\n" if owner
             else f"오늘의 보험·의료 뉴스 브리핑입니다 ({today})\n"]
    if RUN_INFO["note"]:
        lines.append(f"> ℹ {RUN_INFO['note']}\n")
    for field, top in sections.items():
        lines.append(f"## {field} ({len(top)}건)\n")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            src_txt = ", ".join(sorted({x["source"] for x in c["articles"]}))
            if c.get("buzz"):
                src_txt += f" · 🔥관련보도 {c['buzz']}곳"
            if c.get("cat_how") == "섹션" and c.get("cat_label"):
                src_txt += f" · 📂{c['cat_label']}"
            elif c.get("cat_how") == "키워드":
                src_txt += " · 자동분류"
            link = c.get("url", a["link"])
            title = a["title"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"{i}. **[{title}]({link})** — _{src_txt}_")
            if c.get("summary"):
                lines.append(f"   > {c['summary']}")
        lines.append("")
    return "\n".join(lines)[:60000]


def build_email_html(sections) -> str:
    """이메일용 HTML (표 레이아웃 + 인라인 CSS: Gmail/네이버/Outlook 등에서 깨지지 않도록)."""
    esc = html.escape
    now = datetime.now(KST)
    wd = "월화수목금토일"[now.weekday()]
    # 분야별 건수 집계
    counts = collections.OrderedDict()
    for key, top in sections.items():
        fld = key.split(" · ")[0] if " · " in key else key
        counts[fld] = counts.get(fld, 0) + len(top)
    count_txt = " · ".join(f"{f} {n}건" for f, n in counts.items())

    font = "'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',Arial,sans-serif"
    out = [
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'></head>",
        "<body style='margin:0;padding:0;background:#f4f6f8;'>",
        "<table width='100%' cellpadding='0' cellspacing='0' border='0' style='background:#f4f6f8;'><tr>"
        "<td align='center' style='padding:20px 8px;'>",
        f"<table width='760' cellpadding='0' cellspacing='0' border='0' style='max-width:760px;width:100%;"
        f"background:#ffffff;border:1px solid #dde2e8;font-family:{font};'>",
        "<tr><td style='background:#1f3a5f;color:#ffffff;padding:22px 24px;'>"
        "<div style='font-size:21px;font-weight:bold;'>📰 보험·의료 뉴스 브리핑</div>"
        f"<div style='font-size:13px;color:#cfd9e8;margin-top:6px;'>{now:%Y.%m.%d} ({wd}) 기준 · {esc(count_txt)}</div>"
        "</td></tr>",
        "<tr><td style='padding:12px 24px 2px;font-size:14px;color:#333;line-height:1.5;'>"
        "안녕하세요. 오늘의 보험·의료 주요 뉴스입니다. 기사 제목을 누르면 원문으로 이동합니다."
        "</td></tr>",
    ]
    if RUN_INFO["note"]:
        col = "#c0392b" if RUN_INFO["warn"] else "#888"
        out.append(f"<tr><td style='padding:2px 24px 6px;font-size:11.5px;color:{col};line-height:1.4;'>"
                   f"{esc(RUN_INFO['note'])}</td></tr>")
    cur_field = None
    for key, top in sections.items():
        fld, cat = (key.split(" · ", 1) + [""])[:2] if " · " in key else (key, "")
        if fld != cur_field:
            out.append(
                f"<tr><td style='padding:16px 24px 0;'><div style='font-size:18px;font-weight:bold;color:#1f3a5f;"
                f"border-bottom:2px solid #1f3a5f;padding-bottom:8px;'>{esc(fld)}</div></td></tr>")
            cur_field = fld
        if cat:
            out.append(
                f"<tr><td style='padding:12px 24px 4px;font-size:15px;font-weight:bold;color:#222;'>"
                f"▌{esc(cat)} <span style='font-weight:normal;color:#888;font-size:13px;'>({len(top)}건)</span></td></tr>")
        rows = []
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            link = c.get("url", a["link"])
            srcs = ", ".join(sorted({x["source"] for x in c["articles"]}))
            meta = [esc(srcs)]
            if c.get("buzz"):
                meta.append(f"🔥관련보도 {c['buzz']}곳")
            if c.get("cat_how") == "섹션" and c.get("cat_label"):
                meta.append("📂" + esc(c["cat_label"]))
            summ = (f"<div style='font-size:12.5px;color:#444;margin-top:1px;line-height:1.35;'>{esc(c['summary'])}</div>"
                    if c.get("summary") else "")
            rows.append(
                "<tr>"
                f"<td align='center' valign='top' style='width:36px;padding:{ROW_PAD}px 2px;border:1px solid #dde2e8;"
                f"font-size:13px;line-height:1.3;color:#555;'>{i}</td>"
                f"<td style='padding:{ROW_PAD}px 10px;border:1px solid #dde2e8;font-size:13px;line-height:1.3;'>"
                f"<div style='font-size:14px;line-height:1.3;'><a href='{esc(link, quote=True)}' "
                f"style='font-size:14px;font-weight:bold;color:#1a56db;text-decoration:underline;'>{esc(a['title'])}</a></div>"
                f"{summ}"
                f"<div style='font-size:11.5px;color:#888;margin-top:1px;line-height:1.25;'>{' · '.join(meta)}</div>"
                "</td></tr>")
        out.append(
            "<tr><td style='padding:0 24px;'>"
            "<table width='100%' cellpadding='0' cellspacing='0' border='0' style='border-collapse:collapse;'>"
            "<tr style='background:#eef1f6;'>"
            "<th style='width:36px;padding:4px;border:1px solid #dde2e8;font-size:13px;color:#333;'>No</th>"
            "<th style='padding:4px;border:1px solid #dde2e8;font-size:13px;color:#333;'>기사</th></tr>"
            + "".join(rows) + "</table></td></tr>")
    out.append(
        "<tr><td style='padding:14px 24px;font-size:12px;color:#888;line-height:1.5;border-top:1px solid #eee;'>"
        "이 메일은 자동으로 발송되었습니다. 요약문은 기사 본문 앞부분을 바탕으로 만들어져 원문과 다를 수 있으니 "
        "중요한 내용은 원문을 확인하세요. <span style='color:#bbb;'>[" + BUILD + "]</span></td></tr>")
    out.append("</table></td></tr></table></body></html>")
    return "".join(out)


# ───────────────────────── 발송 ─────────────────────────
def send_github_issue(md_body: str):
    """GitHub 이슈로 올림 → GitHub가 계정 이메일로 알림 메일을 보내줌 (외부 서비스 불필요)."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not (token and repo):
        print("[GitHub 이슈 미발송] GITHUB_TOKEN/GITHUB_REPOSITORY 없음")
        return False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/issues", headers=headers, timeout=30,
                          json={"title": f"[브리핑] 보험·의료 뉴스 {datetime.now(KST):%Y-%m-%d}",
                                "body": md_body})
        if r.status_code != 201:
            print(f"[GitHub 이슈 실패] {r.status_code} {r.text[:300]}")
            return False
        num = r.json()["number"]
        print(f"[GitHub 이슈 생성 완료] #{num} → 알림 메일이 계정 이메일로 발송됩니다")
        # 닫으면 GitHub가 "Closed #N as completed" 알림 메일을 한 통 더 보내므로 기본은 닫지 않음
        if os.getenv("GITHUB_ISSUE_CLOSE") == "1":
            requests.patch(f"https://api.github.com/repos/{repo}/issues/{num}", headers=headers,
                           json={"state": "closed"}, timeout=30)
        return True
    except Exception as ex:
        print(f"[GitHub 이슈 실패] {ex}")
        return False


def send_telegram(text: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for ch in chunks:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": ch, "disable_web_page_preview": True}, timeout=20)
    return True


def send_appsscript(subject: str, html_body: str, text_body: str) -> bool:
    """내 구글 계정의 Apps Script(무료)를 통해 여러 명에게 동시 발송 (MAIL_TO에 쉼표로 여러 주소)."""
    url = (os.getenv("APPS_SCRIPT_URL") or "").strip()
    token = (os.getenv("APPS_SCRIPT_TOKEN") or "").strip()
    to = [x.strip() for x in (os.getenv("MAIL_TO") or "").replace(";", ",").split(",") if x.strip()]
    if not (url and token and to):
        print("[Apps Script 미발송] APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN / MAIL_TO 중 비어 있는 값이 있습니다.")
        return False
    wrapped = html_body
    try:
        r = requests.post(url, json={"token": token, "to": to, "subject": subject,
                                     "html": wrapped, "text": text_body},
                          timeout=90, allow_redirects=True)
        try:
            res = r.json()
        except Exception:
            print(f"[Apps Script 실패] JSON 응답이 아님 (HTTP {r.status_code}). 배포 설정에서 "
                  f"'액세스 권한: 모든 사용자'인지 확인하세요. 응답 일부: {r.text[:150]!r}")
            return False
        if res.get("ok"):
            print(f"[Apps Script 발송 완료] {res.get('sent')}명 → {', '.join(to)}"
                  + (f" (실패: {res.get('failed')})" if res.get("failed") else ""))
            return True
        fp = base64.b64encode(hashlib.sha256(token.encode("utf-8")).digest()).decode()[:6]
        print(f"[Apps Script 실패] {res} (GitHub Secret의 토큰: 길이 {len(token)}, 지문 {fp})")
    except Exception as ex:
        print(f"[Apps Script 실패] {ex}")
    return False


def send_resend(html_body: str, text_body: str):
    """Resend(무료) HTTPS API로 발송. SMTP 포트가 막힌 환경에서도 동작."""
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    to = (os.getenv("MAIL_TO") or "").strip()
    if not key:
        return False
    if not to:
        print("[Resend 미발송] MAIL_TO가 비어 있습니다.")
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "from": "News Briefing <onboarding@resend.dev>",
                "to": [x.strip() for x in to.split(",") if x.strip()],
                "subject": f"[브리핑] 보험·의료 뉴스 {datetime.now(KST):%Y-%m-%d}",
                "html": html_body,
                "text": text_body,
            },
            timeout=30,
        )
        if r.status_code in (200, 201):
            print(f"[Resend 발송 완료] → {to}")
            return True
        print(f"[Resend 발송 실패] {r.status_code} {r.text[:300]}")
    except Exception as ex:
        print(f"[Resend 발송 실패] {ex}")
    return False


def send_email(html_body: str, text_body: str):
    user = (os.getenv("GMAIL_USER") or "").strip()
    pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    to = (os.getenv("MAIL_TO") or "").strip()
    missing = [k for k, v in (("GMAIL_USER", user), ("GMAIL_APP_PASSWORD", pw)) if not v]
    if missing:
        print(f"[이메일 미발송] 비어 있는 설정: {', '.join(missing)} "
              "→ GitHub Settings > Secrets and variables > Actions 에서 이름/값을 확인하세요.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[브리핑] 보험·의료 뉴스 {datetime.now(KST):%Y-%m-%d}"
    msg["From"], msg["To"] = user, (to or user)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    rcpts = [x.strip() for x in (to or user).split(",") if x.strip()]
    # 연결이 끊기는 경우가 있어 587(STARTTLS) → 465(SSL) 순으로, 각 2회씩 재시도
    attempts = [("starttls", 587), ("ssl", 465)] * 2
    last_err = None
    for mode, port in attempts:
        try:
            if mode == "ssl":
                s = smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=30)
            else:
                s = smtplib.SMTP("smtp.gmail.com", port, timeout=30)
                s.ehlo()
                s.starttls()
                s.ehlo()
            with s:
                s.login(user, pw)
                s.sendmail(user, rcpts, msg.as_string())
            print(f"[이메일 발송 완료] {mode}:{port} → {', '.join(rcpts)}")
            return True
        except smtplib.SMTPAuthenticationError as ex:
            print(f"[이메일 발송 실패] 로그인 거부: 앱 비밀번호/계정 주소를 확인하세요. ({ex})")
            return False
        except Exception as ex:
            last_err = ex
            print(f"[재시도] {mode}:{port} 실패: {ex}")
            time.sleep(5)
    print(f"[이메일 발송 실패] {last_err}")
    return False



# ───────────────────────── 누적 저장 (분석용 아카이브) ─────────────────────────
# 브리핑에 실린 기사만이 아니라 '수집한 전체 기사'를 월별 CSV로 쌓습니다.
# (이슈 반복 여부·매체 수·국면 변화 분석은 TOP 기사만으로는 왜곡되기 때문)
ARCHIVE_ON = os.getenv("ARCHIVE", "1") != "0"
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "data/articles")
ARCHIVE_DESC_CHARS = int(os.getenv("ARCHIVE_DESC_CHARS", "150") or "150")
ARCHIVE_COLS = ["id", "first_seen", "pub", "field", "source", "title", "link", "desc",
                "cat", "cat_how", "cluster", "cluster_size", "in_briefing", "buzz", "sec"]


def article_id(field: str, source: str, title: str) -> str:
    """같은 기사가 매일 다시 수집돼도 같은 id가 되도록 (분야+신문사+정규화 제목) 해시."""
    return hashlib.sha1(f"{field}|{source}|{norm(title)}".encode("utf-8")).hexdigest()[:16]


def archive_articles(all_clusters, chosen_ids):
    """수집된 모든 기사를 data/articles/YYYY-MM.csv 에 추가 (이미 저장된 기사는 건너뜀)."""
    import csv
    now = datetime.now(KST)
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    cur_path = os.path.join(ARCHIVE_DIR, f"{now:%Y-%m}.csv")
    seen = set()
    for p in (os.path.join(ARCHIVE_DIR, f"{prev}.csv"), cur_path):
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig", newline="") as f:
                seen |= {r.get("id") for r in csv.DictReader(f)}
    rows, total = [], 0
    for field, clusters in all_clusters.items():
        for c in clusters:
            cid = hashlib.sha1(c["norm"].encode("utf-8")).hexdigest()[:10]
            for a in c["articles"]:
                total += 1
                aid = article_id(field, a["source"], a["title"])
                if aid in seen:
                    continue
                seen.add(aid)
                pub = a["pub"].astimezone(KST).strftime("%Y-%m-%d %H:%M") if a.get("pub") else ""
                link = c["url"] if (c.get("url") and a is c["articles"][0]) else a.get("link", "")
                rows.append({
                    "id": aid, "first_seen": f"{now:%Y-%m-%d}", "pub": pub, "field": field,
                    "source": a["source"], "title": a["title"], "link": link,
                    "desc": shorten(a.get("desc", ""), ARCHIVE_DESC_CHARS),
                    "cat": c.get("cat", ""), "cat_how": c.get("cat_how", ""),
                    "cluster": cid, "cluster_size": len(c["articles"]),
                    "in_briefing": 1 if id(c) in chosen_ids else 0,
                    "buzz": c.get("buzz", ""),
                    "sec": "|".join((a.get("cat_raw") or a.get("sec_raw") or [])[:3])[:60],   # 신문사 섹션명(기획·오피니언 구분용)
                })
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    if os.path.exists(cur_path):        # 예전 파일에 'sec' 칸이 없으면 칸을 추가해 다시 저장 (기존 내용은 그대로)
        with open(cur_path, encoding="utf-8-sig", newline="") as f:
            old = list(csv.DictReader(f))
            hdr = old and list(old[0].keys())
        if old and "sec" not in hdr:
            with open(cur_path, "w", encoding="utf-8-sig", newline="") as f:
                w0 = csv.DictWriter(f, fieldnames=ARCHIVE_COLS, extrasaction="ignore", restval="")
                w0.writeheader()
                w0.writerows(old)
    is_new = not os.path.exists(cur_path)
    # 새 파일만 BOM(utf-8-sig)을 붙여 엑셀에서 한글이 깨지지 않게 함 (이어쓰기는 BOM 없이)
    with open(cur_path, "w" if is_new else "a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ARCHIVE_COLS)
        if is_new:
            w.writeheader()
        w.writerows(rows)
    secs = collections.Counter((r["source"], r["sec"] or "(없음)") for r in rows if r["source"] in ("보험저널", "보험매일", "한국보험신문"))
    for (s_, sec_), n_ in secs.most_common(12):
        print(f"[섹션진단] {s_} · {sec_} · {n_}건")
    print(f"[누적저장] 신규 {len(rows)}건 저장 (이번 수집 {total}건 중 기존 {total - len(rows)}건은 중복) → {cur_path}")


# ───────────────────────── 포털 연동 (메일과 같은 내용을 웹 포털에도 올림) ─────────────────────────
def export_portal(sections):
    """오늘 브리핑에 실린 기사를 포털(Cloudflare)로 보냅니다. PORTAL_URL/SYNC_TOKEN이 없으면 건너뜀. 실패해도 메일 발송은 계속."""
    url = (os.getenv("PORTAL_URL") or "").strip().rstrip("/")
    tok = (os.getenv("SYNC_TOKEN") or "").strip()
    if not (url and tok):
        print("[포털] PORTAL_URL/SYNC_TOKEN 이 없어 건너뜁니다")
        return False
    day = datetime.now(KST).strftime("%Y-%m-%d")
    items = []
    for key, top in sections.items():
        fld, cat = (key.split(" · ", 1) + [""])[:2] if " · " in key else (key, "")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            pub = a["pub"].astimezone(KST).strftime("%Y-%m-%d %H:%M") if a.get("pub") else day
            srcs = sorted({x["source"] for x in c["articles"]})
            items.append({
                "id": article_id(fld, a["source"], a["title"]) + "_" + day.replace("-", ""),
                "day": day, "field": fld, "cat": cat or "기타", "rank": i,
                "title": a["title"], "link": c.get("url", a["link"]),
                "source": srcs[0] if srcs else a["source"], "sources": srcs,
                "summary": c.get("summary", "") or "", "buzz": c.get("buzz", 0) or 0, "pub": pub,
                "sec": "|".join((a.get("cat_raw") or a.get("sec_raw") or [])[:3])[:60],
            })
    try:
        for k in range(0, len(items), 200):
            r = requests.post(url + "/api/sync/news", json={"items": items[k:k + 200]}, timeout=60,
                              headers={"Authorization": "Bearer " + tok, "User-Agent": "insurance-radar-sync/1.0"})
            if r.status_code != 200:
                print(f"[포털 실패] {r.status_code} {r.text[:200]}")
                return False
        print(f"[포털] 오늘 브리핑 {len(items)}건을 포털에 올렸습니다")
        return True
    except Exception as ex:
        print(f"[포털 실패] {ex}")
        return False


# ───────────────────────── main ─────────────────────────
def collect_field(field, srcs):
    items, excluded = [], 0
    for name, domain in srcs.items():
        try:
            got, how = fetch_source(name, domain, HOURS)
        except Exception as ex:
            print(f"[오류] {name}: {ex}", file=sys.stderr)
            got, how = [], "오류"
        note = ""
        if field == "보험" and name in GENERAL_FINANCE:
            before = len(got)
            got = [g for g in got if any(k in (g["title"] + " " + g.get("desc", "")) for k in INSURANCE_KEYWORDS)]
            note = f" (보험 관련만 {len(got)}/{before})"
        if field == "보험":
            before = len(got)
            got = [g for g in got if not is_off_topic_insurance(g["title"])]
            if before != len(got):
                note += f" (타 업권 기사 {before - len(got)}건 제외)"
        got, ex_n = filter_excluded(got)
        excluded += ex_n
        print(f"[{field}] {name:<10} ({domain}): {len(got)}건 · {how}{note}")
        items += got
        time.sleep(0.5)
    return items, excluded


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="신문사별 수집 건수만 출력")
    ap.add_argument("--sections", action="store_true", help="신문사별 원본 섹션명 진단만 출력(발송 안 함)")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않음")
    args = ap.parse_args()

    sections = {}
    all_clusters, chosen_ids = {}, set()      # 누적 저장용
    primary_all = []                          # 2차 수집이 1차와 겹침을 가릴 때 쓰는 1차 기사 전체
    for field, srcs in SOURCES.items():
        items, excluded = collect_field(field, srcs)
        primary_all += items
        if args.check:
            continue
        if excluded:
            print(f"[{field}] 홍보·단신성 제목 제외 {excluded}건")
        clusters = cluster(items)
        print(f"[{field}] 중복 병합 후 {len(clusters)}개 이슈 (기사 {len(items)}건)")
        all_clusters[field] = clusters

        if LAYOUT == "category" or args.sections:
            assign_categories(clusters, field)
            print_diagnostics(field, items, clusters)
            if args.sections:
                continue
            selected = select_by_category(clusters, field, PER_CATEGORY)
            chosen = [c for _, top in selected for c in top]
            chosen_ids |= {id(c) for c in chosen}
            for c in chosen:
                summarize(c)
            drop_repeated_summaries(chosen)
            ok = sum(1 for c in chosen if c.get("summary"))
            print(f"[{field}] 요약 성공 {ok}/{len(chosen)}")
            for cat, top in selected:
                sections[f"{field} · {cat}"] = top
        else:
            buzz_top, recent_top = rank(clusters, TOP_N, field)
            chosen_ids |= {id(c) for c in buzz_top + recent_top}
            for c in buzz_top + recent_top:
                summarize(c)
            drop_repeated_summaries(buzz_top + recent_top)
            ok = sum(1 for c in buzz_top + recent_top if c.get("summary"))
            print(f"[{field}] 요약 성공 {ok}/{len(buzz_top) + len(recent_top)}")
            if buzz_top:
                sections[f"{field} 화제성"] = buzz_top
            if recent_top:
                sections[f"{field} 최신"] = recent_top

    if args.check:
        print("\n'수집실패'인 신문사는 SOURCES의 도메인을 확인해 수정하세요.")
        return
    if args.sections:
        print("\n섹션명이 '(섹션명 없음)'인 신문사는 키워드 자동분류가 적용됩니다. "
              "위 섹션명을 보고 CATEGORY_MAP 키워드를 보강하세요.")
        return

    if extra_news is not None:      # 2차 수집: 지정 신문사 밖 기사를 '기타 매체'로 보완 (실패해도 1차는 그대로)
        try:
            extra_sections, note, warn = extra_news.run(
                nb=sys.modules[__name__], primary_items=primary_all, hours=HOURS,
                mark_seen=(not args.dry_run) and os.getenv("SEND_MAIL", "1") != "0")
            sections.update(extra_sections)
            RUN_INFO.update(note=note, warn=warn)
        except Exception as ex:
            RUN_INFO.update(note=f"2차 수집 오류: {ex}", warn=True)
            print(f"[2차] 오류로 건너뜀: {ex}", file=sys.stderr)

    text_body, _ = build(sections)
    html_body = build_email_html(sections)
    with open("briefing.html", "w", encoding="utf-8") as f:
        f.write(html_body)
    print("\n" + text_body)
    if args.dry_run:
        return
    export_portal(sections)       # 메일 발송 생략(SEND_MAIL=0)이어도 포털에는 올립니다. 포털 문제로 메일이 막히지 않음
    if ARCHIVE_ON:
        try:
            archive_articles(all_clusters, chosen_ids)
        except Exception as ex:       # 저장 실패가 메일 발송을 막지 않도록
            print(f"[누적저장 실패] {ex}")
        try:                          # 오늘 수집분도 포털의 '이슈 판단용 기록'에 매일 보태 둡니다 (실패해도 무관)
            import backfill_portal
            backfill_portal.run(2)
        except Exception as ex:
            print(f"[기록 채우기 건너뜀] {ex}")
    if os.getenv("SEND_MAIL", "1") == "0":      # 수동 테스트 실행: 저장만 하고 발송은 생략
        print("[발송 생략] SEND_MAIL=0 → 메일·이슈·텔레그램을 보내지 않았습니다 (briefing.html과 누적 저장만 수행).")
        return
    sent = [send_telegram(text_body)]
    subject = f"{'⚠ ' if RUN_INFO['warn'] else ''}[브리핑] 보험·의료 뉴스 {datetime.now(KST):%Y-%m-%d}"
    delivered = False
    if os.getenv("APPS_SCRIPT_URL"):
        delivered = send_appsscript(subject, html_body, text_body)
        sent.append(delivered)
    if not delivered:      # Apps Script가 없거나 실패하면 기존 방식으로 대체 발송
        if os.getenv("GITHUB_ISSUE") == "1":
            sent.append(send_github_issue(build_markdown(sections)))
        elif os.getenv("RESEND_API_KEY"):
            sent.append(send_resend(html_body, text_body))
        else:
            sent.append(send_email(html_body, text_body))
    if not any(sent):
        print("발송되지 않아 briefing.html만 저장했습니다.")


if __name__ == "__main__":
    main()
