#!/usr/bin/env python3
"""네이버 블로그 글 하나를 수집해 posts/<postId>/ 에 저장한다.

사용: python tools/fetch_post.py posts/<postId>
  - posts/<postId>/source.txt 의 첫 줄에 블로그 URL이 있어야 한다.
결과:
  posts/<postId>/post.json  (제목·날짜·블록 순서: text/image)
  posts/<postId>/post.md    (사람이 읽기 쉬운 버전)
  posts/<postId>/images/NN.jpg
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
HEADERS = {"User-Agent": UA, "Referer": "https://m.blog.naver.com/"}


def parse_url(url: str):
    """다양한 네이버 블로그 URL 형태에서 (blogId, logNo) 추출."""
    u = urlparse(url.strip())
    q = parse_qs(u.query)
    if "blogId" in q and "logNo" in q:
        return q["blogId"][0], q["logNo"][0]
    m = re.search(r"blog\.naver\.com/([^/?#]+)/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"네이버 블로그 URL을 해석할 수 없음: {url}")


def get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))


def hi_res(src: str) -> str:
    """썸네일/블러 파라미터를 고해상도로 교체."""
    src = re.sub(r"\?type=[^&]+", "?type=w966", src)
    if "?type=" not in src and "pstatic.net" in src:
        src += "?type=w966"
    return src


def clean(t: str) -> str:
    t = t.replace("​", "").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def extract_blocks(soup: BeautifulSoup):
    blocks = []
    container = soup.select_one(".se-main-container")
    if container:  # 스마트에디터 ONE
        for comp in container.select(".se-component"):
            classes = comp.get("class", [])
            if "se-text" in classes or "se-quotation" in classes or "se-sectionTitle" in classes:
                paras = [clean(p.get_text(" ")) for p in comp.select(".se-text-paragraph")]
                text = "\n".join(p for p in paras if p)
                if text:
                    blocks.append({"type": "text", "text": text})
            elif any(c in classes for c in ("se-image", "se-imageStrip", "se-imageGroup", "se-sticker")):
                if "se-sticker" in classes:
                    continue
                for img in comp.select("img"):
                    src = img.get("data-lazy-src") or img.get("data-src") or img.get("src")
                    if src and "pstatic.net" in src:
                        blocks.append({"type": "image", "src": hi_res(src)})
                cap = comp.select_one(".se-caption")
                if cap and clean(cap.get_text(" ")):
                    blocks.append({"type": "caption", "text": clean(cap.get_text(" "))})
        return blocks

    # 구 에디터 (postViewArea / post-view)
    area = soup.select_one("#postViewArea, .post-view, #viewTypeSelector")
    if not area:
        return blocks
    for el in area.find_all(["p", "div", "img", "span"], recursive=True):
        if el.name == "img":
            src = el.get("data-lazy-src") or el.get("src")
            if src and "pstatic.net" in src:
                blocks.append({"type": "image", "src": hi_res(src)})
        elif el.name == "p":
            t = clean(el.get_text(" "))
            if t:
                blocks.append({"type": "text", "text": t})
    return blocks


def main(post_dir: str):
    d = Path(post_dir)
    url = (d / "source.txt").read_text(encoding="utf-8").strip().splitlines()[0]
    blog_id, log_no = parse_url(url)
    page = get(f"https://m.blog.naver.com/{blog_id}/{log_no}")
    soup = BeautifulSoup(page.text, "html.parser")

    title_el = soup.select_one(".se-title-text, .se_title, h3.tit_h3, .tit_area")
    og_title = soup.select_one('meta[property="og:title"]')
    title = clean(title_el.get_text(" ")) if title_el else (og_title["content"] if og_title else "")
    date_el = soup.select_one(".blog_date, .se_publishDate, .date")
    date = clean(date_el.get_text(" ")) if date_el else ""

    blocks = extract_blocks(soup)
    if not blocks:
        (d / "debug.html").write_text(page.text, encoding="utf-8")
        sys.exit("본문을 찾지 못했습니다. debug.html 을 확인하세요.")

    img_dir = d / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for b in blocks:
        if b["type"] != "image":
            continue
        n += 1
        ext = ".gif" if ".gif" in b["src"].lower() else ".jpg"
        name = f"{n:02d}{ext}"
        try:
            r = get(b["src"])
            (img_dir / name).write_bytes(r.content)
            b["file"] = f"images/{name}"
        except Exception as e:  # 이미지 하나 실패해도 계속
            b["error"] = str(e)

    meta = {"url": url, "blogId": blog_id, "logNo": log_no, "title": title, "date": date,
            "blocks": blocks, "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (d / "post.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# {title}", "", f"- 원문: {url}", f"- 작성일: {date}", ""]
    for b in blocks:
        if b["type"] == "text":
            md += [b["text"], ""]
        elif b["type"] == "caption":
            md += [f"> {b['text']}", ""]
        elif b.get("file"):
            md += [f"![]({b['file']})", ""]
    (d / "post.md").write_text("\n".join(md), encoding="utf-8")
    print(f"OK: {title} — text {sum(b['type']=='text' for b in blocks)}개, image {n}개")


if __name__ == "__main__":
    main(sys.argv[1])
