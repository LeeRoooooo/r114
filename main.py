import hashlib
import html as html_lib
import re
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

# 제목으로 오인식되는 UI 버튼, 서브카테고리, 시리즈명 블랙리스트
BAD_TITLES = {
    "부동산114",
    "부동산R114",
    "ISSUES",
    "STORIES",
    "이슈",
    "스토리",
    "가장 최근 발표",
    "프롭테크",
    "시장동향",
    "정책·제도",
    "분양·청약",
    "땅의 역사",
    "개발 뒤에 남은 이야기",
    "신도시 이야기",
    "지명의 비밀",
}

FULL_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[.\-/년]\s*(0?[1-9]|1[0-2])[.\-/월]\s*(0?[1-9]|[12]\d|3[01])일?(?!\d)")


def clean_title_text(raw: str) -> str:
    text = html_lib.unescape(raw or "")
    # 앞뒤 가운뎃점(·), 파이프(|), 대시(-) 등 장식 기호 제거
    text = re.sub(r"^[\s·•\-\|]+|[\s·•\-\|]+$", "", text)
    # 사이트명 접미사 제거
    text = re.sub(r"\s*[\|\-–]\s*(부동산R114|부동산114|R114).*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def is_valid_title(title: str) -> bool:
    t = clean_title_text(title)
    if len(t) < 7:
        return False
    if t in BAD_TITLES:
        return False
    if t.startswith("·") or t.startswith("발행"):
        return False
    if re.match(r"^(발행\s*\d+|20\d{2}[.\-/]\d{1,2})$", t):
        return False
    return True


def is_valid_pubdate(pub_str: str) -> bool:
    """자동 폴백 시각(09:00:00이 아닌 임의의 초 단위 실행 시각)인지 판별합니다."""
    if not pub_str:
        return False
    # 정상 파싱된 날짜는 항상 09:00:00 +0900으로 고정 생성됨
    return "09:00:00" in pub_str


# ================= 1. 기존 feed.xml 정상 캐시 로드 =================
def load_existing_items(xml_path: Path) -> dict[str, dict]:
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
        cache = {}
        for item in tree.findall("./channel/item"):
            guid = (item.findtext("guid") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            pure_title = re.sub(r"^\[[^\]]+\]\s*", "", raw_title).strip()
            pub = (item.findtext("pubDate") or "").strip()
            # 제목과 날짜(09:00:00 고정) 모두 정상인 항목만 캐시 인정
            if guid and is_valid_title(pure_title) and is_valid_pubdate(pub):
                cache[guid] = {"title": pure_title, "pubDate": pub}
        return cache
    except Exception:
        return {}


# ================= 2. Playwright 통합 수집 (목록 + 상세 JS 렌더링) =================
def collect_source_items(context, source: dict, existing_cache: dict[str, dict]) -> list[dict]:
    page = context.new_page()
    url = source["url"]
    link_pattern = source["link_pattern"]

    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    try:
        page.wait_for_selector(f"a[href*='{link_pattern}']", timeout=15000)
        page.wait_for_timeout(1500)
    except Exception:
        page.wait_for_timeout(3000)

    # 목록에서 고유 상세 URL과 카드 내부 후보 줄 추출
    raw_links = page.evaluate(
        """({ linkPattern }) => {
            const map = new Map();
            const anchors = Array.from(document.querySelectorAll(`a[href*="${linkPattern}"]`));
            for (const a of anchors) {
                const href = a.href.split('?')[0].split('#')[0];
                const path = new URL(href).pathname.replace(/\\/$/, '');
                if (path === linkPattern.replace(/\\/$/, '')) continue;
                if (!map.has(href)) {
                    map.set(href, href);
                }
            }
            return Array.from(map.values());
        }""",
        {"linkPattern": link_pattern},
    )

    results = []
    for href in raw_links[: source["max_items"]]:
        guid_val = hashlib.sha256(href.rstrip("/").encode("utf-8")).hexdigest()[:24]

        # 이미 정상 수집된 항목이면 상세 페이지 방문 생략
        if guid_val in existing_cache:
            cached = existing_cache[guid_val]
            results.append({
                "guid": guid_val,
                "title": cached["title"],
                "link": href,
                "pubDate": cached["pubDate"],
            })
            continue

        # 상세 페이지 직접 렌더링 후 실제 기사 제목과 발행일 추출
        detail = extract_detail_with_playwright(page, href)
        title = detail["title"] if is_valid_title(detail["title"]) else f"부동산R114 {source['category']} 리포트"
        pub_date = parse_pubdate(detail["date"])

        results.append({
            "guid": guid_val,
            "title": title,
            "link": href,
            "pubDate": pub_date,
        })

    page.close()
    return results


def extract_detail_with_playwright(page, url: str) -> dict:
    """상세 페이지로 이동하여 렌더링 완료 후 헤딩(제목)과 작성일(YYYY.MM.DD)을 추출합니다."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector("h1, h2, article, main", timeout=10000)
        page.wait_for_timeout(1000)
    except Exception:
        return {"title": "", "date": ""}

    data = page.evaluate(
        """() => {
            // 1) 헤딩 태그(h1, h2) 우선 탐색 (네비게이션 로고 제외)
            const headings = Array.from(document.querySelectorAll('main h1, main h2, article h1, article h2, h1, h2'))
                .map(el => (el.innerText || '').trim())
                .filter(t => t.length >= 6 && !t.includes('부동산R114') && !t.includes('부동산114'));

            const title = headings.length > 0 ? headings[0] : (document.title || '');

            // 2) 본문/헤더 영역 텍스트에서 YYYY.MM.DD 또는 YYYY-MM-DD 날짜 탐색
            const container = document.querySelector('main, article') || document.body;
            const bodyText = container ? container.innerText : '';
            const m = bodyText.match(/(?<!\\d)(20\\d{2})[.\\-/년]\\s*(0?[1-9]|1[0-2])[.\\-/월]\\s*(0?[1-9]|[12]\\d|3[01])일?(?!\\d)/);
            const date = m ? m[0] : '';

            return { title, date };
        }"""
    )
    return {
        "title": clean_title_text(data.get("title", "")),
        "date": data.get("date", "").strip(),
    }


def parse_pubdate(raw_date: str) -> str:
    m4 = FULL_DATE_RE.search(raw_date or "")
    if m4:
        y, m, d = map(int, m4.groups())
        try:
            return format_datetime(datetime(y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass
    return format_datetime(datetime.now(KST))


# ================= 3. RSS 2.0 빌드 =================
def main():
    from playwright.sync_api import sync_playwright

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="ko-KR", user_agent=HEADERS["User-Agent"])

        for src in SOURCES:
            items = collect_source_items(context, src, existing_cache)
            print(f"[INFO] {src['id']}: {len(items)}건 처리 완료")

            for it in items:
                if it["guid"] in seen_guids:
                    continue
                seen_guids.add(it["guid"])

                item_el = ET.SubElement(channel, "item")
                ET.SubElement(item_el, "title").text = f"[{src['category']}] {it['title']}"
                ET.SubElement(item_el, "link").text = it["link"]
                guid_el = ET.SubElement(item_el, "guid", isPermaLink="false")
                guid_el.text = it["guid"]
                ET.SubElement(item_el, "category").text = src["category"]
                ET.SubElement(item_el, "pubDate").text = it["pubDate"]

        browser.close()

    if not seen_guids:
        raise RuntimeError("수집된 게시글이 0건입니다. 페이지 구조를 확인하세요.")

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {out_path} 생성 완료 (총 {len(seen_guids)}건)")


if __name__ == "__main__":
    main()
