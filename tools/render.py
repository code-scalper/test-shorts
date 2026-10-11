#!/usr/bin/env python3
"""posts/<postId>/script.json → 1080x1920 쇼츠 mp4 렌더러.

사용: python tools/render.py posts/<postId> [--tts elevenlabs|edge|silent]
결과: posts/<postId>/out/short_v<version>.mp4, captions_v<version>.srt, thumb_v<version>.jpg

TTS 선택 순서(기본 auto): ELEVENLABS_API_KEY 있으면 ElevenLabs → 없으면 Edge TTS → 실패 시 무음(길이 추정).
"""
import argparse
import asyncio
import base64
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H, FPS = 1080, 1920, 30
XFADE = 0.35          # 장면 전환 크로스페이드(초)
GAP = 0.25            # 장면 사이 숨 고르기(초)
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]
DEFAULT_VOICE = {"en": "en-US-AndrewNeural", "ko": "ko-KR-SunHiNeural"}
DEFAULT_ELEVEN_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # premade 'George' — 리포 변수 ELEVENLABS_VOICE_ID 로 교체 권장


def font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    sys.exit("Noto Sans CJK 폰트가 필요합니다 (apt install fonts-noto-cjk)")


def probe_duration(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


# ---------------------------------------------------------------- TTS
def tts_elevenlabs(text, out_mp3, cfg):
    import requests
    voice = cfg.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_ELEVEN_VOICE
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"},
        json={"text": text, "model_id": cfg.get("model_id", "eleven_multilingual_v2"),
              "voice_settings": {"stability": cfg.get("stability", 0.45),
                                 "similarity_boost": cfg.get("similarity_boost", 0.8),
                                 "style": cfg.get("style", 0.2), "use_speaker_boost": True}},
        timeout=120)
    r.raise_for_status()
    data = r.json()
    Path(out_mp3).write_bytes(base64.b64decode(data["audio_base64"]))
    al = data.get("alignment") or {}
    return al.get("character_start_times_seconds"), al.get("character_end_times_seconds")


def tts_edge(text, out_mp3, cfg, lang):
    import edge_tts
    voice = cfg.get("edge_voice") or DEFAULT_VOICE.get(lang, DEFAULT_VOICE["ko"])
    asyncio.run(edge_tts.Communicate(text, voice, rate=cfg.get("edge_rate", "+5%")).save(str(out_mp3)))
    return None, None


def tts_silent(text, out_mp3, lang):
    dur = max(1.5, len(text) / (14.0 if lang == "en" else 6.5))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=mono", "-t", f"{dur:.2f}", "-q:a", "9", str(out_mp3)], check=True)
    return None, None


def synth(text, out_mp3, provider, cfg, lang):
    order = {"auto": ["elevenlabs", "edge", "silent"], "elevenlabs": ["elevenlabs"],
             "edge": ["edge", "silent"], "silent": ["silent"]}[provider]
    for p in order:
        if p == "elevenlabs" and not os.environ.get("ELEVENLABS_API_KEY"):
            continue
        try:
            if p == "elevenlabs":
                return p, tts_elevenlabs(text, out_mp3, cfg)
            if p == "edge":
                return p, tts_edge(text, out_mp3, cfg, lang)
            return p, tts_silent(text, out_mp3, lang)
        except Exception as e:
            print(f"  ! TTS {p} 실패: {e}", file=sys.stderr)
    sys.exit("TTS 실패")


# ---------------------------------------------------------------- 자막
def chunk_text(text, lang):
    """자막 덩어리: 문장 → 최대 글자 수 기준으로 단어 단위 분할."""
    limit = 30 if lang == "en" else 15
    chunks = []
    for sent in re.split(r"(?<=[.!?。])\s+", text.strip()):
        words, cur = sent.split(), ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > limit:
                chunks.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            chunks.append(cur)
    # 너무 짧은 꼬리 덩어리는 앞에 붙임
    merged = []
    for c in chunks:
        if merged and len(c) <= 6 and len(merged[-1]) + len(c) < limit + 8 and not merged[-1][-1] in ".!?":
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


def time_chunks(text, chunks, speech_dur, align):
    starts, ends = align if align else (None, None)
    out, pos = [], 0
    total = sum(len(c) for c in chunks)
    acc = 0.0
    for c in chunks:
        idx = text.find(c.split()[0], pos)
        end_idx = idx + len(c) - 1 if idx >= 0 else -1
        if starts and ends and idx >= 0 and end_idx < len(ends):
            s, e = starts[idx], ends[min(end_idx, len(ends) - 1)]
        else:
            s = acc
            e = acc + speech_dur * len(c) / total
        acc += speech_dur * len(c) / total
        pos = max(pos, end_idx + 1 if end_idx >= 0 else pos)
        out.append((c, s, e))
    # 덩어리 사이 공백 메우기 (깜빡임 방지)
    for i in range(len(out) - 1):
        c, s, e = out[i]
        out[i] = (c, s, out[i + 1][1])
    return out


def wrap(draw, text, fnt, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=fnt) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def text_layer(text, size, maxw, stroke=8, fill=(255, 255, 255), hl=None):
    fnt = font(size)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = wrap(tmp, text, fnt, maxw)
    lh = int(size * 1.22)
    layer = Image.new("RGBA", (W, lh * len(lines) + stroke * 2 + 10), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i, ln in enumerate(lines):
        tw = d.textlength(ln, font=fnt)
        d.text(((W - tw) / 2, stroke + i * lh), ln, font=fnt, fill=hl or fill,
               stroke_width=stroke, stroke_fill=(0, 0, 0))
    return layer


# ---------------------------------------------------------------- 비주얼
class Scene:
    def __init__(self, img_path, motion, start, dur):
        im = Image.open(img_path)
        im = ImageOps.exif_transpose(im.convert("RGB"))
        self.src, self.motion, self.start, self.dur = im, motion, start, dur
        sw, sh = im.size
        # 배경: cover + 블러 + 어둡게
        bg = ImageOps.fit(im, (W // 4, H // 4), Image.LANCZOS).filter(ImageFilter.GaussianBlur(10))
        self.bg = Image.eval(bg.resize((W, H), Image.BICUBIC), lambda v: int(v * 0.55))
        # 전경 영역: 세로가 긴 사진은 풀블리드, 아니면 가로폭 맞춤 프레임
        if sh / sw >= 1.45:
            self.rect = (0, 0, W, H)
        else:
            rh = min(int(W * sh / sw), 1300)
            top = int(960 - rh / 2)
            self.rect = (0, top, W, rh)
        rw, rh = self.rect[2], self.rect[3]
        # 소스에서 목표 비율에 맞는 최대 crop
        ar = rw / rh
        if sw / sh > ar:
            cw, ch = sh * ar, sh
        else:
            cw, ch = sw, sw / ar
        self.base = (cw, ch)

    def frame(self, t):
        """장면 내부 시간 t에서의 1080x1920 프레임."""
        p = min(max(t / max(self.dur, 0.01), 0), 1)
        p = p * p * (3 - 2 * p)  # ease in-out
        sw, sh = self.src.size
        cw, ch = self.base
        m = self.motion
        z = {"zoom-in": 1 + 0.10 * p, "zoom-out": 1.10 - 0.10 * p}.get(m, 1.08)
        w, h = cw / z, ch / z
        cx, cy = sw / 2, sh / 2
        slack_x, slack_y = (sw - w) / 2, (sh - h) / 2
        if m == "pan-right":
            cx = sw / 2 - slack_x + 2 * slack_x * p
        elif m == "pan-left":
            cx = sw / 2 + slack_x - 2 * slack_x * p
        elif m == "pan-down":
            cy = sh / 2 - slack_y + 2 * slack_y * p
        elif m == "pan-up":
            cy = sh / 2 + slack_y - 2 * slack_y * p
        box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        x, y, rw, rh = self.rect
        fg = self.src.transform((rw, rh), Image.EXTENT, box, Image.BICUBIC)
        canvas = self.bg.copy()
        canvas.paste(fg, (x, y))
        return canvas


MOTIONS = ["zoom-in", "pan-right", "zoom-out", "pan-left"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("post_dir")
    ap.add_argument("--tts", default="auto", choices=["auto", "elevenlabs", "edge", "silent"])
    ap.add_argument("--max-seconds", type=float, default=0, help="미리보기용 길이 제한")
    a = ap.parse_args()

    d = Path(a.post_dir)
    spec = json.loads((d / "script.json").read_text(encoding="utf-8"))
    lang = spec.get("lang", "ko")
    ver = spec.get("version", 1)
    vcfg = spec.get("voice", {})
    out_dir = d / "out"
    out_dir.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="render_"))

    # 1) 장면별 TTS
    scenes, cues, audio_parts, t = [], [], [], 0.0
    used = set()
    for i, sc in enumerate(spec["scenes"]):
        mp3 = work / f"s{i:02d}.mp3"
        prov, align = synth(sc["text"], mp3, a.tts, vcfg, lang)
        used.add(prov)
        speech = probe_duration(mp3)
        dur = speech + GAP + (sc.get("hold", 0))
        if i == 0:
            dur += 0.15
        motion = sc.get("motion") or MOTIONS[i % len(MOTIONS)]
        scenes.append(Scene(d / sc["image"], motion, t, dur + (XFADE if i < len(spec["scenes"]) - 1 else 0.4)))
        for c, s, e in time_chunks(sc["text"], chunk_text(sc.get("caption") or sc["text"], lang), speech, align
                                   if not sc.get("caption") else None):
            cues.append((c, t + s + (0.15 if i == 0 else 0), t + e + (0.15 if i == 0 else 0)))
        audio_parts.append((mp3, t + (0.15 if i == 0 else 0)))
        t += dur
        print(f"  scene {i+1}: {speech:.1f}s via {prov} [{motion}]")
    total = t + 0.4
    if a.max_seconds:
        total = min(total, a.max_seconds)

    # 2) 오디오 믹스 (각 장면 음성을 정확한 시작 시각에 배치)
    inputs, filt = [], []
    for k, (mp3, st) in enumerate(audio_parts):
        inputs += ["-i", str(mp3)]
        ms = int(st * 1000)
        filt.append(f"[{k}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{k}]")
    n = len(audio_parts)
    mix = "".join(f"[a{k}]" for k in range(n)) + f"amix=inputs={n}:normalize=0:dropout_transition=0[v]"
    bgm = spec.get("bgm")
    if bgm and (Path("assets/bgm") / bgm).exists():
        inputs += ["-stream_loop", "-1", "-i", str(Path("assets/bgm") / bgm)]
        filt.append(mix)
        filt.append(f"[{n}:a]volume={spec.get('bgm_volume', 0.12)},aformat=channel_layouts=stereo[b]")
        filt.append("[v][b]amix=inputs=2:normalize=0:duration=first[m]")
        final = "[m]"
    else:
        filt.append(mix)
        final = "[v]"
    voice_wav = work / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filt) +
                    f";{final}apad,atrim=0:{total:.3f},loudnorm=I=-14:TP=-1.5:LRA=11[o]",
                    "-map", "[o]", "-ar", "44100", str(voice_wav)], check=True)

    # 3) 자막/타이틀 레이어 준비
    title = spec.get("title", "")
    title_layer = text_layer(title, 70, 960, stroke=8, fill=(255, 226, 80)) if title else None
    cap_layers = [(text_layer(c, spec.get("caption_size", 74 if lang == "en" else 70), 940), s, e) for c, s, e in cues]
    srt = []
    for k, (c, s, e) in enumerate(cues, 1):
        f = lambda x: f"{int(x//3600):02d}:{int(x%3600//60):02d}:{int(x%60):02d},{int(x*1000%1000):03d}"
        srt += [str(k), f"{f(s)} --> {f(e)}", c, ""]

    # 4) 프레임 생성 → ffmpeg
    out_mp4 = out_dir / f"short_v{ver}.mp4"
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(voice_wav),
                            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                            str(out_mp4)], stdin=subprocess.PIPE)
    nframes = int(total * FPS)
    title_until = spec.get("title_seconds", 0)  # 0이면 전체 노출
    thumb = None
    for fi in range(nframes):
        tt = fi / FPS
        cur = [s for s in scenes if s.start <= tt < s.start + s.dur]
        if not cur:
            cur = [scenes[-1]]
        img = cur[0].frame(tt - cur[0].start)
        if len(cur) > 1:  # 크로스페이드
            nxt = cur[1]
            alpha = min(max((tt - nxt.start) / XFADE, 0), 1)
            img = Image.blend(img, nxt.frame(tt - nxt.start), alpha)
        if title_layer and (not title_until or tt < title_until):
            img.paste(title_layer, (0, 150), title_layer)
        for layer, s, e in cap_layers:
            if s <= tt < e:
                y = spec.get("caption_y", 1440)
                img.paste(layer, (0, y), layer)
                break
        if thumb is None and tt >= 1.0:
            thumb = img.copy()
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    enc.wait()
    if enc.returncode:
        sys.exit("ffmpeg 인코딩 실패")
    (out_dir / f"captions_v{ver}.srt").write_text("\n".join(srt), encoding="utf-8")
    (thumb or img).save(out_dir / f"thumb_v{ver}.jpg", quality=88)
    info = {"version": ver, "duration": round(total, 2), "tts": sorted(used), "scenes": len(scenes)}
    (out_dir / f"render_v{ver}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"OK → {out_mp4} ({total:.1f}s, tts={','.join(sorted(used))})")


if __name__ == "__main__":
    main()
