#!/usr/bin/env python3
"""
보험·의료 공공기관 보도자료 브리핑 (무료 도구만 사용)

- 기관별 보도자료 게시판(목록 페이지)을 직접 읽어 '게시일 기준 최근 48시간' 보도자료만 모음
- 금융위원회·금융감독원은 보험 분야만 표시 (제목·담당부서 키워드 → 애매하면 본문 확인)
- 뉴스 브리핑과 같은 표 형식 HTML 메일을 Apps Script로 발송 (보험 동향 탐지와는 별개: 기관별 확인용)
- 기관별 수집 결과(접속·목록 파싱·건수)를 로그와 메일에 표시 → '없음'과 '수집 실패'를 구분

사용:
  python press_briefing.py --check          # 기관별 접속/목록 파싱 결과만 점검 (요약·발송 없음)
  python press_briefing.py --dry-run        # 발송 없이 press_briefing.html 생성
  python press_briefing.py                  # 실제 발송
  --debug-dir debug                         # 기관별 원본 HTML 저장 (게시판 구조 분석용)

환경변수(모두 선택):
  WINDOW_HOURS(기본 48)  MAX_PER_INST(30)  SUMMARY_CHARS(130)  ROW_PAD(4)
  APPS_SCRIPT_URL, APPS_SCRIPT_TOKEN       (뉴스 브리핑과 같은 값)
  PRESS_MAIL_TO                            (보도자료 수신자. 없으면 MAIL_TO의 첫 번째 주소로만 발송)
  PRESS_NOW=2026-11-09T08:10               (시험용 현재시각 고정)
"""
import argparse
import collections
import concurrent.futures
import html
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone, time as dtime
from urllib.parse import urljoin

import requests
from lxml import html as LH

# ───────────────────────── 설정 ─────────────────────────
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "48") or "48")
MAX_PER_INST = int(os.getenv("MAX_PER_INST", "30") or "30")
SUMMARY_CHARS = int(os.getenv("SUMMARY_CHARS", "130") or "130")
ROW_PAD = int(os.getenv("ROW_PAD", "4") or "4")
BUILD = "press-v1.0"

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# 엑셀 목록(12개 기관). insurance_only=True 인 기관은 보험 분야 보도자료만 표시.
# link_fmt: 목록의 링크가 javascript:함수('번호') 형태일 때 상세 주소를 만드는 틀 ({id} 자리에 번호)
INSTITUTIONS = [
    dict(field="보험", name="금융위원회", url="https://www.fsc.go.kr/no010101", insurance_only=True),
    dict(field="보험", name="금융감독원", url="https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218",
         insurance_only=True),
    dict(field="보험", name="보험개발원", url="https://www.kidi.or.kr/user/nd11592.do",
         link_re=r"goBoardView\('[^']*',\s*'View',\s*'(\w+)'",
         link_fmt="https://www.kidi.or.kr/user/nd11592.do?View&boardNo={0}"),
    dict(field="보험", name="생명보험협회", url="https://www.klia.or.kr/board/2/list.do",
         link_re=r"fn_goView\('(\d+)'",
         link_fmt="https://www.klia.or.kr/board/2/view.do?boardNo={0}&boardMngNo=2&seq=1"),
    dict(field="보험", name="손해보험협회", url="https://www.knia.or.kr/data/news"),
    dict(field="보험", name="신용정보원",
         url="https://www.kcredit.or.kr:1441/notice/pressRelease.do?menuNo=320&hpBoardSn=PRESS&link=notice%2FpressRelease.do",
         warmup="https://www.kcredit.or.kr:1441/"),
    dict(field="의료", name="보건복지부", url="https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027",
         link_fmt="https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027&act=view&list_no={id}"),
    dict(field="의료", name="건강보험심사평가원", url="https://www.hira.or.kr/bbsDummy.do?pgmid=HIRAA020041000100"),
    dict(field="의료", name="국민건강보험공단", url="https://www.nhis.or.kr/nhis/together/wbhaea01600m01.do"),
    dict(field="의료", name="국민장기요양보험",
         url="https://www.longtermcare.or.kr/npbs/d/m/000/moveBoardView?menuId=npe0000002787&bKey=B0010"),
    dict(field="의료", name="질병관리청",
         url="https://www.kdca.go.kr/kdca/2848/subview.do?enc=Zm5jdDF8QEB8JTJGYmJzJTJGa2RjYSUyRjQyJTJGYXJ0Y2xMaXN0LmRvJTNG",
         link_re=r"jf_viewArtcl\('(\w+)'(?:%20|\s|,|')+(\d+)(?:%20|\s|,|')+(\d+)",
         link_fmt="https://www.kdca.go.kr/bbs/{0}/{1}/{2}/artclView.do"),
    dict(field="의료", name="식품의약품안전처", url="https://www.mfds.go.kr/brd/m_99/list.do"),
]

# 보험 분야 판별 키워드 (금융위·금감원용)
INS_RE = re.compile(r"보험|손보|생보|지급여력|K-?ICS|IFRS\s?17|설계사|실손|(?<![A-Za-z])GA(?![A-Za-z])")


# ───────────────────────── 공통 도구 ─────────────────────────
def now_kst() -> datetime:
    v = os.getenv("PRESS_NOW", "").strip()
    if v:
        return datetime.fromisoformat(v).replace(tzinfo=KST)
    return datetime.now(KST)


def sp(s) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def shorten(text: str, n: int) -> str:
    text = sp(text)
    if len(text) <= n:
        return text
    cut = text[:n]
    m = max(cut.rfind(". "), cut.rfind("다 "), cut.rfind(" "))
    if m >= n * 0.6:
        cut = cut[:m]
    return cut.rstrip(" ,.·") + "…"


def decode(resp) -> str:
    raw = resp.content
    enc = None
    ct = (resp.headers.get("content-type") or "").lower()
    m = re.search(r"charset=([\w\-]+)", ct)
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(rb"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", raw[:6000], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    enc = enc or "utf-8"
    try:
        txt = raw.decode(enc, errors="replace")
    except LookupError:
        txt = raw.decode("utf-8", errors="replace")
    if txt.count("�") > 20 and enc.lower().replace("-", "") not in ("euckr", "cp949"):
        alt = raw.decode("cp949", errors="replace")
        if alt.count("�") < txt.count("�"):
            txt = alt
    return txt


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def http_get(session, url, retries=2, timeout=25):
    """(응답, 오류문구). 일시 오류는 2회까지 재시도. 차단·인증서 오류는 그대로 보고(우회하지 않음)."""
    last = ""
    for i in range(retries + 1):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code >= 500 and i < retries:
                last = f"HTTP {r.status_code}"
                time.sleep(2)
                continue
            return r, ""
        except requests.exceptions.SSLError as ex:
            return None, f"SSL 인증서 오류: {str(ex)[:120]}"
        except requests.exceptions.RequestException as ex:
            last = f"{type(ex).__name__}: {str(ex)[:120]}"
            time.sleep(2)
    return None, last


# ───────────────────────── 목록 파싱 (게시판 구조에 상관없이 동작하는 범용 방식) ─────────────────────────
DATE1 = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
DATE2 = re.compile(r"(?<![\d.])(\d{2})\.(\d{1,2})\.(\d{1,2})(?![\d.])")
TIME_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
FILE_EXT_RE = re.compile(r"\.(hwpx?|pdf|zip|xlsx?|docx?|pptx?)\b", re.I)
BAD_SHORT = ("다운로드", "첨부", "미리보기", "바로보기", "더보기", "이전", "다음", "목록", "처음", "마지막",
             "닫기", "검색", "초기화", "공유", "인쇄", "스크랩", "복사", "맨앞", "맨뒤")
NOISE_TITLE_TAIL = re.compile(r"\s*(?:N|new|NEW|새글|첨부파일\s*있음|파일첨부)\s*$")
NOISE_TITLE_HEAD = re.compile(r"^\s*(?:새글|NEW|new)\s+")


def parse_date(text: str):
    """(date, time|None) 또는 (None, None)."""
    m = DATE1.search(text)
    if not m:
        m2 = DATE2.search(text)
        if not m2:
            return None, None
        y, mo, d = 2000 + int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        end = m2.end()
    else:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        end = m.end()
    try:
        dt = date(y, mo, d)
    except ValueError:
        return None, None
    t = None
    mt = TIME_RE.search(text[end:end + 12])
    if mt:
        t = dtime(int(mt.group(1)), int(mt.group(2)))
    return dt, t


def title_anchor(row):
    best = None
    for a in row.iter("a"):
        href = (a.get("href") or "").strip()
        txt = sp(a.text_content()) or sp(a.get("title"))
        if not txt or len(re.sub(r"[\W_]", "", txt)) < 4:
            continue
        if href.lower().startswith(("mailto:", "tel:")):
            continue
        if FILE_EXT_RE.search(txt):
            continue
        if len(txt) <= 8 and any(b in txt for b in BAD_SHORT):
            continue
        if re.search(r"(?i)(download|filedown)", href):
            continue
        if best is None or len(txt) > len(best[1]):
            best = (a, txt)
    return best


def row_link(a, page_url, link_fmt=None, link_re=None):
    """(링크, 목록주소로 대체했는지)."""
    href = (a.get("href") or "").strip()
    onclick = a.get("onclick") or ""
    if link_fmt and link_re:
        m0 = re.search(link_re, href + " " + onclick)
        if m0:
            return link_fmt.format(*m0.groups()), False
    if href and not href.lower().startswith(("javascript:", "#")):
        return urljoin(page_url, href), False
    m = re.search(r"\(\s*['\"]?([A-Za-z0-9_\-]{3,})['\"]?", href + " " + onclick)
    if link_fmt and m:
        return link_fmt.format(id=m.group(1)), False
    return page_url, True


DEPT_RE = re.compile(r"^[가-힣A-Za-z0-9·\s]{2,22}(?:국|과|팀|실|부|센터|본부|단|처|담당관|연구소|연구원|사업부)$")


def extract_rows(doc, page_url, link_fmt=None, link_re=None):
    cands = []
    for el in doc.iter("tr", "li", "dl", "div", "article"):
        txt = el.text_content()
        if len(txt) > 3000:
            continue
        d, _ = parse_date(txt)
        if d is None:
            continue
        ta = title_anchor(el)
        if ta is None:
            continue
        cands.append((el, ta))
    # 가장 안쪽 행만 사용 (바깥 래퍼 제거)
    has_inner = set()
    for el, _ in cands:
        for anc in el.iterancestors():
            has_inner.add(anc)
    leaves = [(el, ta) for el, ta in cands if el not in has_inner]
    # 같은 부모 아래 나란히 반복되는 행만 '게시판 목록'으로 인정 (푸터·사이드 잡음 제거)
    groups = collections.defaultdict(list)
    for el, ta in leaves:
        groups[(id(el.getparent()), el.tag)].append((el, ta))
    if groups:
        biggest = max(len(v) for v in groups.values())
        keep = [x for v in groups.values() if len(v) >= max(2, biggest // 2) for x in v]
        leaves = keep or leaves
    rows, seen = [], set()
    for el, (a, title) in leaves:
        title = NOISE_TITLE_HEAD.sub("", NOISE_TITLE_TAIL.sub("", title))
        d, t = parse_date(el.text_content())
        key = (title, d)
        if key in seen:
            continue
        seen.add(key)
        link, is_list = row_link(a, page_url, link_fmt, link_re)
        cells = [sp(c.text_content()) for c in el.iter("td")] if el.tag == "tr" else \
                [sp(c.text_content()) for c in el.iterchildren()]
        dept = next((c for c in cells if c and c != title and title not in c and DEPT_RE.match(c)), "")
        rows.append(dict(title=title, link=link, link_is_list=is_list, date=d, time=t, dept=dept))
    return rows


# ───────────────────────── 기간 필터 · 보험 분야 판별 ─────────────────────────
def in_window(item, now, hours):
    d = item["date"]
    cutoff = now - timedelta(hours=hours)
    if item.get("time"):
        dt = datetime.combine(d, item["time"], tzinfo=KST)
        return cutoff <= dt <= now + timedelta(hours=1)
    return cutoff.date() <= d <= now.date()      # 게시일만 있으면 날짜 기준(경계일 포함)


def rel_label(d: date, today: date) -> str:
    n = (today - d).days
    return {0: "오늘", 1: "어제", 2: "그제"}.get(n, f"{n}일 전" if n > 0 else "")


def is_insurance_quick(item) -> bool:
    return bool(INS_RE.search(item["title"])) or "보험" in (item.get("dept") or "")


# ───────────────────────── 본문(상세 페이지)에서 요약·담당부서 추출 ─────────────────────────
SKIP_LINE = ("담당부서", "담당 부서", "책임자", "담당자", "문의", "연락처", "첨부", "목록", "이전글", "다음글",
             "보도시점", "배포", "참고", "붙임", "글자크기", "글씨 크기", "공유", "인쇄", "스크랩", "저작권", "ⓒ",
             "만족도", "페이지 상단", "개인정보", "로그인", "조회수", "작성자", "등록일", "게시일")
BULLETS = "□○ㅇ▪▫■◆◇●·•※-–— ​"


def body_text(page: str) -> str:
    txt = ""
    try:
        import trafilatura
        txt = trafilatura.extract(page) or ""
    except Exception:
        txt = ""
    if len(txt) < 60:
        try:
            doc = LH.fromstring(page)
            for bad in doc.xpath("//script|//style|//nav|//header|//footer|//noscript"):
                bad.drop_tree()
            best = ""
            for el in doc.xpath("//*[@id or @class]"):
                key = ((el.get("id") or "") + " " + (el.get("class") or "")).lower()
                if any(k in key for k in ("view", "content", "cont", "article", "body", "txt", "detail")):
                    t = "\n".join(x.strip() for x in el.itertext() if x.strip())
                    if len(best) < len(t) < 20000:
                        best = t
            txt = best
            if len(txt) < 60:
                m = doc.xpath("//meta[@property='og:description' or @name='description']/@content")
                txt = sp(m[0]) if m else txt
        except Exception:
            pass
    return txt or ""


NOISE_PATS = [
    r"작성일\s*\d{4}[-.]\d{1,2}[-.]\d{1,2}(?:\s+\d{1,2}:\d{2})?",
    r"현재\s*페이지의\s*내용에\s*만족하십니까\??",
    r"총\s*5점\s*중\s*\d점\(\d+건\)",
    r"(?:전화|TEL|Tel|연락처)\s*[:：]?\s*[\d\-() ]{7,}",
    r"담당부서\s*[:：]?[^\n]{0,30}",
    r"조회수?\s*[:：]?\s*\d+",
    r"(?:^|\s)(?:새글|NEW)(?=\s|$)",
]


def clean_noise(text: str, title: str) -> str:
    t = text or ""
    if title:
        t = t.replace(title, " ")
    for p in NOISE_PATS:
        t = re.sub(p, " ", t)
    return t


def make_summary(text: str, title: str) -> str:
    text = clean_noise(text, title)
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip(BULLETS).strip()
        if len(ln) < 12 or any(k in ln for k in SKIP_LINE) or ln == title:
            continue
        lines.append(ln)
        if sum(len(x) for x in lines) >= SUMMARY_CHARS + 40:
            break
    return shorten(" ".join(lines), SUMMARY_CHARS)


def detail_info(session, url):
    """상세 페이지 → (본문 텍스트, 담당부서, 오류)."""
    r, err = http_get(session, url, retries=1, timeout=20)
    if r is None or r.status_code != 200:
        return "", "", err or f"HTTP {getattr(r, 'status_code', '?')}"
    page = decode(r)
    txt = body_text(page)
    plain = sp(LH.fromstring(page).text_content()) if page.strip() else ""
    m = re.search(r"담당\s*부서\s*[:：]?\s*([가-힣A-Za-z0-9·\s]{2,25}?)(?:\s{2,}|\s*(?:책임자|담당자|전화|연락처)|$)", plain)
    dept = sp(m.group(1)) if m else ""
    return txt, dept, ""


# ───────────────────────── 기관별 수집 ─────────────────────────
def fetch_institution(inst, debug_dir=None):
    st = dict(name=inst["name"], field=inst["field"], insurance_only=bool(inst.get("insurance_only")),
              ok=False, http=None, kb=0, rows=0, newest=None, total_window=0, error="", note="")
    items = []
    session = make_session()
    try:
        if inst.get("warmup"):
            http_get(session, inst["warmup"], retries=0, timeout=20)
        r, err = http_get(session, inst["url"])
    except Exception as ex:                      # 예기치 못한 오류도 기관 하나로 한정
        r, err = None, f"{type(ex).__name__}: {str(ex)[:120]}"
    if r is None:
        st["error"] = f"접속 실패 ({err})"
        return st, items, session
    st["http"] = r.status_code
    st["kb"] = round(len(r.content) / 1024)
    if r.status_code != 200:
        st["error"] = f"HTTP {r.status_code} 응답 (접근 차단 또는 주소 변경 가능성)"
        return st, items, session
    page = decode(r)
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, f"{inst['name']}.html"), "w", encoding="utf-8") as f:
            f.write(page)
    try:
        doc = LH.fromstring(page)
    except Exception as ex:
        st["error"] = f"HTML 해석 실패 ({ex})"
        return st, items, session
    rows = extract_rows(doc, r.url or inst["url"], inst.get("link_fmt"), inst.get("link_re"))
    st["rows"] = len(rows)
    if not rows:
        visible = sp(doc.text_content())[:200]
        n_tr, n_li, n_a = len(doc.xpath("//tr")), len(doc.xpath("//li")), len(doc.xpath("//a"))
        dates = len(DATE1.findall(page))
        st["error"] = (f"목록을 찾지 못함 (tr {n_tr} · li {n_li} · 링크 {n_a} · 날짜표기 {dates}건). "
                       + ("자바스크립트로 목록을 그리는 페이지이거나 차단 안내 페이지일 수 있음" if dates == 0 else "목록 구조 확인 필요"))
        st["note"] = f"화면 텍스트: {visible}"
        return st, items, session
    st["ok"] = True
    st["newest"] = max(x["date"] for x in rows)
    for it in rows:
        it["source"] = inst["name"]
        items.append(it)
    return st, items, session


def select_window(items, now):
    win = [it for it in items if in_window(it, now, WINDOW_HOURS)]
    order = {id(it): i for i, it in enumerate(items)}
    win.sort(key=lambda x: (-x["date"].toordinal(), order[id(x)]))
    return win[:MAX_PER_INST]


def apply_insurance_filter(inst, st, win, session, check_only=False):
    """금융위·금감원: 보험 분야만. 제목/담당부서 → 애매한 건은 본문 확인."""
    kept, undecided, dropped = [], [], []
    for it in win:
        (kept if is_insurance_quick(it) else undecided).append(it)
    if undecided and not check_only:
        for it in undecided:
            txt, dept, _ = detail_info(session, it["link"]) if not it["link_is_list"] else ("", "", "")
            it["_detail"] = (txt, dept)
            if "보험" in dept:
                it["dept"] = it.get("dept") or dept
                it["basis"] = "담당부서"
                kept.append(it)
            elif len(re.findall("보험", (it["title"] + " " + txt)[:2500])) >= 3:
                it["basis"] = "본문 키워드"
                kept.append(it)
            else:
                dropped.append(it)
            time.sleep(0.5)
    else:
        dropped = undecided
    kept.sort(key=lambda x: (-x["date"].toordinal(), win.index(x)))
    return kept, dropped


def fill_summaries(items, sessions):
    def work(it):
        pre = it.get("_detail")
        if pre:
            txt, dept = pre
        elif it["link_is_list"]:
            return
        else:
            txt, dept, _ = detail_info(sessions[it["source"]], it["link"])
        if dept and not it.get("dept"):
            it["dept"] = dept
        it["summary"] = make_summary(txt, it["title"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, items))
    # 사이트 공통 문구가 요약으로 잡힌 경우 제거 (서로 다른 보도자료인데 요약 앞부분이 같음)
    cnt = collections.Counter((it.get("summary") or "")[:30] for it in items if it.get("summary"))
    for it in items:
        if it.get("summary") and cnt[it["summary"][:30]] >= 2:
            it["summary"] = ""


# ───────────────────────── 메일 (뉴스 브리핑과 같은 형식) ─────────────────────────
def build_email_html(results, now) -> str:
    esc = html.escape
    wd = "월화수목금토일"[now.weekday()]
    today = now.date()
    counts = collections.OrderedDict()
    for r in results:
        counts[r["inst"]["field"]] = counts.get(r["inst"]["field"], 0) + len(r["items"])
    ok_n = sum(1 for r in results if r["st"]["ok"])
    count_txt = " · ".join(f"{f} {n}건" for f, n in counts.items())
    failed = [r["inst"]["name"] for r in results if not r["st"]["ok"]]

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
        "<div style='font-size:21px;font-weight:bold;'>🏛️ 보험·의료 공공기관 보도자료</div>"
        f"<div style='font-size:13px;color:#cfd9e8;margin-top:6px;'>{now:%Y.%m.%d} ({wd}) 기준 · 최근 {WINDOW_HOURS}시간 · "
        f"{esc(count_txt)} · 수집 {ok_n}/{len(results)}개 기관</div></td></tr>",
        "<tr><td style='padding:12px 24px 2px;font-size:14px;color:#333;line-height:1.5;'>"
        f"안녕하세요. 최근 {WINDOW_HOURS}시간 이내(게시일 기준) 보험·의료 공공기관 보도자료입니다. 제목을 누르면 원문으로 이동합니다."
        "</td></tr>",
    ]
    cur_field = None
    for r in results:
        inst, st, items = r["inst"], r["st"], r["items"]
        if inst["field"] != cur_field:
            out.append(
                f"<tr><td style='padding:16px 24px 0;'><div style='font-size:18px;font-weight:bold;color:#1f3a5f;"
                f"border-bottom:2px solid #1f3a5f;padding-bottom:8px;'>{esc(inst['field'])}</div></td></tr>")
            cur_field = inst["field"]
        sub = f"({len(items)}건)"
        if inst.get("insurance_only") and st["ok"]:
            sub = f"(보험 {len(items)}건 · 전체 {st['total_window']}건 중)"
        out.append(
            f"<tr><td style='padding:12px 24px 4px;font-size:15px;font-weight:bold;color:#222;'>"
            f"▌{esc(inst['name'])} <span style='font-weight:normal;color:#888;font-size:13px;'>{esc(sub)}</span></td></tr>")
        if not st["ok"]:
            out.append(
                "<tr><td style='padding:0 24px 6px;'><div style='font-size:12.5px;color:#92400e;background:#fffbeb;"
                f"border:1px solid #fde68a;padding:6px 10px;line-height:1.4;'>⚠ 수집 실패 — {esc(st['error'])}</div></td></tr>")
            continue
        if not items:
            out.append(
                f"<tr><td style='padding:0 24px 6px;font-size:12.5px;color:#888;'>최근 {WINDOW_HOURS}시간 이내 새 보도자료 없음"
                f"{' (보험 분야 기준)' if inst.get('insurance_only') else ''}</td></tr>")
            continue
        rows = []
        for i, it in enumerate(items, 1):
            lab = rel_label(it["date"], today)
            meta = [esc(inst["name"]), f"{it['date']:%Y-%m-%d}" + (f"({lab})" if lab else "")]
            if it.get("dept"):
                meta.append(esc(it["dept"]))
            if it.get("basis"):
                meta.append(f"보험 분야 판단: {esc(it['basis'])}")
            summ = (f"<div style='font-size:12.5px;color:#444;margin-top:1px;line-height:1.35;'>{esc(it['summary'])}</div>"
                    if it.get("summary") else "")
            rows.append(
                "<tr>"
                f"<td align='center' valign='top' style='width:36px;padding:{ROW_PAD}px 2px;border:1px solid #dde2e8;"
                f"font-size:13px;line-height:1.3;color:#555;'>{i}</td>"
                f"<td style='padding:{ROW_PAD}px 10px;border:1px solid #dde2e8;font-size:13px;line-height:1.3;'>"
                f"<div style='font-size:14px;line-height:1.3;'><a href='{esc(it['link'], quote=True)}' "
                f"style='font-size:14px;font-weight:bold;color:#1a56db;text-decoration:underline;'>{esc(it['title'])}</a></div>"
                f"{summ}"
                f"<div style='font-size:11.5px;color:#888;margin-top:1px;line-height:1.25;'>{' · '.join(meta)}</div>"
                "</td></tr>")
        out.append(
            "<tr><td style='padding:0 24px;'>"
            "<table width='100%' cellpadding='0' cellspacing='0' border='0' style='border-collapse:collapse;'>"
            "<tr style='background:#eef1f6;'>"
            "<th style='width:36px;padding:4px;border:1px solid #dde2e8;font-size:13px;color:#333;'>No</th>"
            "<th style='padding:4px;border:1px solid #dde2e8;font-size:13px;color:#333;'>보도자료</th></tr>"
            + "".join(rows) + "</table></td></tr>")
    foot = ("이 메일은 자동으로 발송되었습니다. 게시일 기준 최근 "
            f"{WINDOW_HOURS}시간 이내 보도자료이며, 금융위원회·금융감독원은 보험 분야만 표시합니다. "
            "요약문은 보도자료 본문 앞부분을 바탕으로 만들어져 원문과 다를 수 있으니 중요한 내용은 원문을 확인하세요.")
    if failed:
        foot += f" <b style='color:#92400e;'>수집 실패 기관: {esc(', '.join(failed))}</b>"
    out.append("<tr><td style='padding:14px 24px;font-size:12px;color:#888;line-height:1.5;border-top:1px solid #eee;'>"
               f"{foot} <span style='color:#bbb;'>[{BUILD}]</span></td></tr>")
    out.append("</table></td></tr></table></body></html>")
    return "".join(out)


def build_text(results, now) -> str:
    lines = [f"🏛️ 보험·의료 공공기관 보도자료 {now:%Y-%m-%d} (최근 {WINDOW_HOURS}시간)\n"]
    for r in results:
        inst, st, items = r["inst"], r["st"], r["items"]
        lines.append(f"━━ [{inst['field']}] {inst['name']} ({len(items)}건) ━━")
        if not st["ok"]:
            lines.append(f"   ⚠ 수집 실패: {st['error']}")
        elif not items:
            lines.append("   (새 보도자료 없음)")
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}  [{it['date']:%Y-%m-%d}{' · ' + it['dept'] if it.get('dept') else ''}]")
            if it.get("summary"):
                lines.append(f"   └ {it['summary']}")
            lines.append(f"   {it['link']}")
        lines.append("")
    return "\n".join(lines)


# ───────────────────────── 발송 (뉴스 브리핑과 같은 Apps Script) ─────────────────────────
def recipients():
    raw = (os.getenv("PRESS_MAIL_TO") or "").strip()
    if not raw:                                   # 시험 기간: MAIL_TO의 첫 번째 주소(본인)에게만
        raw = ((os.getenv("MAIL_TO") or "").replace(";", ",").split(",") or [""])[0]
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def send_appsscript(subject, html_body, text_body) -> bool:
    url = (os.getenv("APPS_SCRIPT_URL") or "").strip()
    token = (os.getenv("APPS_SCRIPT_TOKEN") or "").strip()
    to = recipients()
    if not (url and token and to):
        print("[Apps Script 미발송] APPS_SCRIPT_URL / APPS_SCRIPT_TOKEN / 수신 주소(PRESS_MAIL_TO 또는 MAIL_TO) 중 비어 있는 값이 있습니다.")
        return False
    try:
        r = requests.post(url, json={"token": token, "to": to, "subject": subject,
                                     "html": html_body, "text": text_body}, timeout=90, allow_redirects=True)
        try:
            res = r.json()
        except Exception:
            print(f"[Apps Script 실패] JSON 응답이 아님 (HTTP {r.status_code}). 웹 앱 액세스 권한이 '모든 사용자'인지 확인하세요.")
            return False
        if res.get("ok"):
            print(f"[Apps Script 발송 완료] {res.get('sent')}명 → {', '.join(to)}"
                  + (f" (실패: {res.get('failed')})" if res.get("failed") else ""))
            return True
        print(f"[Apps Script 실패] {res}")
    except Exception as ex:
        print(f"[Apps Script 실패] {ex}")
    return False


# ───────────────────────── main ─────────────────────────
def status_line(st, n_win=None, n_show=None) -> str:
    head = f"[{st['field']}] {st['name']:<10}"
    if not st["ok"]:
        return f"{head} ⚠ {st['error']}"
    s = f"{head} HTTP {st['http']} · {st['kb']}KB · 목록 {st['rows']}행 · 최신 게시일 {st['newest']}"
    if n_win is not None:
        s += f" · 최근 {WINDOW_HOURS}시간 {n_win}건"
    if n_show is not None and st["insurance_only"]:
        s += f" → 보험 {n_show}건"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="기관별 접속·목록 파싱 결과만 점검")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 press_briefing.html만 생성")
    ap.add_argument("--debug-dir", default="", help="기관별 원본 목록 HTML 저장 폴더")
    args = ap.parse_args()

    now = now_kst()
    print(f"[시작] {now:%Y-%m-%d %H:%M} KST · 게시일 기준 최근 {WINDOW_HOURS}시간 · {BUILD}")

    fetched = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {inst["name"]: ex.submit(fetch_institution, inst, args.debug_dir or None) for inst in INSTITUTIONS}
        for inst in INSTITUTIONS:
            fetched[inst["name"]] = futs[inst["name"]].result()

    results, sessions, all_shown = [], {}, []
    for inst in INSTITUTIONS:
        st, items, session = fetched[inst["name"]]
        sessions[inst["name"]] = session
        win = select_window(items, now) if st["ok"] else []
        st["total_window"] = len(win)
        shown, dropped = win, []
        if st["ok"] and inst.get("insurance_only"):
            shown, dropped = apply_insurance_filter(inst, st, win, session, check_only=args.check)
        print(status_line(st, len(win) if st["ok"] else None, len(shown)))
        if st.get("note"):
            print(f"      {st['note']}")
        for d in dropped:
            print(f"      · 보험 외로 제외: {d['title']} ({d['date']})")
        results.append(dict(inst=inst, st=st, items=shown))
        all_shown += shown

    if args.check:
        bad = [r["inst"]["name"] for r in results if not r["st"]["ok"]]
        print(f"\n[점검 결과] 성공 {len(results) - len(bad)}/{len(results)}개 기관"
              + (f" · 실패: {', '.join(bad)}" if bad else ""))
        return

    fill_summaries(all_shown, sessions)
    html_body = build_email_html(results, now)
    text_body = build_text(results, now)
    with open("press_briefing.html", "w", encoding="utf-8") as f:
        f.write(html_body)
    print("\n" + text_body)
    if args.dry_run:
        return
    subject = f"[보도자료] 보험·의료 공공기관 {now:%Y-%m-%d}"
    if not send_appsscript(subject, html_body, text_body):
        print("발송되지 않아 press_briefing.html만 저장했습니다.")


if __name__ == "__main__":
    main()
