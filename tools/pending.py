#!/usr/bin/env python3
"""워크플로에서 처리할 대상 폴더를 찾는다.

  python tools/pending.py fetch               → source.txt 는 있고 post.json 이 없는 posts/* 폴더
  python tools/pending.py render [renders_dir] → script.json 의 version 에 해당하는 렌더가
                                                 renders 브랜치에 아직 없는 posts/* 폴더
  python tools/pending.py new-id <url>        → URL 로 만든 폴더 이름(<blogId>-<logNo>)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_post import parse_url  # noqa: E402


def main():
    mode = sys.argv[1]
    if mode == "new-id":
        b, n = parse_url(sys.argv[2])
        print(f"{b}-{n}")
        return
    posts = sorted(p for p in Path("posts").glob("*") if p.is_dir())
    out = []
    if mode == "fetch":
        out = [str(p) for p in posts if (p / "source.txt").exists() and not (p / "post.json").exists()]
    elif mode == "render":
        rdir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("renders")
        for p in posts:
            sj = p / "script.json"
            if not sj.exists():
                continue
            ver = json.loads(sj.read_text(encoding="utf-8")).get("version", 1)
            if not (rdir / p / "out" / f"short_v{ver}.mp4").exists():
                out.append(str(p))
    print(" ".join(out))


if __name__ == "__main__":
    main()
