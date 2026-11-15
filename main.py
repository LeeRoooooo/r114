import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path

# ================= 설정 영역 (JSON 대체) =================
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
        "item_selector": "ul.board-list li, article, .list-item, a[href*='/trends/']",
        "title_selector": ".tit, .title, h3, strong, span",
        "date_selector": ".date, time",
        "max_items": 20,
    },
    {
        "id": "r114-stories",
        "category": "스토리",
        "url": "https://r114.com/trends/stories",
        "item_selector": "ul.board-list li, article, .list-item, a[href*='/trends/']",
        "title_selector": ".tit, .title, h3, strong, span",
        "date_selector": ".date, time",
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


# ================= 1. 정적 파서 (표준 라이브러리) =================
class AnchorExtractor(HTMLParser):
    def __init__(self, base_url: str, path_prefix: str):
        super().__init__()
        self.base_url = base_url
        self.path_prefix = path_prefix
        self._in_a = False
        self._href = ""
        self._chunks: list[str] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag.lower() == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href and self.path_prefix in href and href.rstrip("/") != self.path_prefix.rstrip("/"):
                self._in_a = True
                self._href = urllib.parse.urljoin(self.base_url, href)
                self._chunks = []

    def handle_data(self, data: str):
        if self._in_a:
            self._chunks.append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._in_a:
            text = re.sub(r"\s+", " ", "".join(self._chunks)).strip()
            if len(text) >= 4:
                self.items.append({"title": text, "link": self._href, "date": ""})
            self._in_a = False


def fetch_static(source: dict) -> list[dict]:
    url = source["url"]
    req = urllib.request.Request(url, headers=HEADERS)
    html = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                html = resp.read().decode(charset, errors="replace")
                break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                return []
            time.sleep(2 ** attempt)

    path_prefix = urllib.parse.urlparse(url).path.rstrip("/") + "/"
    parser = AnchorExtractor(url, path_prefix)
    parser.feed(html)

    seen, results = set(), []
    for item in parser.items:
        if item["link"] not in seen:
            seen.add(item["link"])
            results.append(item)
            if len(results) >= source["max_items"]:
                break
    return results


# ================= 2. 동적 파서 (Playwright 폴백) =================
def fetch_dynamic(source: dict) -> list[dict]:
    from playwright.sync_api import sync_playwright

    url = source["url"]
    path_prefix = urllib.parse.urlparse(url).path.rstrip("/")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=25000)

        try:
            page.wait_for_selector(source["item_selector"], timeout=12000)
        except Exception:
            page.wait_for_timeout(3000)

        raw_items = page.evaluate(
            """({ itemSel, titleSel, dateSel, pathPrefix }) => {
                const results = [];
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!href.includes(pathPrefix) || href.replace(/\\/$/, '') === pathPrefix) continue;
                    const card = a.closest('li, article, .list-item') || a;
                    const titleEl = card.querySelector(titleSel) || a;
                    const dateEl = dateSel ? card.querySelector(dateSel) : null;
                    results.push({
                        title: (titleEl.innerText || a.innerText || '').trim(),
                        link: a.href,
                        date: dateEl ? (dateEl.innerText || '').trim() : ''
                    });
                }
                return results;
            }""",
            {
                "itemSel": source["item_selector"],
                "titleSel": source["title_selector"],
                "dateSel": source["date_selector"],
                "pathPrefix": path_prefix,
            },
        )
        browser.close()

    seen, results = set(), []
    for item in raw_items:
        title = re.sub(r"\s+", " ", item["title"]).strip()
        link = item["link"].strip()
        if len(title) < 4 or not link or link in seen:
            continue
        seen.add(link)
        results.append({"title": title, "link": link, "date": item["date"]})
        if len(results) >= source["max_items"]:
            break
    return results


# ================= 3. RSS 2.0 빌드 및 검증 =================
def parse_pubdate(raw_date: str) -> str:
    match = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw_date or "")
    if match:
        y, m, d = map(int, match.groups())
        return format_datetime(datetime(y, m, d, 9, 0, 0, tzinfo=KST))
    return format_datetime(datetime.now(KST))


def main():
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_META["title"]
    ET.SubElement(channel, "link").text = FEED_META["link"]
    ET.SubElement(channel, "description").text = FEED_META["description"]
    ET.SubElement(channel, "language").text = FEED_META["language"]
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(KST))

    seen_guids = set()

    for src in SOURCES:
        items = fetch_static(src)
        if not items:
            print(f"[INFO] 정적 파싱 0건 -> Playwright 동적 파싱 전환: {src['id']}")
            items = fetch_dynamic(src)

        print(f"[INFO] {src['id']}: {len(items)}건 수집 완료")

        for it in items:
            guid_val = hashlib.sha256(it["link"].rstrip("/").encode("utf-8")).hexdigest()[:24]
            if guid_val in seen_guids:
                continue
            seen_guids.add(guid_val)

            item_el = ET.SubElement(channel, "item")
            ET.SubElement(item_el, "title").text = f"[{src['category']}] {it['title']}"
            ET.SubElement(item_el, "link").text = it["link"]
            guid_el = ET.SubElement(item_el, "guid", isPermaLink="false")
            guid_el.text = guid_val
            ET.SubElement(item_el, "category").text = src["category"]
            ET.SubElement(item_el, "pubDate").text = parse_pubdate(it.get("date", ""))

    if not seen_guids:
        raise RuntimeError("수집된 게시글이 0건입니다. 셀렉터 또는 페이지 구조를 확인하세요.")

    out_path = Path(FEED_META["output_file"])
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {out_path} 생성 완료 (총 {len(seen_guids)}건)")


if __name__ == "__main__":
    main()
