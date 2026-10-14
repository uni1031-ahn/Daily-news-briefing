#!/usr/bin/env python3
"""
보험/의료 뉴스 일일 브리핑 v2 (무료 도구만 사용)

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
import difflib
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

# ───────────────────────── 설정 ─────────────────────────
TOP_N = int(os.getenv("TOP_N", "10") or "10")      # 분야별 상위 기사 수 (10 또는 20)
HOURS = int(os.getenv("HOURS", "24") or "24")      # 최근 몇 시간 기사 수집
SIM_THRESHOLD = 0.8                                # 제목 유사도 기준(0~1)

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
        "메디컬타임즈": "medicaltimes.com",
        "메디게이트뉴스": "medigatenews.com",
        "데일리메디": "dailymedi.com",
        "더메디컬": "themedical.co.kr",
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
]

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ───────────────────────── 수집 ─────────────────────────
def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def to_dt(e):
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def fetch_direct(name: str, domain: str, cutoff):
    """신문사 자체 RSS (많은 국내 인터넷신문이 /rss/allArticle.xml 사용)."""
    for host in (f"https://www.{domain}", f"https://{domain}"):
        try:
            r = requests.get(f"{host}/rss/allArticle.xml", headers=UA, timeout=15)
            if r.status_code != 200 or "<item" not in r.text:
                continue
            feed = feedparser.parse(r.content)
            items = []
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


def tokens(title: str) -> set:
    return {w for w in norm(title).split() if len(w) >= 2}


def similar(n1, t1, n2, t2) -> bool:
    """같은 이슈의 기사인지 판단: (0) 회사명 확인 (1) 문자열 유사도 (2) 핵심 단어 겹침."""
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
    # 긴 공통 단어가 있고, 공통 단어가 3개 이상일 때만 같은 이슈로 판단
    return len(common) >= 3 and overlap >= 0.6 and long_common


def cluster(items):
    """제목이 같거나 같은 이슈로 보이는 기사를 하나로 병합."""
    clusters = []
    for it in sorted(items, key=lambda x: x["pub"] or EPOCH, reverse=True):
        n = norm(it["title"])
        if not n:
            continue
        tk = tokens(it["title"])
        for c in clusters:
            if any(similar(n, tk, m, tm) for m, tm in c["keys"]):
                c["articles"].append(it)
                c["keys"].append((n, tk))
                break
        else:
            clusters.append({"norm": n, "keys": [(n, tk)], "articles": [it]})
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
    try:
        import trafilatura
        r = requests.get(url, headers=UA, timeout=15)
        return trafilatura.extract(r.text) or ""
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
        prompt = (f"다음 기사를 한국어로 2문장 이내(150자 내외)로 핵심만 요약해줘. "
                  f"제목: {title}\n본문: {text[:4000]}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""


def summarize(cl):
    # 직접 링크가 있는 기사를 대표로 우선 사용
    arts = sorted(cl["articles"], key=lambda a: not a.get("direct"))
    a = arts[0]
    url = resolve_url(a["link"])
    cl["url"] = url
    desc = next((x["desc"] for x in arts if len(x.get("desc", "")) > 30), "")
    body = clean_text(fetch_body(url)) or clean_text(desc)
    cl["summary"] = gemini_summary(a["title"], body) or extractive_summary(body) or clean_text(desc)[:240]
    time.sleep(1)


# ───────────────────────── 포맷 ─────────────────────────
def build(sections):
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    text_lines = [f"📰 보험·의료 뉴스 브리핑 {today}\n"]
    html_parts = [f"<h2>📰 보험·의료 뉴스 브리핑 {today}</h2>"]
    for field, top in sections.items():
        text_lines.append(f"━━ {field} TOP {len(top)} ━━")
        html_parts.append(f"<h3>{field} TOP {len(top)}</h3><ol>")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            src_txt = ", ".join(sorted({x["source"] for x in c["articles"]}))
            if c.get("buzz"):
                src_txt += f" · 🔥관련보도 {c['buzz']}곳"
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
    for field, top in sections.items():
        lines.append(f"## {field} TOP {len(top)}\n")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            src_txt = ", ".join(sorted({x["source"] for x in c["articles"]}))
            if c.get("buzz"):
                src_txt += f" · 🔥관련보도 {c['buzz']}곳"
            link = c.get("url", a["link"])
            title = a["title"].replace("[", "\\[").replace("]", "\\]")
            lines.append(f"{i}. **[{title}]({link})** — _{src_txt}_")
            if c.get("summary"):
                lines.append(f"   > {c['summary']}")
        lines.append("")
    return "\n".join(lines)[:60000]


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
        # 이슈가 쌓이지 않도록 바로 닫기 (알림 메일은 생성 시점에 이미 발송됨)
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


# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="신문사별 수집 건수만 출력")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않음")
    args = ap.parse_args()

    sections = {}
    for field, srcs in SOURCES.items():
        items = []
        for name, domain in srcs.items():
            try:
                got, how = fetch_source(name, domain, HOURS)
            except Exception as ex:
                print(f"[오류] {name}: {ex}", file=sys.stderr)
                got, how = [], "오류"
            if field == "보험" and name in GENERAL_FINANCE:
                before = len(got)
                got = [g for g in got if any(k in (g["title"] + " " + g.get("desc", "")) for k in INSURANCE_KEYWORDS)]
                note = f" (보험 관련만 {len(got)}/{before})"
            else:
                note = ""
            print(f"[{field}] {name:<10} ({domain}): {len(got)}건 · {how}{note}")
            items += got
            time.sleep(0.5)
        if args.check:
            continue
        buzz_top, recent_top = rank(cluster(items), TOP_N, field)
        for c in buzz_top + recent_top:
            summarize(c)
        ok = sum(1 for c in buzz_top + recent_top if c.get("summary"))
        print(f"[{field}] 요약 성공 {ok}/{len(buzz_top) + len(recent_top)}")
        if buzz_top:
            sections[f"{field} 화제성"] = buzz_top
        if recent_top:
            sections[f"{field} 최신"] = recent_top
    if args.check:
        print("\n'수집실패'인 신문사는 SOURCES의 도메인을 확인해 수정하세요.")
        return

    text_body, html_body = build(sections)
    with open("briefing.html", "w", encoding="utf-8") as f:
        f.write(f"<meta charset='utf-8'><body style='font-family:sans-serif;max-width:760px;margin:auto'>{html_body}</body>")
    print("\n" + text_body)
    if args.dry_run:
        return
    sent = [send_telegram(text_body)]
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
