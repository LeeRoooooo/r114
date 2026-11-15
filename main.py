import hashlib
import html as html_lib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path

# ================= 설정 영역 =================
FEED_META = {
    "title": "부동산R114 트렌드 & 이슈",
    "link": "https://r114.com/trends/issues",
    "description": "부동산R114(r114.com) 트렌드 이슈 및 스토리 통합 RSS 피드",
    "language": "ko",
    "output_file": "feed.xml",
}

SOURCES = [
    {
        "id": "r114-issues",
        "category": "이슈",
        "url": "https://r114.com/trends/issues",
        "link_pattern": "/trends/issues/",
        "max_items": 20,
    },
    {
        "id": "r114-stories",
        "category": "스토리",
        "url": "https://r114.com/trends/stories",
        "link_pattern": "/trends/stories/",
        "max_items": 20,
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
KST = timezone(timedelta(hours=9))

# 제목으로 잘못 잡히는 사이트명/시리즈명/메타 라벨
BAD_TITLES = {
    "부동산114",
    "부동산R114",
    "ISSUES",
    "STORIES",
    "이슈",
    "스토리",
    "땅의 역사",
    "개발 뒤에 남은 이야기",
    "신도시 이야기",
    "지명의 비밀",
}

FULL_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[.\-/년]\s*(0?[1-9]|1[0-2])[.\-/월]\s*(0?[1-9]|[12]\d|3[01])일?(?!\d)")
ISO_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})(?:[T\s](\d{2}:\d{2}(?::\d{2})?))?")


def is_valid_title(title: str) -> bool:
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) < 6:
        return False
    if t in BAD_TITLES:
        return False
    if re.match(r"^(발행\s*\d+|20\d{2}[.\-/]\d{1,2})$", t):
        return False
    return True


def clean_title_text(raw: str) -> str:
    text = html_lib.unescape(raw or "")
    # 사이트명 접미사 제거 (예: "기사 제목 | 부동산R114" 또는 "- 부동산114")
    text = re.sub(r"\s*[\|\-–]\s*(부동산R114|부동산114|R114).*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


# ================= 1. 목록 수집 (Playwright로 고유 상세 URL 확보) =================
def collect_article_links(source: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright

    url = source["url"]
    link_pattern = source["link_pattern"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=25000)

        try:
            page.wait_for_selector(f"a[href*='{link_pattern}']", timeout=15000)
            page.wait_for_timeout(1500)
        except Exception:
            page.wait_for_timeout(3000)

        raw_items = page.evaluate(
            """({ linkPattern }) => {
                const map = new Map();
                const anchors = Array.from(document.querySelectorAll(`a[href*="${linkPattern}"]`));

                for (const a of anchors) {
                    const href = a.href.split('?')[0].split('#')[0];
                    const path = new URL(href).pathname.replace(/\\/$/, '');
                    if (path === linkPattern.replace(/\\/$/, '')) continue;

                    // 동일 href를 가리키는 여러 <a>(썸네일, 제목 등) 중 가장 긴 텍스트를 후보로 병합
                    const txt = (a.innerText || '').trim();
                    const prev = map.get(href) || { link: href, texts: [] };
                    if (txt) prev.texts.push(txt);
                    map.set(href, prev);
                }
                return Array.from(map.values());
            }""",
            {"linkPattern": link_pattern},
        )
        browser.close()

    results = []
    for item in raw_items:
        lines = []
        for block in item["texts"]:
            lines.extend([ln.strip() for ln in block.split("\n") if ln.strip()])

        # 유효한 제목 후보 추출
        candidate_title = ""
        for ln in lines:
            cleaned = clean_title_text(ln)
            if is_valid_title(cleaned):
                candidate_title = cleaned
                break

        # 목록 텍스트에서 날짜 추출 시도
        date_str = ""
        for ln in lines:
            m = FULL_DATE_RE.search(ln)
            if m:
                date_str = m.group(0)
                break

        results.append({
            "link": item["link"],
            "title": candidate_title,
            "date": date_str,
        })
        if len(results) >= source["max_items"]:
            break

    return results


# ================= 2. 상세 페이지 보완 파서 (제목/날짜 누락 시 정밀 추출) =================
def fetch_detail_meta(url: str) -> tuple[str, str]:
    """상세 페이지 HTML의 메타 태그(og:title, published_time) 및 본문에서 정확한 제목과 발행일을 추출합니다."""
    req = urllib.request.Request(url, headers=HEADERS)
    html = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                html = resp.read().decode(charset, errors="replace")
                break
        except Exception:
            time.sleep(1)

    if not html:
        return "", ""

    title = ""
    # 1) og:title 또는 twitter:title 추출
    for meta_pat in [
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title[^>]*>([^<]+)</title>",
        r"<h1[^>]*>(.*?)</h1>",
    ]:
        m = re.search(meta_pat, html, flags=re.IGNORECASE | re.DOTALL)
        if m:
            raw_t = re.sub(r"<[^>]+>", " ", m.group(1))
            cleaned = clean_title_text(raw_t)
            if is_valid_title(cleaned):
                title = cleaned
                break

    date_str = ""
    # 2) article:published_time / datePublished / JSON-LD 날짜 우선 추출
    for date_pat in [
        r'(?:published_time|datePublished|createdAt|regDate)["\'\s:=]+["\']?(20\d{2}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?)?)',
        r"<time[^>]*>([^<]+)</time>",
    ]:
        m = re.search(date_pat, html, flags=re.IGNORECASE)
        if m:
            date_str = m.group(1).strip()
            break

    # 3) 본문 텍스트 내 YYYY.MM.DD 패턴 폴백
    if not date_str:
        text_only = re.sub(r"<[^>]+>", " ", html)
        m_full = FULL_DATE_RE.search(text_only)
        if m_full:
            date_str = m_full.group(0)

    return title, date_str


# ================= 3. RSS 2.0 빌드 및 캐시 재사용 =================
def load_existing_items(xml_path: Path) -> dict[str, dict]:
    """기존 feed.xml에 이미 정상 저장된 (title, pubDate)를 캐싱해 불필요한 재요청을 줄입니다."""
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
        cache = {}
        for item in tree.findall("./channel/item"):
            guid = (item.findtext("guid") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            # "[이슈] 제목"에서 말머리 제거 후 유효성 검사
            pure_title = re.sub(r"^\[[^\]]+\]\s*", "", raw_title).strip()
            pub = (item.findtext("pubDate") or "").strip()
            if guid and is_valid_title(pure_title):
                cache[guid] = {"title": pure_title, "pubDate": pub}
        return cache
    except Exception:
        return {}


def parse_pubdate(raw_date: str, fallback_rfc2822: str | None = None) -> str:
    text = (raw_date or "").strip()

    m_iso = ISO_DATE_RE.search(text)
    if m_iso:
        d_part, t_part = m_iso.groups()
        y, m, d = map(int, d_part.split("-"))
        hh, mm, ss = (9, 0, 0)
        if t_part:
            parts = list(map(int, t_part.split(":")))
            hh, mm = parts[0], parts[1]
            ss = parts[2] if len(parts) > 2 else 0
        try:
            return format_datetime(datetime(y, m, d, hh, mm, ss, tzinfo=KST))
        except ValueError:
            pass

    m4 = FULL_DATE_RE.search(text)
    if m4:
        y, m, d = map(int, m4.groups())
        try:
            return format_datetime(datetime(y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass

    if fallback_rfc2822:
        return fallback_rfc2822

    return format_datetime(datetime.now(KST))


def main():
    out_path = Path(FEED_META["output_file"])
    existing_cache = load_existing_items(out_path)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_META["title"]
    ET.SubElement(channel, "link").text = FEED_META["link"]
    ET.SubElement(channel, "description").text = FEED_META["description"]
    ET.SubElement(channel, "language").text = FEED_META["language"]
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(KST))

    seen_guids = set()

    for src in SOURCES:
        items = collect_article_links(src)
        print(f"[INFO] {src['id']}: 목록 링크 {len(items)}건 확보")

        for it in items:
            guid_val = hashlib.sha256(it["link"].rstrip("/").encode("utf-8")).hexdigest()[:24]
            if guid_val in seen_guids:
                continue
            seen_guids.add(guid_val)

            cached = existing_cache.get(guid_val, {})
            title = it["title"] or cached.get("title", "")
            raw_date = it["date"]

            # 제목이 없거나(부동산114, 시리즈명 필터링됨) 날짜가 없으면 상세 페이지에서 정밀 보완
            if not is_valid_title(title) or not raw_date:
                detail_title, detail_date = fetch_detail_meta(it["link"])
                if is_valid_title(detail_title):
                    title = detail_title
                if detail_date:
                    raw_date = detail_date

            if not is_valid_title(title):
                title = f"부동산R114 {src['category']} 콘텐츠"

            pub_date_str = parse_pubdate(raw_date, cached.get("pubDate"))

            item_el = ET.SubElement(channel, "item")
            ET.SubElement(item_el, "title").text = f"[{src['category']}] {title}"
            ET.SubElement(item_el, "link").text = it["link"]
            guid_el = ET.SubElement(item_el, "guid", isPermaLink="false")
            guid_el.text = guid_val
            ET.SubElement(item_el, "category").text = src["category"]
            ET.SubElement(item_el, "pubDate").text = pub_date_str

    if not seen_guids:
        raise RuntimeError("수집된 게시글이 0건입니다. 페이지 구조를 확인하세요.")

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {out_path} 생성 완료 (총 {len(seen_guids)}건)")


if __name__ == "__main__":
    main()
