"""语音转文字服务：按 transcribe_backend 分流。

- local_funasr（2026-08-24 起默认启用）：本地 FunASR 音频级语音转写（媒体理解/会议纪要共用）。
- api_vision（预留）：GLM 视觉模型逐帧提取画面字幕/文字（"视觉转文字"），
  输出带 [mm:ss] 时间戳的分段文本。适用于带字幕/图文视频。
- local_whisper（预留接口）：FFmpeg 提取音频 → faster-whisper 转写。
  决策：不部署本地模型（用户确认），接口保留供未来接入；当前调用返回友好错误。
"""
from __future__ import annotations

import asyncio
import base64
import subprocess
import time
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.transcriber")
_settings = get_settings()

# GLM 视觉并发配额（对齐平台 5 并发）：单视频抽帧并行分析用满配额
_sem_vision = asyncio.Semaphore(3)  # 2026-08-11：5 并发触发 GLM 1302 限流 → 3

# GLM 视觉转文字提示词（强化提示词：明确输出格式与时间戳要求）
VISION_TRANSCRIBE_PROMPT = (
    "你是视频字幕提取助手。下面按时间顺序提供视频关键帧（每帧标注了视频时间点 [mm:ss]）。"
    "请提取每一帧画面中出现的全部字幕与文字内容（包括标题文字、字幕条、画面内文案）。\n"
    "要求：\n"
    "1. 按帧的时间顺序输出，每行格式：[mm:ss] 该时间点画面中的文字\n"
    "2. 同一时间点有多个文字内容时用分号分隔\n"
    "3. 画面中无文字时输出 [mm:ss] （无文字）\n"
    "4. 仅输出提取结果，不要任何解释或额外说明\n"
)


async def transcribe(video_path: str, frames_dir: str | None = None, vision_model: str | None = None) -> dict:
    """转写视频，返回 {"text": 带时间戳全文, "backend": "api_vision"|"local_whisper", "segments": [...]}。

    vision_model=D3 批次级覆盖（定制化工具页模型选择框；None=settings.glm_vision_model）。
    """
    backend = _settings.transcribe_backend
    if backend in ("local_funasr", "local_whisper"):  # local_whisper 兼容旧配置值
        return await _transcribe_funasr(video_path)
    return await _transcribe_vision(video_path, frames_dir, vision_model)


# ---------------------------------------------------------------- GLM 视觉转文字

async def _transcribe_vision(video_path: str, frames_dir: str | None, vision_model: str | None = None) -> dict:
    """抽帧 → GLM 视觉逐帧提取文字 → 带时间戳拼接。"""
    if frames_dir is None:
        frames_dir = str(Path(video_path).parent / "frames")
    Path(frames_dir).mkdir(parents=True, exist_ok=True)

    # 等距抽帧（覆盖全片；帧数受 video_max_frames 上限约束；ffmpeg 同步 → to_thread 防阻塞）
    duration = await asyncio.to_thread(_probe_duration, video_path)
    frame_paths = await asyncio.to_thread(_extract_uniform_frames, video_path, Path(frames_dir), duration)
    if not frame_paths:
        return {"text": "", "backend": "api_vision", "segments": [], "note": "无法抽帧（视频无画面？）"}

    # 逐帧调用 GLM 视觉（并行分发，_sem_vision 限并发；2026-08-11 并发测试：5 并发 × 多路
    # 并发调用会触发 GLM 1302 速率限制——并发 3 + 帧间 0.3s 间隔控住爆发窗口）
    async def _analyze_one(fp: str) -> str:
        b64 = base64.b64encode(await asyncio.to_thread(Path(fp).read_bytes)).decode()  # M9
        async with _sem_vision:
            await asyncio.sleep(0.3)  # 帧间限速（平摊请求窗口）
            return await _glm_vision_text(f"data:image/jpeg;base64,{b64}", VISION_TRANSCRIBE_PROMPT, vision_model)

    parts = list(await asyncio.gather(*[_analyze_one(fp) for fp, _ts in frame_paths]))

    segments = [{"start": ts, "text": t} for (_, ts), t in zip(frame_paths, parts)]
    text = "\n".join(f"[{ts}] {t}" for (_, ts), t in zip(frame_paths, parts))
    return {"text": text, "backend": "api_vision", "segments": segments}


async def _glm_vision_text(data_uri: str, prompt: str, model: str | None = None) -> str:
    payload = {
        "model": model or _settings.glm_vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    # 2026-08-11 并发测试修复：GLM 视觉 API 限流（code 1302 速率限制 / HTTP 429）在并发
    # 下实测频繁触发（5 路并发即中招），原实现无重试 → 任务直接置 failed。
    # 限流为窗口/配额型（串行 8 次全过、并发爆发即 1302）→ 退避 3s/6s/12s（最多 8 次，用户要求；
    # 1305 高峰时段 4 次仍会耗尽）。
    # F1（2026-08-14）：帧级超时 120s→30s + 重试总时长上限 120s——原单帧最坏 120s×4≈8 分钟，
    # 限流/挂起时视频批次被单帧拖死
    deadline = time.monotonic() + 120
    for attempt in range(9):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                resp = await c.post(
                    f"{_settings.zhipu_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {_settings.zhipu_api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException:
            if attempt == 3 or time.monotonic() > deadline:
                raise RuntimeError("GLM 视觉识别失败: 请求超时（重试总时长超限）")
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return (resp.json()["choices"][0]["message"]["content"] or "").strip()
        body = (resp.text or "").lower()
        # 2026-09-01：补 1305（glm-4.7「访问量过大」限流码）——原只认 429/1302/速率限制
        is_rate_limited = resp.status_code == 429 or "1302" in body or "1305" in body or "速率限制" in body
        if is_rate_limited and attempt < 8 and time.monotonic() <= deadline:
            await asyncio.sleep(3 * (attempt + 1))  # 限流退避：3s 递增（最多 8 次，deadline 120s 兜底）
            continue
        raise RuntimeError(f"GLM 视觉识别失败: {resp.text[:200]}")
    raise RuntimeError("GLM 视觉识别失败: 重试耗尽")


# ---------------------------------------------------------------- FunASR 本地语音转写（2026-08-24 启用）

_funasr_model = None
_funasr_lock = asyncio.Lock()
# 2026-08-27（超时兜底）：单次 FunASR 推理超时（25-30× 实时，2h 音频约 5min，600s 余量充足）——
# 推理挂起时任务转 failed 可重试（会议纪要/媒体理解共用；原无超时 → 任务永久卡 running）
_FUNASR_INFER_TIMEOUT_S = 600
# 2026-09-04（模型加载卡死根因）：AutoModel 首次初始化含模型下载且无自带超时——
# 下载挂起会让转写任务永久卡 uploaded（实测 14:50 记录卡死）；加载超时 900s（正常 3-5min）
_FUNASR_LOAD_TIMEOUT_S = 900


def _ms_to_mmss(ms: int) -> str:
    """毫秒 → mm:ss（与 api_vision 的 start 格式对齐）。"""
    s = max(0, ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


# 2026-09-04（转写卡死根因）：modelcope 缓存布局为
# ~/.cache/modelscope/models/iic--<repo>（旧版 funasr 按 hub/models/iic/... 解析，版本
# 不符时 AutoModel 会重新走下载流程——网络卡住 → 转写任务永久卡）。本地已下载则直接传
# 绝对路径（AutoModel 接受本地目录），彻底绕过下载解析。
_MODELSCOPE_CACHE = Path.home() / ".cache" / "modelscope" / "models"


def _local_model_dir(repo: str) -> str:
    """modelscope repo 名 → 本地快照目录（config.yaml 在 snapshots/<rev> 内而非 repo 根。

    2026-09-04 实测：funasr 对本地路径 os.path.exists → 跳过下载直接读 config.yaml，
    指向 repo 根会读不到配置 → 'not registered'；须指向 snapshots/master。
    """
    p = _MODELSCOPE_CACHE / repo.replace("/", "--")
    if not p.is_dir():
        return repo
    snap = p / "snapshots"
    if snap.is_dir():
        revs = sorted(snap.iterdir())
        if revs:
            return str(revs[0])
    return str(p)


def _get_funasr_model():
    """FunASR 懒加载单例（Paraformer-zh + VAD + 标点，CPU）。

    2026-08-24（用户决策启用）：语音转文字默认走本地 FunASR——一次模型调用出
    完整口播转写（替代 GLM 视觉逐帧看字幕，省大量 LLM 调用）；中文口播 CPU 实测 25-30×
    实时（174s 视频 6.9s），字符级时间戳直接聚合为 [mm:ss] 分段。
    """
    global _funasr_model
    if _funasr_model is None:
        from funasr import AutoModel

        _funasr_model = AutoModel(
            model=_local_model_dir("iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
            vad_model=_local_model_dir("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"),
            punc_model=_local_model_dir("iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"),
            device="cpu",
            # 2026-09-03（会议纪要卡住根因）：funasr 每次 AutoModel 初始化先做外网"更新检查"
            # （连 github），网络不通时模型加载卡死 → 转写任务无限等待。禁用更新检查（模型本地缓存）
            disable_update=True,
        )
    return _funasr_model


def _funasr_segments(text: str, timestamp: list) -> list[dict]:
    """字符级时间戳（毫秒对，逐字对齐 text）→ 按句号切 [mm:ss] 分段（对齐 api_vision 结构）。"""
    segs: list[dict] = []
    cur_chars: list[str] = []
    cur_start: int | None = None
    for ch, (s, _e) in zip(text, timestamp):
        if cur_start is None:
            cur_start = s
        cur_chars.append(ch)
        if ch in "。！？；":
            segs.append({"start": _ms_to_mmss(cur_start), "text": "".join(cur_chars)})
            cur_chars, cur_start = [], None
    if cur_chars:
        segs.append({"start": _ms_to_mmss(cur_start or 0), "text": "".join(cur_chars)})
    return segs


async def _transcribe_funasr(video_path: str) -> dict:
    """local_funasr：ffmpeg 提取 16kHz 单声道 → FunASR 一次转写 → [mm:ss] 分段。

    CPU 实测：Paraformer-zh 中文口播 25-30× 实时（174s 视频 ~7s）；返回结构与 api_vision
    对齐（{"text", "backend": "local_funasr", "segments": [{"start": "mm:ss", "text"}]}）。
    """
    import subprocess as _sp
    import tempfile

    try:
        async with _funasr_lock:
            # 2026-09-04（转写卡死根因）：模型首次加载需下载（AutoModel 无自带超时，
            # 下载卡住会永久挂起任务）——wrap 超时 900s（正常首次 ~3-5min，网络差会更快失败），
            # 超时转 failed 可重试（模型完成后下次秒加载）
            model = await asyncio.wait_for(
                asyncio.to_thread(_get_funasr_model), timeout=_FUNASR_LOAD_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise RuntimeError(f"FunASR 模型加载超时（{_FUNASR_LOAD_TIMEOUT_S // 60} 分钟仍未完成，"
                           f"可能是首次运行需下载模型或网络异常），请稍后重试")
    except Exception as e:
        raise RuntimeError(f"FunASR 模型加载失败（本地语音转写不可用）: {str(e)[:120]}")

    # 提取音频（16kHz 单声道 wav——whisper/FunASR 标准输入）
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        await asyncio.to_thread(
            _sp.run,
            ["ffmpeg", "-y", "-v", "error", "-i", video_path, "-ar", "16000", "-ac", "1", wav_path],
            capture_output=True, text=True, timeout=300,
        )
        res = await asyncio.wait_for(
            asyncio.to_thread(model.generate, input=wav_path, batch_size_s=300),
            timeout=_FUNASR_INFER_TIMEOUT_S,
        )
    finally:
        Path(wav_path).unlink(missing_ok=True)
    if not res:
        return {"text": "", "backend": "local_funasr", "segments": [], "note": "音频转写为空"}
    text = str(res[0].get("text") or "").strip()
    timestamp = res[0].get("timestamp") or []
    segs = _funasr_segments(text, timestamp) if timestamp else []
    # 无字符时间戳时按句切（兜底）
    if not segs and text:
        import re as _re

        parts = [p for p in _re.split(r"(?<=[。！？；])", text) if p.strip()]
        segs = [{"start": "00:00", "text": p} for p in parts]
    return {"text": text, "backend": "local_funasr", "segments": segs}


# ---------------------------------------------------------------- FFmpeg 工具

def _probe_duration(video_path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip() or 0)
    except Exception as e:
        logger.warning("ffprobe 失败: %s", str(e)[:120])
        return 0.0


def _extract_uniform_frames(video_path: str, frames_dir: Path, duration: float) -> list[tuple[str, str]]:
    """等距抽帧（默认 12 帧内覆盖全片），返回 [(路径, mm:ss 时间戳)]。"""
    n = _settings.video_max_frames
    if duration <= 0:
        duration = n  # 未知时长按 n 秒粗估
    step = max(duration / n, 0.5)
    paths: list[tuple[str, str]] = []
    for i in range(n):
        t = round(i * step, 1)
        if t >= duration:
            break
        fp = frames_dir / f"uni_{i:02d}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(t), "-i", video_path,
                "-frames:v", "1", "-q:v", "2", "-vf", "scale=720:-2", str(fp),
            ],
            capture_output=True, timeout=60,
        )
        if fp.exists() and fp.stat().st_size > 0:
            paths.append((str(fp), _fmt_ts(t)))
    return paths


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- 说话人分离（2026-08-25 会议纪要双轨录音）

_campplus_model = None
_campplus_lock = asyncio.Lock()


def _get_campplus_model():
    """Cam++ 说话人 embedding 模型懒加载单例（CPU，~100MB；VoxCeleb 训练，192 维向量）。"""
    global _campplus_model
    if _campplus_model is None:
        from funasr import AutoModel

        _campplus_model = AutoModel(
            model="iic/speech_campplus_sv_zh-cn_16k-common",
            device="cpu",
            disable_update=True,  # 2026-09-03：同 funasr 主模型——禁外网更新检查防加载卡死
        )
    return _campplus_model


def _mmss_to_sec(mmss: str) -> float:
    m, s = mmss.split(":")
    return int(m) * 60 + float(s)


def _cluster_speakers(embs: list, distance_threshold: float = 0.4) -> list[int]:
    """说话人凝聚聚类（高精度）：sklearn AgglomerativeClustering——cosine 距离 + average 连接，
    全局最优（不依赖段序），distance_threshold 自动定簇数（0.4 ≈ 余弦相似度 0.6 判同一人）。

    簇数异常（全部分散）时回退阈值放宽合并（distance_threshold=0.55 ≈ sim 0.45，宁可少分不漏）。
    """
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    embs = np.asarray(embs, dtype=np.float32)
    if embs.shape[0] == 1:
        return [0]
    for th in (distance_threshold, 0.55, 0.7):
        cluster = AgglomerativeClustering(
            n_clusters=None, metric="cosine", linkage="average", distance_threshold=th)
        labels = cluster.fit_predict(embs).tolist()
        n = len(set(labels))
        # 段数多且聚成 1 簇可接受（同一人）；段数少时聚成 1 簇也可接受；异常是"每段一簇"
        if n < embs.shape[0]:
            return labels
    return [0] * embs.shape[0]  # 极端兜底：全部归一人


async def diarize_segments(audio_path: str, segments: list[dict]) -> list[dict]:
    """对 [mm:ss] 分段做说话人聚类：每段切 16k 音频 → Cam++ embedding → 聚类。

    返回带 "speaker"（int 簇号，从 0 起）字段的 segments 副本；任何失败（模型加载/
    无有效时间戳/音频读取）返回原样（不阻断转写，仅日志）。
    段 end 取下一段 start（时间戳升序），最后段取全片时长。
    """
    import io as _io
    import subprocess as _sp
    import tempfile

    if not segments or len(segments) < 2:
        return segments
    # 兜底句切分支（start 全 00:00）无有效时间戳 → 无法聚类
    if all(s.get("start") == "00:00" for s in segments):
        return segments
    try:
        async with _campplus_lock:
            model = await asyncio.to_thread(_get_campplus_model)
        # 整片转 16k 单声道 wav → numpy（一次 ffmpeg，段切在内存做）
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            await asyncio.to_thread(
                _sp.run,
                ["ffmpeg", "-y", "-v", "error", "-i", audio_path, "-ar", "16000", "-ac", "1", wav_path],
                capture_output=True, text=True, timeout=300,
            )
            from scipy.io import wavfile

            import numpy as np

            sr, data = await asyncio.to_thread(wavfile.read, wav_path)
            data = data.astype(np.float32) / 32767.0  # int16 → float32（ffmpeg 输出 pcm_s16le）
        finally:
            Path(wav_path).unlink(missing_ok=True)

        duration = data.shape[0] / sr
        embs: list = []
        valid_idx: list[int] = []
        for i, s in enumerate(segments):
            start = _mmss_to_sec(s.get("start", "00:00"))
            end = _mmss_to_sec(segments[i + 1].get("start", "00:00")) if i + 1 < len(segments) else duration
            if end - start < 0.3:  # 过短段无说话人信息
                continue
            seg_audio = data[int(sr * start):int(sr * end)]
            if seg_audio.size == 0:
                continue
            res = await asyncio.to_thread(model.generate, input=seg_audio)
            emb = res[0].get("spk_embedding")
            if emb is None or not len(emb):
                continue
            embs.append(emb[0])
            valid_idx.append(i)
        if len(embs) < 2:
            # 兜底：分段粒度由标点决定，多人连续说话可能整段不分（如拼接音频）。
            # 对每段内取 头/中/尾 2s 子窗提 embedding 聚类，子窗多数簇号归该段。
            from collections import Counter

            sub_embs: list = []
            sub_owner: list[int] = []
            for i, s in enumerate(segments):
                start = _mmss_to_sec(s.get("start", "00:00"))
                end = _mmss_to_sec(segments[i + 1].get("start", "00:00")) if i + 1 < len(segments) else duration
                if end - start < 4:
                    continue
                for frac in (0.15, 0.5, 0.85):
                    w_start = start + (end - start) * frac
                    seg_audio = data[int(sr * w_start):int(sr * (w_start + 2))]
                    if seg_audio.size == 0:
                        continue
                    res = await asyncio.to_thread(model.generate, input=seg_audio)
                    emb = res[0].get("spk_embedding")
                    if emb is not None and len(emb):
                        sub_embs.append(emb[0])
                        sub_owner.append(i)
            if len(sub_embs) < 4:
                return segments
            sub_labels = _cluster_speakers(sub_embs)
            seg_votes: dict[int, Counter] = {}
            for owner, lab in zip(sub_owner, sub_labels):
                seg_votes.setdefault(owner, Counter())[lab] += 1
            out = [dict(s) for s in segments]
            for i, cnt in seg_votes.items():
                out[i]["speaker"] = cnt.most_common(1)[0][0]
            logger.info("说话人聚类（滑窗兜底）audio=%s 段数=%d 聚类数=%d",
                        audio_path, len(seg_votes), len({c.most_common(1)[0][0] for c in seg_votes.values()}))
            return out
        labels = _cluster_speakers(embs)
        out = [dict(s) for s in segments]
        for i, lab in zip(valid_idx, labels):
            out[i]["speaker"] = lab
        logger.info("说话人聚类完成 audio=%s 段数=%d 聚类数=%d", audio_path, len(valid_idx), len(set(labels)))
        # 相邻同人段合并（同一人连续说话合成一段，agent 读起来更干净；start 取首段）
        merged_out: list[dict] = []
        for s in out:
            if s.get("speaker") is not None and merged_out \
                    and merged_out[-1].get("speaker") == s["speaker"] and merged_out[-1].get("speaker") is not None:
                merged_out[-1]["text"] = (merged_out[-1].get("text", "") + s.get("text", "")).strip()
                continue
            merged_out.append(s)
        return merged_out
    except Exception as e:
        logger.warning("说话人聚类失败（跳过，仅时间戳）audio=%s err=%s", audio_path, str(e)[:150])
        return segments


def _fmt_speaker(tag: str, speaker: int | None, multi: bool) -> str:
    """说话人标签：multi=False → 仅 tag（单说话人，如"本地"）；multi=True → tag+字母（本地A/线上B）。"""
    if speaker is None or not multi:
        return tag
    return f"{tag}{chr(ord('A') + speaker)}"


async def transcribe_dual_track(local_path: str, remote_path: str) -> dict:
    """双轨转写（2026-08-25 会议纪要）：本地轨（麦克风）+ 线上轨（系统声音）分别 FunASR 转写
    与说话人聚类，按时间轴合并。

    输出：{"text": "[mm:ss] 本地A: ...\\n[mm:ss] 线上B: ...", "backend": "local_funasr+diarize",
           "segments": [{"start","text","speaker","track"}]}
    任一转写失败 → 返回已成功的轨（不阻断）；全失败抛异常。
    """
    local = await _transcribe_funasr(local_path)
    remote = await _transcribe_funasr(remote_path)

    local_segs = await diarize_segments(local_path, local.get("segments") or [])
    remote_segs = await diarize_segments(remote_path, remote.get("segments") or [])

    local_multi = len({s.get("speaker") for s in local_segs if s.get("speaker") is not None}) > 1
    remote_multi = len({s.get("speaker") for s in remote_segs if s.get("speaker") is not None}) > 1

    merged: list[dict] = []
    for s in local_segs:
        merged.append({**s, "track": "本地", "speaker": s.get("speaker")})
    for s in remote_segs:
        merged.append({**s, "track": "线上", "speaker": s.get("speaker")})
    merged.sort(key=lambda s: _mmss_to_sec(s.get("start", "00:00")))

    parts = []
    for s in merged:
        multi = (s["track"] == "本地" and local_multi) or (s["track"] == "线上" and remote_multi)
        label = _fmt_speaker(s["track"], s.get("speaker"), multi)
        parts.append(f"[{s.get('start', '00:00')}] {label}: {s.get('text', '')}")
    return {"text": "\n".join(parts), "backend": "local_funasr+diarize", "segments": merged}
