#!/usr/bin/env python3
"""
2차 수집: 네이버 뉴스 검색 API(NAVER API HUB)로 '지정 신문사 목록 밖' 기사를 보완한다.

동작 요약
  1) 1차(news_briefing.py가 수집한 지정 신문사 기사)는 그대로 둔다.
  2) 네이버 뉴스 검색으로 최근 48시간 기사를 모은 뒤, 아래에 해당하면 버린다.
       - 홍보·단신성 제목 / 제외어 / 보험·의료와 무관한 기사
       - 이미 '기타 매체'로 한 번 게재한 기사 (data/extra_seen.json)
       - 1차 기사와 같은 이슈 (오늘 1차 + 최근 3일간 1차 기사 data/extra_primary.json 과 비교)
  3) 남은 기사를 '기타 매체 · 보험', '기타 매체 · 의료' 섹션으로 덧붙인다.
  4) 메일 상단에 수집 현황 한 줄을 넣고, 이상이 있으면 제목에 ⚠ 를 붙인다.

이 모듈이 실패해도 1차 브리핑은 정상 발송되도록 호출하는 쪽(main)에서 try/except로 감싼다.
저장하는 것은 기사 주소의 해시와, 1차 기사(이미 직접 수집한 기사)의 제목·짧은 설명뿐이다.
네이버 검색 결과(기사 본문·설명)는 저장하지 않는다.

필요한 환경변수(GitHub Secrets)
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET   (NAVER API HUB 애플리케이션의 인증 정보)

선택 환경변수
  EXTRA_HOURS       조회 범위(시간). 기본 48
  EXTRA_PER_FIELD   분야별 최대 게재 건수. 기본 5 (보험 5 + 의료 5 = 최대 10건)
  EXTRA_PAGES       키워드당 최대 조회 페이지 수. 기본 3 (한 페이지 100건, 범위를 벗어나면 중간에 멈춤)
  MIN_PRIMARY_ITEMS 1차 수집 건수가 이 값보다 적으면 경고. 기본 15

설정 파일 (저장소 config/ 폴더, GitHub 화면에서 직접 수정 가능)
  extra_keywords.txt  '보험: 검색어' / '의료: 검색어'
  extra_exclude.txt   제목에 있으면 제외할 단어
  extra_domains.txt   '도메인=매체명'

단독 점검:  python extra_news.py     (저장 없이 2차 결과만 출력)
"""
import collections
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse

import requests

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
KST = timezone(timedelta(hours=9))

CONFIG_DIR = os.getenv("EXTRA_CONFIG_DIR", "config")
SEEN_PATH = os.getenv("EXTRA_SEEN_PATH", "data/extra_seen.json")
PRIMARY_PATH = os.getenv("EXTRA_PRIMARY_PATH", "data/extra_primary.json")
KEEP_DAYS = 10            # 게재 기록 보관일
PRIMARY_KEEP_HOURS = 96   # 1차 기사 기억 기간 (조회 범위 48시간보다 넉넉히)

HOURS = int(os.getenv("EXTRA_HOURS", "48") or "48")
PER_FIELD = int(os.getenv("EXTRA_PER_FIELD", "5") or "5")
PAGES = int(os.getenv("EXTRA_PAGES", "3") or "3")
MIN_PRIMARY = int(os.getenv("MIN_PRIMARY_ITEMS", "15") or "15")
MAX_SECONDS = 240          # 2차 전체 제한시간(초). 넘으면 그때까지 모은 것만 사용

DEFAULT_KEYWORDS = {
    "보험": [
        "보험사 상품", "보험 신상품", "건강보험 보험사", "GA 보험대리점", "보험설계사",
        "실손보험", "암보험 간병보험", "보험업계 금융당국", "보험 소비자 분쟁", "보험 AI 디지털",
    ],
    "의료": [
        "의료계 병원", "건강보험 수가", "복지부 의료 정책", "제약 바이오 신약",
        "의대 전공의", "필수의료 지역의료", "디지털헬스 의료 AI", "병원 경영",
    ],
}
DEFAULT_EXCLUDE = ["경품", "이벤트 당첨", "쿠폰", "프로야구", "프로축구", "연예", "맛집"]
DEFAULT_DOMAINS = {
    "ddaily.co.kr": "디지털데일리", "etnews.com": "전자신문", "zdnet.co.kr": "지디넷코리아",
    "bloter.net": "블로터", "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스", "news1.kr": "뉴스1",
    "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "sedaily.com": "서울경제",
    "edaily.co.kr": "이데일리", "mt.co.kr": "머니투데이", "fnnews.com": "파이낸셜뉴스",
    "asiae.co.kr": "아시아경제", "heraldcorp.com": "헤럴드경제", "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보", "donga.com": "동아일보", "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보", "kmib.co.kr": "국민일보",
    "segye.com": "세계일보", "seoul.co.kr": "서울신문", "ytn.co.kr": "YTN",
    "insnews.co.kr": "한국보험신문", "insjournal.co.kr": "보험저널", "dailyinsu.com": "보험매일",
    "fntimes.com": "한국금융신문", "kbanker.co.kr": "DFT대한금융신문", "thebell.co.kr": "더벨",
}
# 보험 분야 관련성: 제목에 강한 단어가 있거나, 설명에 '보험'이 2번 이상
INSURANCE_STRONG = ["보험", "손보", "생보", "설계사", "실손", "언더라이팅", "지급여력", "K-ICS", "IFRS17", "재보험"]
MEDICAL_KEYWORDS = [
    "의료", "병원", "의사", "의대", "복지부", "건강보험", "건보", "제약", "바이오", "신약", "수가",
    "환자", "의협", "전공의", "간호", "보건", "진료", "의약", "헬스케어", "의학", "치료제",
]
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "from")


class ApiError(Exception):
    def __init__(self, msg, fatal=False):
        super().__init__(msg)
        self.fatal = fatal


# ───────────── 설정 파일 ─────────────
def _read_lines(name):
    path = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def load_keywords():
    lines = _read_lines("extra_keywords.txt")
    if not lines:
        return DEFAULT_KEYWORDS
    out = {"보험": [], "의료": []}
    for ln in lines:
        if ":" in ln:
            fld, kw = (x.strip() for x in ln.split(":", 1))
            if fld in out and kw:
                out[fld].append(kw)
    return out if any(out.values()) else DEFAULT_KEYWORDS


def load_exclude():
    return _read_lines("extra_exclude.txt") or DEFAULT_EXCLUDE


def load_domains():
    mapping = dict(DEFAULT_DOMAINS)
    for ln in _read_lines("extra_domains.txt") or []:
        if "=" in ln:
            d, n = (x.strip() for x in ln.split("=", 1))
            if d and n:
                mapping[d.lower()] = n
    return mapping


# ───────────── 문자열·주소 ─────────────
def clean(s):
    s = re.sub(r"</?b>", "", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def domain_of(url):
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def media_name(url, mapping):
    d = domain_of(url)
    for dom, name in mapping.items():
        if d == dom or d.endswith("." + dom):
            return name
    return d or "기타"


def norm_url(url):
    p = urlparse(url or "")
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(TRACKING_PARAMS)]
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}?{urlencode(q)}".rstrip("?")


def url_hash(url):
    return hashlib.sha1(norm_url(url).encode("utf-8")).hexdigest()[:16]


def parse_pub(s):
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        return None


# ───────────── 기록 파일 ─────────────
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)


def load_recent_primary(now):
    """최근 며칠간의 1차 기사(어제 브리핑에 이미 나간 이슈)를 불러와 비교 대상에 포함."""
    out = []
    for r in _load_json(PRIMARY_PATH, []):
        try:
            pub = datetime.fromisoformat(r["pub"])
        except Exception:
            continue
        if now - pub <= timedelta(hours=PRIMARY_KEEP_HOURS):
            out.append({"title": r["title"], "desc": r.get("desc", ""), "source": r.get("source", ""),
                        "pub": pub, "link": "", "direct": True, "extra": False, "old": True})
    return out


def save_recent_primary(now, primary_items):
    keep = {}
    for r in _load_json(PRIMARY_PATH, []):
        keep[(r.get("title", ""), r.get("source", ""))] = r
    for a in primary_items:
        pub = a.get("pub") or now
        keep[(a["title"], a["source"])] = {"title": a["title"], "desc": (a.get("desc") or "")[:200],
                                           "source": a["source"], "pub": pub.isoformat()}
    cutoff = now - timedelta(hours=PRIMARY_KEEP_HOURS)
    rows = []
    for r in keep.values():
        try:
            if datetime.fromisoformat(r["pub"]) >= cutoff:
                rows.append(r)
        except Exception:
            pass
    _save_json(PRIMARY_PATH, rows)


# ───────────── API 호출 ─────────────
def fetch_query(session, query, cid, key, cutoff, pages=PAGES, display=100):
    out = []
    for p in range(pages):
        try:
            r = session.get(
                API_URL,
                params={"query": query, "display": display, "start": 1 + p * display,
                        "sort": "date", "format": "json"},
                headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": key},
                timeout=15)
        except requests.RequestException as ex:
            raise ApiError(f"접속 오류({type(ex).__name__})")
        if r.status_code != 200:
            # 401/403 키 오류, 429 한도 초과 → 계속 호출해도 소용없으므로 중단 신호
            raise ApiError(f"HTTP {r.status_code}", fatal=r.status_code in (401, 403, 429))
        try:
            items = r.json().get("items", [])
        except ValueError:
            raise ApiError("응답 형식 오류")
        oldest = None
        for e in items:
            pub = parse_pub(e.get("pubDate", ""))
            if pub is not None and (oldest is None or pub < oldest):
                oldest = pub
            if pub is not None and pub < cutoff:
                continue
            url = e.get("originallink") or e.get("link") or ""
            out.append({"title": clean(e.get("title", "")), "origin": url, "link": url,
                        "desc": clean(e.get("description", "")), "pub": pub,
                        "direct": False, "extra": True})
        if len(items) < display or (oldest is not None and oldest < cutoff):
            break
        time.sleep(0.2)
    return out


def relevant(field, it):
    title, text = it["title"], it["title"] + " " + it["desc"]
    if field == "보험":
        return any(k in title for k in INSURANCE_STRONG) or text.count("보험") >= 2
    return any(k in text for k in MEDICAL_KEYWORDS)


# ───────────── 메인 ─────────────
def run(nb, primary_items, hours, mark_seen=True):
    """반환: (추가할 섹션 dict, 수집 현황 문구, 경고 여부). hours는 호환용(1차 조회 시간)."""
    t0 = time.time()
    now = datetime.now(timezone.utc)
    warn = []

    # 1차 수집 현황 점검 (조용한 실패를 조기에 알아채기 위함)
    per_src = collections.Counter(a["source"] for a in primary_items)
    names = [n for srcs in nb.SOURCES.values() for n in srcs]
    zero = [n for n in names if per_src.get(n, 0) == 0]
    note = f"1차 {len(primary_items)}건" + (f" (0건 매체 {len(zero)}곳: {', '.join(zero)})" if zero else "")
    if len(primary_items) < MIN_PRIMARY:
        warn.append(f"1차 수집이 평소보다 적음({len(primary_items)}건)")
    if len(zero) >= 3:
        warn.append(f"1차 0건 매체 {len(zero)}곳")

    cid = os.getenv("NAVER_CLIENT_ID", "").strip()
    key = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not cid or not key:
        warn.append("2차 미사용: 네이버 API 키 미설정")
        return {}, _fmt(note, warn), True

    cutoff = now - timedelta(hours=HOURS)
    keywords, mapping, exclude = load_keywords(), load_domains(), load_exclude()

    # 조회
    raw, errors, ok, total_q, stop = {}, [], 0, 0, False
    with requests.Session() as sess:
        for field, qs in keywords.items():
            for q in qs:
                if stop or time.time() - t0 > MAX_SECONDS:
                    stop = True
                    break
                total_q += 1
                try:
                    got = fetch_query(sess, q, cid, key, cutoff)
                    ok += 1
                except ApiError as ex:
                    errors.append(f"'{q}' {ex}")
                    stop = ex.fatal
                    continue
                for it in got:
                    it["field"] = field
                    raw.setdefault(norm_url(it["origin"]), it)
                time.sleep(0.3)

    # 걸러내기
    seen = _load_json(SEEN_PATH, {})
    items, _ = nb.filter_excluded(list(raw.values()))
    kept = []
    for it in items:
        if not it["title"] or any(w in it["title"] for w in exclude):
            continue
        if not relevant(it["field"], it) or url_hash(it["origin"]) in seen:
            continue
        it["source"] = media_name(it["origin"], mapping)
        kept.append(it)

    # 1차 기사(오늘 + 최근 며칠)와 같은 이슈는 제외 — 기존 중복 병합 로직 재사용
    past = load_recent_primary(now)
    past_keys = {(a["title"], a["source"]) for a in primary_items}
    past = [a for a in past if (a["title"], a["source"]) not in past_keys]
    clusters = nb.cluster(list(primary_items) + past + kept)
    fresh = [c for c in clusters if all(a.get("extra") for a in c["articles"])]

    def newest(c):
        return max((a["pub"] for a in c["articles"] if a["pub"]), default=nb.EPOCH)

    sections, picked = {}, 0
    today = datetime.now(KST).strftime("%Y-%m-%d")
    for field in keywords:
        pool = sorted((c for c in fresh if c["articles"][0]["field"] == field),
                      key=lambda c: (len({a["source"] for a in c["articles"]}), newest(c)), reverse=True)[:PER_FIELD]
        for c in pool:
            a = c["articles"][0]
            c["url"] = a["link"]
            c["summary"] = nb.shorten(nb.clean_text(a["desc"]), nb.SUMMARY_CHARS)
            for x in c["articles"]:
                seen[url_hash(x["origin"])] = today
        if pool:
            sections[f"기타 매체 · {field}"] = pool
            picked += len(pool)

    if mark_seen:
        try:
            keep_from = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            _save_json(SEEN_PATH, {h: d for h, d in seen.items() if d >= keep_from})
            save_recent_primary(now, primary_items)
        except OSError as ex:
            print(f"[2차] 기록 저장 실패: {ex}", file=sys.stderr)

    note += (f" · 2차(네이버 뉴스, 최근 {HOURS}시간) 조회 {ok}/{total_q}회 성공, 후보 {len(kept)}건 → "
             f"1차와 겹치지 않는 이슈 {len(fresh)}개 중 {picked}건 게재")
    if errors:
        warn.append(f"2차 조회 오류 {len(errors)}건")
        print("[2차] 오류: " + "; ".join(errors), file=sys.stderr)
    if total_q and ok == 0:
        warn.append("2차 조회가 모두 실패")
    if stop and ok and not errors:
        warn.append("2차 제한시간 초과로 일부 키워드 생략")
    print(f"[2차] {_fmt(note, warn)} ({time.time() - t0:.1f}초)")
    return sections, _fmt(note, warn), bool(warn)


def _fmt(note, warn):
    return ("⚠ " + " / ".join(warn) + " — " + note) if warn else note


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    import news_briefing as nb
    secs, status, _ = run(nb, [], nb.HOURS, mark_seen=False)
    print(status)
    for k, cl in secs.items():
        print(f"\n== {k} ==")
        for c in cl:
            a = c["articles"][0]
            print(f"- {a['title']} [{a['source']}]\n  {c['url']}")
