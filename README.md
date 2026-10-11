# test-shorts

네이버 블로그 글 1편 → 한국어 나레이션 쇼츠(1080×1920) 자동 제작 테스트 리포.

## 흐름

```
① 링크 전달        posts/<blogId>-<logNo>/source.txt 커밋
② 수집 (Actions)   1. 블로그 수집 → post.json, post.md, images/ 커밋
③ 대본 작성        Claude가 post.md·사진을 보고 script.json 작성 후 커밋
④ 렌더 (Actions)   2. 쇼츠 렌더링 → renders 브랜치 posts/<id>/out/short_v<N>.mp4
⑤ 검수             영상 확인 후 수정 요청 → script.json version+1 → ④ 반복
⑥ 최종             script.json "status": "final" → Release 로 최종본 게시
```

수동으로 돌릴 때: Actions → **1. 블로그 수집** → Run workflow → URL 입력.

## 설정

| 종류 | 이름 | 설명 |
|---|---|---|
| Secret | `ELEVENLABS_API_KEY` | 없으면 무료 Edge TTS(ko-KR-SunHiNeural)로 대체 |
| Variable | `ELEVENLABS_VOICE_ID` | 한국어 보이스 ID (ElevenLabs Voice Library에서 선택) |

## script.json

```jsonc
{
  "version": 1,               // 수정할 때마다 +1 (렌더 파일명 short_v<N>.mp4)
  "status": "draft",          // "final" 이면 Release 게시
  "lang": "ko",
  "title": "상단 고정 제목",
  "title_seconds": 0,         // 0 = 끝까지 노출
  "voice": { "voice_id": "", "edge_voice": "ko-KR-SunHiNeural" },
  "bgm": null,                // assets/bgm/ 안 파일명
  "scenes": [
    { "image": "images/01.jpg", "text": "나레이션 문장", "motion": "zoom-in" }
    // motion: zoom-in | zoom-out | pan-left | pan-right | pan-up | pan-down
    // caption: 자막을 나레이션과 다르게 하고 싶을 때만
    // hold: 장면 뒤에 추가로 머무를 초
  ]
}
```

## 로컬 실행

```bash
pip install requests beautifulsoup4 pillow edge-tts   # + ffmpeg, fonts-noto-cjk
python tools/fetch_post.py posts/<id>
python tools/render.py posts/<id> --tts edge
```
