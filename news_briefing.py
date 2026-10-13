#!/usr/bin/env python3
"""
보험/의료 뉴스 일일 브리핑 (무료 도구만 사용)

동작:
 1) 신문사별 도메인으로 Google News RSS(site: 검색)에서 최근 N시간 기사 수집
 2) 제목 정규화 + 유사도로 중복 기사 병합 (여러 신문사가 다룬 이슈일수록 상위)
 3) 분야별 TOP N 선정
 4) 기사 본문 앞부분으로 요약 (기본: 추출 요약 / 선택: Gemini 무료 API)
 5) 텔레그램 / 이메일(Gmail) 발송 + briefing.html 저장

사용:
  pip install -r requirements.txt
  python news_briefing.py --check          # 신문사별 수집 건수 점검 (도메인 검증용)
  python news_briefing.py --dry-run        # 발송 없이 콘솔 출력 + briefing.html 생성
  python news_briefing.py                  # 실제 발송
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
TOP_N = int(os.getenv("TOP_N", "10"))          # 분야별 상위 기사 수 (10 또는 20)
HOURS = int(os.getenv("HOURS", "24"))          # 최근 몇 시간 기사 수집
SIM_THRESHOLD = 0.62                           # 제목 유사도 기준(0~1). 높을수록 엄격

# 도메인은 직접 확인해서 필요하면 수정하세요. (--check 로 0건인 곳 찾기)
# 확실하지 않은 도메인에는 "?" 표시
SOURCES = {
    "보험": {
        "한국보험신문": "insnews.co.kr",       # ?
        "보험저널": "insjournal.co.kr",
        "보험매일": "dailyinsu.com",           # ?
        "한국금융신문": "fntimes.com",
        "DFT대한금융신문": "kbanker.co.kr",
        "더벨": "thebell.co.kr",
    },
    "의료": {
        "의학신문": "bosa.co.kr",
        "의협신문": "doctorsnews.co.kr",
        "의사신문": "doctorstimes.com",        # ?
        "청년의사": "docdocdoc.co.kr",
        "메디컬타임즈": "medicaltimes.com",
        "메디게이트뉴스": "medigatenews.com",
        "데일리메디": "dailymedi.com",
        "더메디컬": "themedical.co.kr",        # ?
        "메디파나신문": "medipana.com",
    },
}

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (news-briefing)"}


# ───────────────────────── 수집 ─────────────────────────
def fetch_source(name: str, domain: str, hours: int):
    days = max(1, -(-hours // 24))
    q = quote(f"site:{domain} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url, request_headers=UA)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if pub and pub < cutoff:
            continue
        title = html.unescape(e.title)
        # Google News는 제목 뒤에 " - 언론사명"을 붙임 → 제거
        title = re.sub(r"\s+-\s+[^-]{1,30}$", "", title).strip()
        items.append({"title": title, "link": e.link, "source": name, "pub": pub})
    return items


# ───────────────────────── 중복 병합 ─────────────────────────
def norm(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", t)   # [단독] (종합) 등 제거
    t = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def cluster(items):
    """제목이 같거나 매우 유사한 기사를 하나로 병합."""
    clusters = []
    for it in sorted(items, key=lambda x: x["pub"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        n = norm(it["title"])
        placed = False
        for c in clusters:
            if n == c["norm"] or difflib.SequenceMatcher(None, n, c["norm"]).ratio() >= SIM_THRESHOLD:
                c["articles"].append(it)
                placed = True
                break
        if not placed:
            clusters.append({"norm": n, "articles": [it]})
    return clusters


def rank(clusters, top_n):
    def score(c):
        srcs = {a["source"] for a in c["articles"]}
        newest = max((a["pub"] for a in c["articles"] if a["pub"]), default=datetime.min.replace(tzinfo=timezone.utc))
        return (len(srcs), newest)          # 1) 다룬 신문사 수  2) 최신순
    return sorted(clusters, key=score, reverse=True)[:top_n]


# ───────────────────────── 요약 ─────────────────────────
def resolve_url(link: str) -> str:
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(link, interval=1)
        if r.get("status"):
            return r["decoded_url"]
    except Exception:
        pass
    return link


def fetch_body(url: str) -> str:
    try:
        import trafilatura
        r = requests.get(url, headers=UA, timeout=15)
        return trafilatura.extract(r.text) or ""
    except Exception:
        return ""


def extractive_summary(text: str, n=2, max_chars=220) -> str:
    sents = re.split(r"(?<=[.!?다])\s+", text.replace("\n", " "))
    sents = [s.strip() for s in sents if len(s.strip()) > 25]
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
    a = cl["articles"][0]
    url = resolve_url(a["link"])
    cl["url"] = url
    body = fetch_body(url) if url != a["link"] or "news.google.com" not in url else ""
    summ = gemini_summary(a["title"], body) or extractive_summary(body)
    cl["summary"] = summ
    time.sleep(1)  # 서버 부담 완화


# ───────────────────────── 포맷 ─────────────────────────
def build(sections):
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    text_lines, html_parts = [f"📰 보험·의료 뉴스 브리핑 {today}\n"], [
        f"<h2>📰 보험·의료 뉴스 브리핑 {today}</h2>"]
    for field, top in sections.items():
        text_lines.append(f"━━ {field} TOP {len(top)} ━━")
        html_parts.append(f"<h3>{field} TOP {len(top)}</h3><ol>")
        for i, c in enumerate(top, 1):
            a = c["articles"][0]
            srcs = sorted({x["source"] for x in c["articles"]})
            src_txt = ", ".join(srcs)
            text_lines.append(f"{i}. {a['title']}  [{src_txt}]")
            if c.get("summary"):
                text_lines.append(f"   └ {c['summary']}")
            text_lines.append(f"   {c.get('url', a['link'])}")
            html_parts.append(
                f"<li><b><a href='{html.escape(c.get('url', a['link']))}'>{html.escape(a['title'])}</a></b>"
                f" <small>[{html.escape(src_txt)}]</small>"
                + (f"<br><span style='color:#444'>{html.escape(c['summary'])}</span>" if c.get("summary") else "")
                + "</li>")
        html_parts.append("</ol>")
        text_lines.append("")
    return "\n".join(text_lines), "\n".join(html_parts)


# ───────────────────────── 발송 ─────────────────────────
def send_telegram(text: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    # 텔레그램 메시지 4096자 제한 → 분할 전송
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


def send_email(html_body: str, text_body: str):
    user, pw, to = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD"), os.getenv("MAIL_TO")
    if not (user and pw):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[브리핑] 보험·의료 뉴스 {datetime.now(KST):%Y-%m-%d}"
    msg["From"], msg["To"] = user, to or user
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, (to or user).split(","), msg.as_string())
    return True


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
                got = fetch_source(name, domain, HOURS)
            except Exception as ex:
                print(f"[오류] {name}: {ex}", file=sys.stderr)
                got = []
            print(f"[{field}] {name:<10} ({domain}): {len(got)}건")
            items += got
            time.sleep(0.5)
        if args.check:
            continue
        top = rank(cluster(items), TOP_N)
        for c in top:
            summarize(c)
        sections[field] = top
    if args.check:
        print("\n0건인 신문사는 SOURCES의 도메인을 확인해 수정하세요.")
        return

    text_body, html_body = build(sections)
    with open("briefing.html", "w", encoding="utf-8") as f:
        f.write(f"<meta charset='utf-8'><body style='font-family:sans-serif;max-width:760px;margin:auto'>{html_body}</body>")
    print("\n" + text_body)
    if args.dry_run:
        return
    sent = [send_telegram(text_body), send_email(html_body, text_body)]
    if not any(sent):
        print("발송 설정(텔레그램/이메일)이 없어 briefing.html만 저장했습니다.")


if __name__ == "__main__":
    main()
