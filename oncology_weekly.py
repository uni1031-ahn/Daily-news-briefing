#!/usr/bin/env python3
"""
oncology_weekly.py — "항암 주간 요약" 생성기 (표준 라이브러리만 사용)

뉴스 브리핑의 '의료' 영역에 한 꼭지로 끼워 넣을 HTML 조각을 만든다.

산출물 (OUT_DIR = data/oncology)
  YYYY-Www.json        주간 원자료(기사 클러스터, 트렌드 카운트)
  YYYY-Www.csv         기사 아카이브
  latest_section.html  메일/포털에 그대로 붙일 수 있는 인라인 스타일 조각
  latest.json          최신 주차 요약(포털에서 읽기 쉬운 형태)

사용법
  python oncology_weekly.py              # 실제 수집
  python oncology_weekly.py --no-ai      # Claude 요약 없이(스니펫 기반)
  python oncology_weekly.py --demo       # 가상 샘플로 레이아웃 확인(실제 뉴스 아님)

환경변수
  ANTHROPIC_API_KEY   (선택) 있으면 한국어 제목/요약을 Claude로 생성
  CLAUDE_MODEL        (선택) 기본 claude-haiku-5-5 (기존 브리핑과 동일)
  NAVER_CLIENT_ID / NAVER_CLIENT_SECRET   (선택) 국내 뉴스 보강
  OUT_DIR             (선택) 기본 data/oncology
"""
import argparse, csv, datetime as dt, difflib, html, json, os, re, sys
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

KST = dt.timezone(dt.timedelta(hours=9))
OUT_DIR = os.environ.get("OUT_DIR", "data/oncology")
DAYS = 8                       # 수집 범위(일) — 주 1회 발송 + 여유 1일
MAX_ITEMS = 10                 # 꼭지에 싣는 핵심 소식 수
UA = "Mozilla/5.0 (oncology-weekly-digest)"

# ---------------------------------------------------------------- 수집원
# Google News RSS 검색식. 한 줄 = 한 번의 요청. (when:8d 로 기간 제한)
GN_QUERIES = [
    # (질의, 언어)
    ("CAR-T cell therapy", "en"), ("antibody-drug conjugate ADC cancer", "en"),
    ("immuno-oncology PD-1 PD-L1", "en"), ("radioligand therapy cancer", "en"),
    ("targeted therapy oncology FDA approval", "en"), ("bispecific antibody cancer", "en"),
    ("breast cancer HER2 CDK4/6", "en"), ("oncology Phase 3 topline results", "en"),
    ("site:fiercebiotech.com oncology", "en"), ("site:onclive.com", "en"),
    ("site:cancernetwork.com", "en"), ("site:fda.gov oncology approval", "en"),
    ("항암제 신약", "ko"), ("면역항암제", "ko"), ("표적항암제", "ko"),
    ("CAR-T 세포치료제", "ko"), ("항체약물접합체 ADC", "ko"),
    ("유방암 치료제", "ko"), ("항암 임상 3상", "ko"), ("항암제 급여 건강보험", "ko"),
]
# 직접 RSS (실패해도 무시. 주소는 바뀔 수 있으니 로그에서 0건이면 교체)
DIRECT_FEEDS = [   # 직접 RSS (FDA 주소는 404라 제거. site:fda.gov 구글뉴스 질의가 대신함). 필요하면 (이름, 주소) 추가
]

# ---------------------------------------------------------------- 분류
CATS = [  # (key, 라벨, 색, 정규식) — 위에서부터 먼저 맞는 것
    ("reg",      "허가·급여", "#1f6feb", r"\bFDA\b|EMA|approv|clearance|허가|승인|급여|건보|식약처|심평원|보험"),
    ("cart",     "CAR-T·세포치료", "#8250df", r"CAR[- ]?T|CAR-NK|cell therapy|TCR|TIL\b|세포치료|키메라"),
    ("adc",      "ADC", "#bf3989", r"\bADC\b|antibody[- ]drug conjugate|deruxtecan|vedotin|govitecan|항체[- ·]?약물|접합체"),
    ("rlt",      "방사성의약품", "#9a6700", r"radioligand|radiopharma|lutetium|Lu-177|177Lu|방사성|RLT"),
    ("io",       "면역항암", "#1a7f37", r"immuno-?oncology|immunotherap|PD-?1|PD-?L1|CTLA|checkpoint|pembrolizumab|nivolumab|면역항암|면역관문|키트루다|옵디보"),
    ("targeted", "표적항암", "#0969da", r"targeted therap|tyrosine kinase|TKI|EGFR|ALK\b|KRAS|HER2|CDK4|PARP|BTK|표적|억제제"),
    ("trial",    "임상", r"#6e7781", r"Phase [123I]+|phase [123I]+|topline|trial|임상"),
    ("biz",      "딜·사업", "#cf222e", r"acquir|acquisition|licen[cs]|deal|partnership|collaboration|인수|라이선스|기술이전|제휴|계약"),
]
CAT_LABEL = {k: l for k, l, _, _ in CATS}; CAT_LABEL["other"] = "기타"
CAT_COLOR = {k: c for k, _, c, _ in CATS}; CAT_COLOR["other"] = "#6e7781"
CAT_RE = [(k, re.compile(rx, re.I)) for k, _, _, rx in CATS]

ONC_RE = re.compile(
    r"cancer|oncolog|tumou?r|carcinoma|lymphoma|leukemia|myeloma|melanoma|CAR[- ]?T|"
    r"암\b|항암|종양|림프종|백혈병|골수종|흑색종|유방암|폐암|대장암|위암|간암|췌장암|전립선암|난소암", re.I)
# CAR-T 는 자가면역(루푸스·중증근무력증 등)에도 쓰이므로, 암 용어 없이 자가면역 용어만 있으면 제외
STRICT_ONC_RE = re.compile(
    r"cancer|oncolog|tumou?r|carcinoma|lymphoma|leukemia|myeloma|melanoma|"
    r"암\b|항암|종양|림프종|백혈병|골수종|흑색종|유방암|폐암|대장암|위암|간암|췌장암|전립선암|난소암", re.I)
ROUNDUP_RE = re.compile(r"\bbrief\b|biopharma brief|round-?up|week in review|weekly (news|digest|update)|news digest|브리프|모음|뉴스 ?정리|주간 ?소식|이번 ?주 ?소식", re.I)
AUTOIMM_RE = re.compile(r"lupus|autoimmun|multiple sclerosis|myasthenia|rheumat|루푸스|자가면역|다발성 ?경화|중증근무력|류마티스", re.I)
BREAST_RE = re.compile(r"breast|HER2|trastuzumab|CDK4|유방", re.I)
TAG_BOOST = [(re.compile(r"FDA|approv|승인|허가", re.I), 2),
             (re.compile(r"Phase 3|3상|topline", re.I), 2),
             (re.compile(r"acquir|인수|billion|조원|억 ?달러", re.I), 1)]

# 트렌드 용어(전주 대비 언급 건수)
TERMS = ["CAR-T", "ADC", "bispecific", "radioligand", "KRAS", "PD-1", "HER2", "TROP2",
         "in vivo CAR-T", "GLP-1", "TCE", "CDK4/6", "PROTAC", "mRNA vaccine"]
TERMS_KO = {"CAR-T": r"CAR[- ]?T", "ADC": r"\bADC\b|항체[- ·]?약물", "bispecific": r"bispecific|이중항체|이중특이",
            "radioligand": r"radioligand|방사성", "KRAS": r"KRAS", "PD-1": r"PD-?1|PD-?L1",
            "HER2": r"HER2", "TROP2": r"TROP-?2", "in vivo CAR-T": r"in vivo CAR", "GLP-1": r"GLP-?1",
            "TCE": r"T[- ]cell engager|TCE", "CDK4/6": r"CDK ?4/?6", "PROTAC": r"PROTAC|degrader",
            "mRNA vaccine": r"mRNA (cancer )?vaccine|암백신"}

# ---------------------------------------------------------------- 유틸
def log(*a): print(*a, file=sys.stderr)

def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t or "")      # 끝의 ' - 매체명' 제거
    t = re.sub(r"[^0-9A-Za-z가-힣 ]+", " ", t).lower()
    return re.sub(r"\s+", " ", t).strip()

def source_of(title, fallback=""):
    m = re.search(r"\s-\s([^-]{2,40})$", title or "")
    return (m.group(1).strip() if m else fallback) or "?"

def iso_week(d): y, w, _ = d.isocalendar(); return f"{y}-W{w:02d}"

def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

# ---------------------------------------------------------------- 수집
def parse_rss(xml_bytes, default_source=""):
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try: when = parsedate_to_datetime(pub).astimezone(KST) if pub else None
        except Exception: when = None
        src_el = it.find("source")
        src = (src_el.text.strip() if src_el is not None and src_el.text else source_of(title, default_source))
        snippet = strip_html(it.findtext("description"))[:400]
        out.append(dict(title=title, link=link, when=when, source=src, snippet=snippet))
    return out

def gn_url(q, lang):
    hl, gl, ceid = ("ko", "KR", "KR:ko") if lang == "ko" else ("en-US", "US", "US:en")
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{q} when:{DAYS}d")
            + f"&hl={hl}&gl={gl}&ceid={ceid}")

def collect_naver(queries):
    cid = (os.environ.get("NAVER_CLIENT_ID") or "").strip()
    sec = (os.environ.get("NAVER_CLIENT_SECRET") or "").strip()
    if not (cid and sec): return []
    out = []
    for q in queries:
        url = "https://openapi.naver.com/v1/search/news.json?display=30&sort=date&query=" + urllib.parse.quote(q)
        try:
            data = json.loads(http_get(url, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}))
        except Exception as e:
            log(f"[naver] {q}: 실패 {e}"); continue
        n = 0
        for it in data.get("items", []):
            try: when = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except Exception: continue
            link = it.get("originallink") or it.get("link")
            host = urllib.parse.urlparse(link).netloc.replace("www.", "")
            out.append(dict(title=strip_html(it["title"]), link=link, when=when, source=host,
                            snippet=strip_html(it.get("description", ""))[:400])); n += 1
        log(f"[naver] {q}: {n}건")
    return out

def collect(now):
    arts = []
    for q, lang in GN_QUERIES:
        try:
            got = parse_rss(http_get(gn_url(q, lang)))
            log(f"[gnews] {q}: {len(got)}건")
            arts += got
        except Exception as e:
            log(f"[gnews] {q}: 실패 {e}")
    for name, url in DIRECT_FEEDS:
        try:
            got = parse_rss(http_get(url), name)
            log(f"[rss] {name}: {len(got)}건"); arts += got
        except Exception as e:
            log(f"[rss] {name}: 실패 {e}")
    arts += collect_naver(["항암제", "면역항암제", "CAR-T", "표적항암제", "유방암 신약"])
    cutoff = now - dt.timedelta(days=DAYS)
    def is_onc(a):
        t = a["title"] + " " + a["snippet"]
        if not ONC_RE.search(t): return False
        if ROUNDUP_RE.search(a["title"]): return False       # 여러 소식을 묶은 브리프·모음 기사 제외
        return not (AUTOIMM_RE.search(t) and not STRICT_ONC_RE.search(t))
    keep = [a for a in arts if a["when"] and a["when"] >= cutoff and is_onc(a)]
    log(f"수집 {len(arts)}건 → 기간·항암 필터 후 {len(keep)}건")
    return keep

# ---------------------------------------------------------------- 군집·점수
def tokens(t): return set(norm_title(t).split())

def similar(a, b):
    na, nb = norm_title(a["title"]), norm_title(b["title"])
    if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.78: return True
    ta, tb = tokens(a["title"]), tokens(b["title"])
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6

def cluster(arts, now):
    arts = sorted(arts, key=lambda a: a["when"], reverse=True)
    groups = []
    for a in arts:
        for g in groups:
            if a["link"] == g[0]["link"] or similar(a, g[0]):
                g.append(a); break
        else:
            groups.append([a])
    out = []
    for i, g in enumerate(groups):
        head = g[0]
        text = head["title"] + " " + head["snippet"]
        cat = next((k for k, rx in CAT_RE if rx.search(text)), "other")
        srcs = sorted({a["source"] for a in g})
        days_old = (now - head["when"]).total_seconds() / 86400
        score = 2 * len(srcs) + sum(b for rx, b in TAG_BOOST if rx.search(text)) + max(0, 2 - days_old / 3)
        out.append(dict(id=i, title=head["title"], link=head["link"], when=head["when"].isoformat(),
                        source=head["source"], sources=srcs, snippet=head["snippet"], cat=cat,
                        breast=bool(BREAST_RE.search(text)), score=round(score, 2),
                        lang="ko" if re.search(r"[가-힣]", head["title"]) else "en",
                        members=[dict(title=a["title"], link=a["link"], source=a["source"]) for a in g[:5]]))
    out.sort(key=lambda c: c["score"], reverse=True)
    return out

def trend_counts(clusters):
    blob = [c["title"] + " " + c["snippet"] for c in clusters]
    res = {}
    for t in TERMS:
        rx = re.compile(TERMS_KO.get(t, re.escape(t)), re.I)
        res[t] = sum(1 for b in blob if rx.search(b))
    return res

def load_prev(week_label):
    try:
        files = sorted(f for f in os.listdir(OUT_DIR) if re.match(r"\d{4}-W\d{2}\.json$", f)
                       and f[:-5] < week_label)
        if files:
            with open(os.path.join(OUT_DIR, files[-1]), encoding="utf-8") as fh:
                return json.load(fh).get("trend", {})
    except Exception: pass
    return {}

# ---------------------------------------------------------------- AI 요약
def claude_text(prompt, max_tokens=8000):
    """Claude 호출 → 본문 텍스트. 키가 없거나 실패하면 None."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key: return None
    model = os.environ.get("CLAUDE_MODEL") or os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-5-5"
    base = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    hd = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def call(extra):
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST", headers=hd,
                                     data=json.dumps({**base, **extra}).encode())
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    try:
        try:   # 추론 블록이 앞에 붙어 본문이 밀리는 것을 막기 위해 추론을 끔 (기존 브리핑과 같은 방식)
            j = call({"thinking": {"type": "disabled"}})
        except urllib.error.HTTPError as e:
            if e.code != 400: raise
            j = call({})   # 이 모델이 추론 끄기 설정을 받지 않으면 설정 없이 다시
        txt = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text").strip()
        if not txt:
            raise ValueError(f"빈 응답 · 블록={[b.get('type') for b in j.get('content', [])]} · 끝난 이유={j.get('stop_reason')}")
        return txt
    except urllib.error.HTTPError as e:
        log(f"[ai] 실패: HTTP {e.code} {e.read()[:200]!r}")
    except Exception as e:
        log(f"[ai] 실패: {e}")
    return None

def ai_summarize(top, breast):
    items = [dict(id=c["id"], title=c["title"], snippet=c["snippet"], sources=c["sources"][:4]) for c in top + breast]
    prompt = (
        "다음은 이번 주 항암 분야 기사 제목/스니펫입니다. 제공된 텍스트에 있는 사실만 사용하세요. "
        "제목·스니펫에 없는 수치, 효능, 승인 여부를 추측하지 마세요.\n"
        "- 같은 사건(같은 약·같은 승인·같은 발표)을 다룬 항목은 가장 정보가 많은 하나만 남기고 나머지에는 same_as 에 그 id를 적으세요. "
        "영문/국문 기사, 매체가 달라도 같은 사건이면 중복입니다.\n"
        "- 암·항암과 무관한 기사(자가면역질환, 당뇨·비만 등 비종양 주제가 중심)는 exclude 를 true 로 하세요.\n"
        "- 여러 소식을 한데 묶은 브리프·모음·주간 정리 기사는 exclude 를 true 로 하세요.\n"
        "- 기업 실적·매출·주가·자금조달이 중심인 기사(임상·허가·기술·계약 소식이 아닌 것)는 exclude 를 true 로 하세요.\n"
        "- cat 은 다음 중 가장 알맞은 하나: reg(승인·허가·급여·심사), cart(CAR-T·세포치료), adc(항체약물접합체), "
        "rlt(방사성의약품), io(면역항암·면역관문억제제·이중항체 면역), targeted(표적항암·저분자·이중특이항체 표적치료), "
        "trial(임상 결과·진입), biz(계약·인수·기술이전·투자), other. 승인 기사는 약물 종류와 상관없이 reg 로 합니다.\n"
        "- vague 는 약 이름·회사·기관 등 구체적 대상이 제목·스니펫에 없어 무슨 소식인지 알기 어려우면 true.\n"
        "- title_ko 에는 매체명을 붙이지 마세요. 영문 기사는 한국어로 번역하세요.\n"
        "- summary 에서 '수치가 없다/제공되지 않았다' 같은 부재 언급은 하지 말고, 알 수 있는 사실만 쓰세요.\n"
        "JSON만 출력: {\"items\": [{\"id\": 번호, \"same_as\": 중복이면 대표 id 아니면 null, \"exclude\": false, \"cat\": \"분류키\", \"vague\": false, \"title_ko\": \"한국어 제목\", \"summary\": \"1~2문장 한국어 요약\"}]}\n\n"
        + json.dumps(items, ensure_ascii=False))
    txt = claude_text(prompt)
    if not txt: return None
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0))
    except Exception as e:
        log(f"[ai] 요약 JSON 해석 실패 → 스니펫으로 대체: {e}")
        return None

def ai_headline(top):
    """최종 선정된 소식만 보고 '이번 주 흐름' 1~2문장을 만든다 (목록과 문장이 어긋나지 않도록)."""
    lines = "\n".join(f"{i}. {c.get('title_ko') or c['title']} — {c.get('summary') or ''}" for i, c in enumerate(top, 1))
    txt = claude_text("아래 이번 주 항암 핵심 소식 목록만 근거로, 이번 주 흐름을 한국어 1~2문장(150자 이내)으로 요약하세요. "
                      "목록에 없는 사건은 언급하지 말고, 설명 없이 문장만 출력하세요.\n\n" + lines, 600)
    return txt.strip().strip('"') if txt else ""

def apply_ai(clusters, ai):
    if not ai: return ""
    by_id = {x.get("id"): x for x in ai.get("items", [])}
    for c in clusters:
        x = by_id.get(c["id"])
        if x:
            c["title_ko"], c["summary"] = x.get("title_ko"), x.get("summary")
            if x.get("same_as") not in (None, "", c["id"]):
                c["dup_of"] = x["same_as"]
            if x.get("cat") in CAT_LABEL:
                c["cat"] = x["cat"]
            if x.get("vague") is True:
                c["vague"] = True
            if x.get("exclude") is True:
                c["dup_of"] = -1          # 비항암으로 판단 → 목록에서 제외
    return ai.get("headline", "")

# ---------------------------------------------------------------- HTML 조각 (메일 호환: table+인라인 스타일)
def esc(s): return html.escape(s or "", quote=True)

def render_section(week_label, start, end, headline, top, breast, trend, prev, demo=False, ai_used=False):
    th = "padding:6px 8px;border-bottom:2px solid #d0d7de;text-align:left;font-size:12px;color:#57606a;"
    td = "padding:8px;border-bottom:1px solid #eaeef2;font-size:13px;vertical-align:top;color:#1f2328;"
    rows = []
    for n, c in enumerate(top, 1):
        title = c.get("title_ko") or c["title"]
        summ = c.get("summary") or (c["snippet"][:140] + ("…" if len(c["snippet"]) > 140 else ""))
        badge = (f'<span style="background:{CAT_COLOR[c["cat"]]};color:#fff;border-radius:3px;'
                 f'padding:1px 6px;font-size:11px;white-space:nowrap;">{esc(CAT_LABEL[c["cat"]])}</span>')
        srcs = ", ".join(esc(s) for s in c["sources"][:3]) + (f" 외 {len(c['sources'])-3}곳" if len(c["sources"]) > 3 else "")
        rows.append(
            f'<tr><td style="{td}width:22px;color:#8c959f;">{n}</td>'
            f'<td style="{td}width:92px;">{badge}</td>'
            f'<td style="{td}"><a href="{esc(c["link"])}" style="color:#0969da;text-decoration:none;font-weight:600;">{esc(title)}</a>'
            f'<div style="color:#57606a;margin-top:3px;">{esc(summ)}</div>'
            f'<div style="color:#8c959f;font-size:11px;margin-top:3px;">{srcs}</div></td></tr>')
    brows = "".join(
        f'<li style="margin:0 0 4px 0;"><a href="{esc(c["link"])}" style="color:#0969da;text-decoration:none;">'
        f'{esc(c.get("title_ko") or c["title"])}</a></li>' for c in breast) or '<li style="color:#8c959f;">이번 주 해당 소식 없음</li>'
    trows = []
    for t, v in sorted(trend.items(), key=lambda kv: -kv[1])[:8]:
        if v == 0: continue
        p = prev.get(t)
        d = "" if p is None else (f' <span style="color:#1a7f37;">▲{v-p}</span>' if v > p else
                                   f' <span style="color:#cf222e;">▼{p-v}</span>' if v < p else ' <span style="color:#8c959f;">-</span>')
        trows.append(f'<span style="display:inline-block;margin:0 10px 4px 0;font-size:12px;">'
                     f'<b>{esc(t)}</b> {v}{d}</span>')
    demo_note = ('<div style="background:#fff8c5;border:1px solid #d4a72c;padding:6px 10px;margin-bottom:10px;'
                 'font-size:12px;">⚠ 데모 화면입니다. 아래 내용은 레이아웃 확인용 가상 샘플이며 실제 뉴스가 아닙니다.</div>') if demo else ""
    foot = ("제목·스니펫 기준 자동 요약" + ("(AI)" if ai_used else "") +
            " · 투자·진료 판단 전 원문과 공식 자료를 확인하세요.")
    return f"""<!-- onc-weekly-start -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #d0d7de;border-radius:6px;font-family:-apple-system,'Malgun Gothic',Arial,sans-serif;margin:16px 0;">
<tr><td style="padding:12px 14px;background:#f6f8fa;border-bottom:1px solid #d0d7de;">
<span style="font-size:15px;font-weight:700;color:#1f2328;">항암 주간 요약</span>
<span style="font-size:12px;color:#57606a;margin-left:8px;">{esc(week_label)} · {start:%m.%d}–{end:%m.%d}</span></td></tr>
<tr><td style="padding:12px 14px;">
{demo_note}
<div style="font-size:14px;line-height:1.5;margin-bottom:10px;color:#1f2328;">{esc(headline)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><th style="{th}">#</th><th style="{th}">분류</th><th style="{th}">핵심 소식</th></tr>
{''.join(rows)}
</table>
<div style="margin-top:14px;font-size:13px;font-weight:700;color:#1f2328;">유방암 워치</div>
<ul style="margin:6px 0 0 18px;padding:0;font-size:13px;">{brows}</ul>
<div style="margin-top:14px;font-size:13px;font-weight:700;color:#1f2328;">키워드 언급량 <span style="font-weight:400;color:#8c959f;font-size:11px;">(수집 기사 내 건수, 전주 대비)</span></div>
<div style="margin-top:6px;">{''.join(trows) or '<span style="color:#8c959f;font-size:12px;">집계 없음</span>'}</div>
<div style="margin-top:12px;font-size:11px;color:#8c959f;">{esc(foot)}</div>
</td></tr></table>
<!-- onc-weekly-end -->
"""

# ---------------------------------------------------------------- 저장
def save(week_label, start, end, headline, clusters, top, breast, trend, section_html, demo):
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = dict(week=week_label, start=start.date().isoformat(), end=end.date().isoformat(),
                   headline=headline, demo=demo, trend=trend, top=top, breast=breast,
                   total_clusters=len(clusters))
    if not demo:   # 데모는 이력(주차 파일)을 오염시키지 않음
        with open(os.path.join(OUT_DIR, f"{week_label}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        with open(os.path.join(OUT_DIR, f"{week_label}.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["week", "cat", "score", "title", "source", "n_sources", "link", "when"])
            for c in clusters:
                w.writerow([week_label, c["cat"], c["score"], c["title"], c["source"], len(c["sources"]), c["link"], c["when"]])
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "latest_section.html"), "w", encoding="utf-8") as f:
        f.write(section_html)

# ---------------------------------------------------------------- 데모(가상)
def demo_clusters(now):
    def mk(i, cat, ti, sn, srcs, br=False):
        return dict(id=i, title=ti, link="https://example.com/demo/%d" % i, when=now.isoformat(),
                    source=srcs[0], sources=srcs, snippet=sn, cat=cat, breast=br,
                    score=10 - i, lang="ko", members=[])
    return [
        mk(0, "adc", "[가상] A사 TROP2 ADC, 3상 1차 지표 충족", "가상 샘플 문장입니다.", ["가상일보", "샘플뉴스", "예시신문"], True),
        mk(1, "reg", "[가상] 신규 CAR-T 치료제 허가 심사 착수", "가상 샘플 문장입니다.", ["샘플뉴스", "예시신문"]),
        mk(2, "io", "[가상] 병용 면역항암 요법 급여 확대 논의", "가상 샘플 문장입니다.", ["예시신문"]),
        mk(3, "rlt", "[가상] 방사성의약품 생산시설 증설 발표", "가상 샘플 문장입니다.", ["가상일보"]),
        mk(4, "biz", "[가상] 이중항체 후보물질 라이선스 계약", "가상 샘플 문장입니다.", ["샘플뉴스"]),
    ]

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true"); ap.add_argument("--no-ai", action="store_true")
    args = ap.parse_args()
    now = dt.datetime.now(KST)
    start, end = now - dt.timedelta(days=7), now
    week_label = iso_week(now)

    if args.demo:
        clusters = demo_clusters(now)
        for c in clusters: c["title_ko"], c["summary"] = c["title"], c["snippet"]
        headline = "[가상] 이번 주 ADC·세포치료 중심의 샘플 문장입니다. 실제 뉴스가 아닙니다."
        trend = {"CAR-T": 4, "ADC": 6, "HER2": 3, "PD-1": 2, "KRAS": 1}
        prev = {"CAR-T": 3, "ADC": 6, "HER2": 5, "PD-1": 2}
        ai_used = False
    else:
        arts = collect(now)
        if not arts:
            log("수집 0건 — 네트워크/피드 확인 필요. 기존 파일을 덮어쓰지 않고 종료합니다.")
            sys.exit(1)
        clusters = cluster(arts, now)
        trend, prev = trend_counts(clusters), load_prev(week_label)
        cand = clusters[:MAX_ITEMS * 3]          # 중복·비항암 제거 후에도 10건이 남도록 넉넉히 요약
        breast_all = [c for c in clusters if c["breast"] and c not in cand][:3]
        ai = None if args.no_ai else ai_summarize(cand, breast_all)
        apply_ai(clusters, ai)
        ai_used = bool(ai)
        headline = ""
    if args.demo:
        top = clusters[:MAX_ITEMS]
        breast = [c for c in clusters if c["breast"]][:3]
    else:
        pool = [c for c in clusters[:MAX_ITEMS * 3] if c.get("dup_of") is None]   # AI가 지목한 중복 제외
        # 구체성이 떨어지는 기사는 뒤로(순서는 유지). 단, 여러 매체가 다룬 기사는 후순위로 밀지 않음
        pool.sort(key=lambda c: bool(c.get("vague")) and len(c["sources"]) < 2)
        top = pool[:MAX_ITEMS]
        if ai_used:
            headline = ai_headline(top)
        if not headline:
            n_cat = {}
            for c in clusters: n_cat[c["cat"]] = n_cat.get(c["cat"], 0) + 1
            parts = [f"{CAT_LABEL[k]} {v}건" for k, v in sorted(n_cat.items(), key=lambda kv: -kv[1])[:3]]
            headline = f"이번 주 항암 관련 {len(clusters)}개 이슈 중 " + ", ".join(parts) + " 순으로 많았습니다."
        # 유방암 워치: 상위에 이미 오른 건 제외하고 별도 3건 (중복 지목분도 제외)
        breast = [c for c in clusters if c["breast"] and c not in top and c.get("dup_of") is None][:3]
    section = render_section(week_label, start, end, headline, top, breast, trend, prev, args.demo, ai_used)
    save(week_label, start, end, headline, clusters, top, breast, trend, section, args.demo)
    log(f"완료: {OUT_DIR}/latest_section.html ({week_label}{' DEMO' if args.demo else ''})")

if __name__ == "__main__":
    main()
