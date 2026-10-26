"""누적 저장된 기사(data/articles/*.csv)를 포털로 보내 '이슈 판단용 기준 기록'을 바로 채웁니다.
화면에는 보이지 않고 이슈 판단에만 쓰입니다. 여러 번 실행해도 중복되지 않습니다.
사용: python backfill_portal.py [일수=35]"""
import csv, glob, os, sys
from datetime import datetime, timedelta, timezone
import requests

KST = timezone(timedelta(hours=9))


def run(days=35):
    url = (os.getenv("PORTAL_URL") or "").strip().rstrip("/")
    tok = (os.getenv("SYNC_TOKEN") or "").strip()
    if not (url and tok):
        print("[기록 채우기] PORTAL_URL/SYNC_TOKEN 이 없어 건너뜁니다")
        return False
    since = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    items, seen = [], set()
    for p in sorted(glob.glob(os.path.join("data", "articles", "*.csv"))):
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                d = (r.get("pub") or "")[:10] or (r.get("first_seen") or "")[:10]
                t = (r.get("title") or "").strip()
                if not (d and t and d >= since) or r.get("id") in seen:
                    continue
                seen.add(r.get("id"))
                items.append({"id": r.get("id"), "d": d, "t": t, "s": (r.get("desc") or "")[:200], "u": r.get("link") or ""})
    days_n = len({x["d"] for x in items})
    print(f"[기록 채우기] 최근 {days}일 기사 {len(items)}건 ({days_n}일치)를 보냅니다")
    try:
        for k in range(0, len(items), 200):
            r = requests.post(url + "/api/sync/newsarch", json={"items": items[k:k + 200]}, timeout=60,
                              headers={"Authorization": "Bearer " + tok, "User-Agent": "insurance-radar-sync/1.0"})
            if r.status_code != 200:
                print(f"[기록 채우기 실패] {r.status_code} {r.text[:200]}")
                return False
        print("[기록 채우기] 완료")
        return True
    except Exception as ex:
        print(f"[기록 채우기 실패] {ex}")
        return False


if __name__ == "__main__":
    sys.exit(0 if run(int(sys.argv[1]) if len(sys.argv) > 1 else 35) else 1)
