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
        "date_selector": ".date, time, .day, .reg-date, span",
        "max_items": 20,
    },
    {
        "id": "r114-stories",
        "category": "스토리",
        "url": "https://r114.com/trends/stories",
        "item_selector": "ul.board-list li, article, .list-item, a[href*='/trends/']",
        "title_selector": ".tit, .title, h3, strong, span",
        "date_selector": ".date, time, .day, .reg-date, span",
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

# 날짜 정규식 패턴 (YYYY.MM.DD, YY.MM.DD, YYYY년 MM월 DD일, 상대시간 지원)
DATE_PATTERNS = [
    re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?"),
    re.compile(r"(?<!\d)(\d{2})\s*[.\-/]\s*(0[1-9]|1[0-2])\s*[.\-/]\s*(0[1-9]|[12]\d|3[01])(?!\d)"),
]
REL_PATTERN = re.compile(r"(\d+)\s*(분|시간|일)\s*전")


def extract_date_and_clean_title(title: str, raw_date: str = "") -> tuple[str, str]:
    """제목이나 날짜 문자열에서 날짜를 추출하고, 제목에 붙은 날짜 텍스트를 깔끔히 제거합니다."""
    combined = f"{raw_date} {title}".strip()
    extracted_date = ""

    for pat in DATE_PATTERNS:
        m = pat.search(combined)
        if m:
            extracted_date = m.group(0)
            if not raw_date:
                title = title.replace(extracted_date, " ").strip()
            break

    if not extracted_date:
        m_rel = REL_PATTERN.search(combined)
        if m_rel:
            extracted_date = m_rel.group(0)
            if not raw_date:
                title = title.replace(extracted_date, " ").strip()

    clean_title = re.sub(r"\s+", " ", title).strip(" -|·")
    return clean_title, (raw_date or extracted_date).strip()


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
        elif self.items and not self.items[-1].get("date"):
            # <a> 태그 직후 형제 요소에 날짜가 나오는 경우 보완 캡처
            for pat in DATE_PATTERNS:
                m = pat.search(data)
                if m:
                    self.items[-1]["date"] = m.group(0)
                    break

    def handle_endtag(self, tag: str):
        if tag.lower() == "a" and self._in_a:
            raw_text = re.sub(r"\s+", " ", "".join(self._chunks)).strip()
            title, date_str = extract_date_and_clean_title(raw_text)
            if len(title) >= 4:
                self.items.append({"title": title, "link": self._href, "date": date_str})
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

    # 정적 파싱에서 아이템은 찾았지만 날짜를 단 1건도 못 찾았다면 JS 렌더링 구조로 판단해 동적 폴백 유도
    if results and not any(r.get("date") for r in results):
        return []
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
            """({ titleSel, dateSel, pathPrefix }) => {
                const results = [];
                const dateRegex = /(20\\d{2}\\s*[.\\-/년]\\s*\\d{1,2}\\s*[.\\-/월]\\s*\\d{1,2}\\s*일?)|((?<!\\d)\\d{2}\\s*[.\\-/]\\s*(?:0[1-9]|1[0-2])\\s*[.\\-/]\\s*(?:0[1-9]|[12]\\d|3[01])(?!\\d))|(\\d+\\s*(?:분|시간|일)\\s*전)/;
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!href.includes(pathPrefix) || href.replace(/\\/$/, '') === pathPrefix) continue;
                    const card = a.closest('li, article, .list-item, .card, .item, tr, div') || a;
                    const titleEl = card.querySelector(titleSel) || a;

                    let dateText = '';
                    if (dateSel) {
                        const dateCandidates = Array.from(card.querySelectorAll(dateSel));
                        for (const el of dateCandidates) {
                            const txt = (el.innerText || '').trim();
                            const m = txt.match(dateRegex);
                            if (m) { dateText = m[0]; break; }
                        }
                    }
                    if (!dateText) {
                        const m = (card.innerText || '').match(dateRegex);
                        if (m) dateText = m[0];
                    }

                    results.push({
                        title: (titleEl.innerText || a.innerText || '').trim(),
                        link: a.href,
                        date: dateText
                    });
                }
                return results;
            }""",
            {
                "titleSel": source["title_selector"],
                "dateSel": source["date_selector"],
                "pathPrefix": path_prefix,
            },
        )
        browser.close()

    seen, results = set(), []
    for item in raw_items:
        title, date_str = extract_date_and_clean_title(item["title"], item.get("date", ""))
        link = item["link"].strip()
        if len(title) < 4 or not link or link in seen:
            continue
        seen.add(link)
        results.append({"title": title, "link": link, "date": date_str})
        if len(results) >= source["max_items"]:
            break
    return results


# ================= 3. RSS 2.0 빌드 및 검증 =================
def load_existing_pubdates(xml_path: Path) -> dict[str, str]:
    """기존 feed.xml에서 guid별 pubDate를 읽어와 날짜 미상 게시글의 시각 변동을 방지합니다."""
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
        mapping = {}
        for item in tree.findall("./channel/item"):
            guid = item.findtext("guid")
            pub = item.findtext("pubDate")
            if guid and pub:
                mapping[guid.strip()] = pub.strip()
        return mapping
    except Exception:
        return {}


def parse_pubdate(raw_date: str, fallback_rfc2822: str | None = None) -> str:
    text = (raw_date or "").strip()
    now = datetime.now(KST)

    # 1) 4자리 연도: YYYY.MM.DD / YYYY-MM-DD / YYYY년 MM월 DD일
    m4 = DATE_PATTERNS[0].search(text)
    if m4:
        y, m, d = map(int, m4.groups())
        try:
            return format_datetime(datetime(y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass

    # 2) 2자리 연도: YY.MM.DD
    m2 = DATE_PATTERNS[1].search(text)
    if m2:
        y, m, d = map(int, m2.groups())
        try:
            return format_datetime(datetime(2000 + y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass

    # 3) 상대 시간: N분 전 / N시간 전 / N일 전
    m_rel = REL_PATTERN.search(text)
    if m_rel:
        val, unit = int(m_rel.group(1)), m_rel.group(2)
        delta = (
            timedelta(minutes=val) if unit == "분"
            else timedelta(hours=val) if unit == "시간"
            else timedelta(days=val)
        )
        target = (now - delta).replace(minute=0, second=0, microsecond=0)
        return format_datetime(target)

    # 4) 기존 feed.xml에 기록된 pubDate가 있으면 유지 (불필요한 변경 방지)
    if fallback_rfc2822:
        return fallback_rfc2822

    return format_datetime(now)


def main():
    out_path = Path(FEED_META["output_file"])
    existing_pubdates = load_existing_pubdates(out_path)

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
            print(f"[INFO] 정적 파싱 0건(또는 날짜 미검출) -> Playwright 동적 파싱 전환: {src['id']}")
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
            ET.SubElement(item_el, "pubDate").text = parse_pubdate(
                it.get("date", ""), existing_pubdates.get(guid_val)
            )

    if not seen_guids:
        raise RuntimeError("수집된 게시글이 0건입니다. 셀렉터 또는 페이지 구조를 확인하세요.")

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {out_path} 생성 완료 (총 {len(seen_guids)}건)")


if __name__ == "__main__":
    main()
