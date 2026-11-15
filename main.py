import hashlib
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

# 엄격한 날짜 패턴 (앞뒤 숫자 오인식 방지)
FULL_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[.\-/년]\s*(0?[1-9]|1[0-2])[.\-/월]\s*(0?[1-9]|[12]\d|3[01])일?(?!\d)")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{2})[.\-/](0[1-9]|1[0-2])[.\-/](0[1-9]|[12]\d|3[01])(?!\d)")
REL_DATE_RE = re.compile(r"(\d+)\s*(분|시간|일)\s*전")

# 카드 상단에 붙는 시리즈/발행호 메타 라벨 필터링
NOISE_LINE_RE = re.compile(
    r"^(ISSUES|STORIES|이슈|스토리|발행\s*\d+.*|20\d{2}\.\d{1,2}|조회\s*\d+.*)$",
    re.IGNORECASE,
)


def classify_card_lines(lines: list[str], fallback_title: str = "") -> tuple[str, str]:
    """카드 내 텍스트 줄 목록에서 (실제 기사 제목, 날짜 문자열)을 분리 추출합니다."""
    date_str = ""
    title_candidates: list[str] = []

    for raw in lines:
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue

        # 1) 날짜 줄 판별
        m_full = FULL_DATE_RE.search(line)
        m_short = SHORT_DATE_RE.search(line) if not m_full else None
        m_rel = REL_DATE_RE.search(line) if not (m_full or m_short) else None

        if m_full or m_short or m_rel:
            matched = (m_full or m_short or m_rel).group(0)
            if not date_str:
                date_str = matched
            # 날짜만 있는 줄이면 제목 후보에서 제외, 제목과 같은 줄이면 날짜 부분만 제거
            rest = line.replace(matched, " ").strip(" -|·")
            if len(rest) >= 6 and not NOISE_LINE_RE.match(rest):
                title_candidates.append(rest)
            continue

        # 2) 시리즈명/발행호/카테고리 뱃지 등 노이즈 줄 제외
        if NOISE_LINE_RE.match(line):
            continue

        if len(line) >= 5:
            title_candidates.append(line)

    # 가장 길고 구체적인 문장(일반적으로 제목, 요약문이 길게 붙을 경우 첫 번째 후보 우선) 선택
    if title_candidates:
        # 시리즈명(짧은 명사구)보다 길고, 본문 요약(50자 이상)보다 앞선 제목 선택
        best_title = title_candidates[0]
        if len(best_title) < 10 and len(title_candidates) > 1:
            best_title = max(title_candidates[:2], key=len)
    else:
        best_title = re.sub(r"\s+", " ", fallback_title).strip()

    return best_title, date_str


# ================= 1. 정적 파서 (SSR JSON/HTML 대응) =================
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

    # Next.js / SPA 페이지에서 정적 HTML에 본문/날짜가 없으면 바로 동적 파서로 넘김
    if "__NEXT_DATA__" not in html and source["link_pattern"] not in html:
        return []
    return []  # r114.com은 클라이언트 렌더링 카드 구조이므로 Playwright로 정확히 추출


# ================= 2. 동적 파서 (Playwright) =================
def fetch_dynamic(source: dict) -> list[dict]:
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
            page.wait_for_timeout(1500)  # 카드 텍스트/날짜 바인딩 완료 대기
        except Exception:
            page.wait_for_timeout(3000)

        raw_cards = page.evaluate(
            """({ linkPattern }) => {
                const seen = new Set();
                const results = [];
                const anchors = Array.from(document.querySelectorAll(`a[href*="${linkPattern}"]`));

                for (const a of anchors) {
                    const href = a.href;
                    // 상세 글 링크(UUID 또는 ID 포함)만 필터링
                    const path = new URL(href).pathname.replace(/\\/$/, '');
                    if (path === linkPattern.replace(/\\/$/, '') || seen.has(href)) continue;
                    seen.add(href);

                    // 1) <a> 태그 내부에 모든 카드 정보가 들어있는 경우 우선 사용
                    let container = a;
                    let text = (container.innerText || '').trim();

                    // 2) <a>가 제목만 감싸고 있다면, 형제 중 다른 <a>가 없는 가장 가까운 카드 부모로 확장
                    if (text.split('\\n').filter(Boolean).length < 2) {
                        let parent = a.parentElement;
                        for (let i = 0; i < 4 && parent; i++) {
                            const linkCount = parent.querySelectorAll(`a[href*="${linkPattern}"]`).length;
                            if (linkCount === 1) {
                                container = parent;
                                text = (container.innerText || '').trim();
                            } else {
                                break;
                            }
                            parent = parent.parentElement;
                        }
                    }

                    // 헤딩 태그(h1~h4)가 있으면 별도 힌트로 전달
                    const heading = container.querySelector('h1, h2, h3, h4');
                    results.push({
                        link: href,
                        lines: text.split('\\n').map(s => s.trim()).filter(Boolean),
                        heading: heading ? heading.innerText.trim() : ''
                    });
                }
                return results;
            }""",
            {"linkPattern": link_pattern},
        )
        browser.close()

    results = []
    for card in raw_cards:
        lines = card.get("lines", [])
        heading = card.get("heading", "")
        if heading and heading not in lines:
            lines.insert(0, heading)

        title, date_str = classify_card_lines(lines, fallback_title=heading)
        if len(title) < 4:
            continue

        results.append({
            "title": title,
            "link": card["link"],
            "date": date_str,
        })
        if len(results) >= source["max_items"]:
            break

    return results


# ================= 3. RSS 2.0 빌드 및 검증 =================
def load_existing_pubdates(xml_path: Path) -> dict[str, str]:
    if not xml_path.exists():
        return {}
    try:
        tree = ET.parse(xml_path)
        return {
            item.findtext("guid", "").strip(): item.findtext("pubDate", "").strip()
            for item in tree.findall("./channel/item")
            if item.findtext("guid") and item.findtext("pubDate")
        }
    except Exception:
        return {}


def parse_pubdate(raw_date: str, fallback_rfc2822: str | None = None) -> str:
    text = (raw_date or "").strip()
    now = datetime.now(KST)

    m4 = FULL_DATE_RE.search(text)
    if m4:
        y, m, d = map(int, m4.groups())
        try:
            return format_datetime(datetime(y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass

    m2 = SHORT_DATE_RE.search(text)
    if m2:
        y, m, d = map(int, m2.groups())
        try:
            return format_datetime(datetime(2000 + y, m, d, 9, 0, 0, tzinfo=KST))
        except ValueError:
            pass

    m_rel = REL_DATE_RE.search(text)
    if m_rel:
        val, unit = int(m_rel.group(1)), m_rel.group(2)
        delta = (
            timedelta(minutes=val) if unit == "분"
            else timedelta(hours=val) if unit == "시간"
            else timedelta(days=val)
        )
        return format_datetime((now - delta).replace(minute=0, second=0, microsecond=0))

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
            print(f"[INFO] 동적 파싱 실행: {src['id']}")
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
        raise RuntimeError("수집된 게시글이 0건입니다. 페이지 구조를 확인하세요.")

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] {out_path} 생성 완료 (총 {len(seen_guids)}건)")


if __name__ == "__main__":
    main()
