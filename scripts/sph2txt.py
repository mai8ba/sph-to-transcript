#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sph2txt.py — 微信视频号链接 → 逐字稿（单 skill 自包含，Windows / macOS 通用）

一条命令：视频号链接 → 按语义分段好的纯净逐字稿（只有一个 .txt）
全程免费、零 API Key，只需首次一次微信扫码。

链路：
    视频号分享链接
      → 元宝 HTTP 解析（拿 CDN 直链，无需常驻浏览器）
      → urllib 分块下载 mp4（断点续传、域名白名单、大小上限）
      → ffmpeg 抽 16k 单声道 WAV（临时目录，用完即删）
      → 能量包络找换气点，把硬切点挪到静音最低处（不切断字词）
      → 本地 SenseVoice 分块识别（不上传任何音视频）
      → 按语义合并成段落，只输出 txt

输出目录里永远只有 .txt；mp4 / wav 等过程文件落在系统临时目录，跑完自动删除
（加 --keep-video 才会把 mp4 单独留在 <out>/videos/）。

用法：
    python sph2txt.py --setup                    # 首次准备（装依赖 + 下模型）
    python sph2txt.py --login                    # 首次扫码（存登录态）
    python sph2txt.py --doctor                   # 体检
    python sph2txt.py "<链接>"                    # 单条出稿（默认无时间码、自动分段）
    python sph2txt.py "<链接1>" "<链接2>"          # 批量出稿
    python sph2txt.py --url "<链接1>" --url "<链接2>"
    python sph2txt.py --links-file links.txt     # 从文件读链接（每行一条）
    python sph2txt.py "<链接>" --timestamps      # 需要时间码时才加这个参数

元宝解析部分的接口调用逻辑改编自 Backtthefuture/video-transcript（MIT License）：
    https://github.com/Backtthefuture/video-transcript
本文件为独立分发包，不依赖任何其它 WorkBuddy skill。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# 平台与常量
# ---------------------------------------------------------------------------

IS_WIN = os.name == "nt"

APP_DIR = Path.home() / ".cache" / "sph-transcript"
MODEL_DIR = APP_DIR / "sensevoice"
STATE_FILE = Path.home() / ".workbuddy" / "credentials" / "yuanbao_state.json"

YUANBAO_ORIGIN = "https://yuanbao.tencent.com"
AGENT_ID = "naQivTmsDa/cf4d0079-ed1b-4c55-a3f3-2ca1379727d1"
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# SenseVoice 模型（sherpa-onnx 官方转换版）
MODEL_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
MODEL_FILES = {
    "model.int8.onnx": 239_233_841,
    "tokens.txt": 315_894,
}
MODEL_SOURCES = [
    ("HuggingFace 镜像", "https://hf-mirror.com/{repo}/resolve/main/{name}"),
    ("HuggingFace 官方", "https://huggingface.co/{repo}/resolve/main/{name}"),
    ("ModelScope", "https://modelscope.cn/models/pengzhendong/"
                   "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/master/{name}"),
]


def model_sources() -> list[tuple[str, str]]:
    """允许用环境变量 SPH_MODEL_URL 指定自建镜像（形如 https://your.host/path/{name}）。"""
    custom = os.environ.get("SPH_MODEL_URL", "").strip()
    if custom:
        return [("自定义镜像", custom)] + MODEL_SOURCES
    return MODEL_SOURCES

# 媒体域名白名单：只允许腾讯系 CDN
MEDIA_SUFFIXES = ("qq.com", "qpic.cn", "myqcloud.com", "tencent.com", "gtimg.com")
MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# —— 识别分块与换气点 ——
CHUNK_SEC = 30.0                     # 目标分块时长（仅作锚点，实际切点会被挪到换气处）
DECODE_BATCH = 8                     # 每批同时解码的块数
ENERGY_HOP = 0.02                    # 能量包络帧长（秒）
BOUNDARY_SEARCH = 3.0                # 在锚点前后各搜索多少秒找能量最低点
MIN_CHUNK_SEC = 8.0                  # 切点合法区间下限（防止两个切点挤在一起）
MAX_CHUNK_SEC = 34.0                 # 单块上限（含搜索偏移）

# —— 语义分段 ——
PARA_PAUSE_SEC = 1.0                 # 停顿达此长度视为段落边界
PARA_MIN_CHARS = 60                  # 段落不足此长度时，普通停顿不切
PARA_MAX_CHARS = 280                 # 单段上限，超出则在该节内做平衡拆分
PARA_HEAD_MIN = 20                   # 遇到「第二杯」这类标记时，上一段至少这么长才切

# 段落起始标记：序数/列举类，这类词几乎一定开启新话题
# 注意故意不含「天/年」——「第二天」「第二年」通常是叙事顺承，不是新话题
_PARA_HEAD_BODY = (
    r"第[一二三四五六七八九十百0-9]+[杯个点条步次轮件]"
    r"|第[一二三四五六七八九十百]+[、，,.]"
    r"|首先|其次|再次|再一个|另外|此外|还有一点|接下来|最后|最终"
    r"|总之|总结一下|说白了|说到底"
)
_PARA_HEAD = re.compile(r"^(?:" + _PARA_HEAD_BODY + r")")
_PARA_HEAD_ANY = re.compile(_PARA_HEAD_BODY)
_SENT_RE = re.compile(r"[^。！？…；!?;]+[。！？…；!?;]*")

# Windows 非法文件名字符；额外去掉 #（会破坏 Obsidian wikilink 锚点）
_ILLEGAL = re.compile(r'[\\/:*?"<>|#\r\n\t\f\v]')
_TAG_RE = re.compile(r"<\|[^|]*\|>")

VERSION = "2.2.0"


# ---------------------------------------------------------------------------
# 输出 helpers
# ---------------------------------------------------------------------------

def _init_console() -> None:
    """Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


class AppError(RuntimeError):
    """带稳定错误码的业务异常。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(f"[{code}] {message}")

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "error": self.message, "hint": self.hint}


def first_line(text: str) -> str:
    """视频号标题常带换行和 hashtag，取第一行有效内容做展示与文件名。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def sanitize(name: str, max_len: int = 60) -> str:
    name = first_line((name or "").replace("\u3000", " "))
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip("_")
    return name[:max_len].strip("_") or "video"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GB"


def format_clock(sec: float) -> str:
    total = int(round(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def clean_text(text: str) -> str:
    text = _TAG_RE.sub("", text or "").replace("\u200b", " ")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# SSL / HTTP
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


def _opener():
    """禁用系统代理（元宝与视频号 CDN 在国内直连更快更稳）。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
    )


def _http_json(url: str, method: str = "GET", payload=None, headers=None, timeout: int = 30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raise AppError("E_NETWORK", f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as exc:
        raise AppError("E_NETWORK", f"网络请求失败：{exc.reason}",
                       "检查网络连接，或确认是否需要关闭代理") from None
    except TimeoutError:
        raise AppError("E_NETWORK", "请求超时", "稍后重试，或检查网络") from None
    except (OSError, ValueError) as exc:
        raise AppError("E_NETWORK", f"网络异常：{type(exc).__name__}: {exc}",
                       "网络不稳定时稍后重试") from None
    try:
        return (json.loads(body) if body else {}), status
    except json.JSONDecodeError:
        raise AppError("E_NETWORK", f"接口返回不是 JSON：{body[:160]}") from None


RETRYABLE_CODES = ("E_NETWORK", "E_FEED")


def _retry(fn, label: str = "", attempts: int = 3, base: float = 1.5):
    """网络类失败短退避重试；确定性失败（链接无效等）立即抛出，不浪费时间。"""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except AppError as exc:
            if exc.code not in RETRYABLE_CODES or i >= attempts:
                raise
            last = exc
        except (TimeoutError, OSError) as exc:
            if i >= attempts:
                raise AppError("E_NETWORK", f"{label}多次超时：{type(exc).__name__}: {exc}",
                               "网络不稳定，稍后重试") from None
            last = exc
        delay = base * i
        say(f"      {label}失败，{delay:.0f}s 后重试（第 {i}/{attempts - 1} 次）：{str(last)[:70]}")
        time.sleep(delay)
    if last is not None:
        raise AppError("E_NETWORK", f"{label}重试后仍失败：{last}", "稍后重试")
    raise AppError("E_NETWORK", f"{label}失败")


# ---------------------------------------------------------------------------
# 元宝登录态
# ---------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.is_file():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(state) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


def cookie_header(state) -> str:
    if not state:
        return ""
    parts, seen = [], set()
    for cookie in state.get("cookies") or []:
        domain = cookie.get("domain") or ""
        if "tencent.com" not in domain and "yuanbao" not in domain:
            continue
        name = cookie.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def _yb_headers(cookie: str, extra=None) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": YUANBAO_ORIGIN,
        "Referer": f"{YUANBAO_ORIGIN}/chat/{AGENT_ID}",
        "User-Agent": DESKTOP_UA,
        "X-Requested-With": "XMLHttpRequest",
        "X-Source": "web",
        "X-Platform": "mac",
        "Cookie": cookie,
    }
    if extra:
        headers.update(extra)
    return headers


def _pick_user_id(info) -> str:
    data = info.get("data") if isinstance(info.get("data"), dict) else info
    if not isinstance(data, dict):
        return ""
    for key in ("userId", "userid", "user_id", "id"):
        if data.get(key):
            return str(data[key])
    return ""


def http_get_userinfo(cookie: str):
    parsed, status = _http_json(
        f"{YUANBAO_ORIGIN}/api/getuserinfo", "GET", headers=_yb_headers(cookie))
    if status != 200:
        raise AppError("E_AUTH", f"getuserinfo 失败：HTTP {status}")
    return parsed


def http_parse_share(share_url: str, cookie: str, user_id: str = "") -> dict:
    extra = {"Content-Type": "application/json", "X-AgentID": AGENT_ID}
    if user_id:
        extra["T-UserID"] = user_id
        extra["X-ID"] = user_id
    parsed, status = _http_json(
        f"{YUANBAO_ORIGIN}/api/weixin/get_parse_result",
        "POST",
        payload={"type": "video_channel_url", "url": share_url, "scene": 1},
        headers=_yb_headers(cookie, extra),
    )
    if status != 200:
        raise AppError("E_PARSE", f"解析接口失败：HTTP {status}")
    if parsed.get("code") not in (None, 0):
        raise AppError("E_PARSE", f"元宝解析失败：{parsed.get('msg') or parsed.get('message') or parsed.get('code')}")
    data = parsed.get("data") or {}
    if not data.get("playable_url") and not data.get("wx_export_id"):
        raise AppError("E_PARSE_EMPTY",
                       "元宝已响应但没有返回播放地址",
                       "可能是链接失效、内容受限（私密/付费/已删除），或接口发生变化")
    return data


def _generate_rid() -> str:
    import random
    return f"{int(time.time()):x}-" + "".join(random.choice("0123456789abcdef") for _ in range(8))


def fetch_feed_info(token: str, eid: str):
    rid = _generate_rid()
    api_url = (
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
        f"?_rid={rid}&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
    )
    referer = (
        "https://channels.weixin.qq.com/finder-preview/pages/feed"
        "?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={urllib.parse.quote(token)}&entry_scene=0&eid={urllib.parse.quote(eid)}"
    )
    payload = json.dumps({"baseReq": {"generalToken": token}, "exportId": eid}).encode("utf-8")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": DESKTOP_UA,
    }
    req = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")
    try:
        with _opener().open(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"网络失败：{getattr(exc, 'reason', exc)}"
    try:
        return json.loads(body), None
    except json.JSONDecodeError:
        return None, "get_feed_info 返回不是 JSON"


def _coerce_duration(value) -> int:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    if num > 10000:
        num /= 1000.0
    return int(round(num)) if 1 <= num <= 36000 else 0


def extract_duration(feed) -> int:
    data = (feed or {}).get("data") or {}
    feed_info = data.get("feedInfo") or {}
    for blob in (feed_info, feed_info.get("h264VideoInfo") or {},
                 feed_info.get("h265VideoInfo") or {}, data):
        if not isinstance(blob, dict):
            continue
        for key in ("videoPlayLen", "videoDuration", "duration", "playLen", "videoLen"):
            dur = _coerce_duration(blob.get(key))
            if dur:
                return dur
    return 0


def token_eid(parsed: dict):
    playable = parsed.get("playable_url") or ""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(playable).query)
    token = (qs.get("token") or [""])[0]
    eid = (qs.get("eid") or qs.get("exportId") or [""])[0] or (parsed.get("wx_export_id") or "")
    return token, eid


def build_profile(share_url: str, feed) -> dict:
    data = (feed or {}).get("data") or {}
    info = data.get("feedInfo") or {}
    author_info = data.get("authorInfo") or {}
    direct = ((info.get("h264VideoInfo") or {}).get("videoUrl") or "").strip() \
        or (info.get("videoUrl") or "").strip() \
        or ((info.get("h265VideoInfo") or {}).get("videoUrl") or "").strip()
    author = author_info.get("nickname") or ""
    desc = info.get("description") or ""
    return {
        "title": f"{author}-{desc}" if author else (desc or "视频号视频"),
        "author": author,
        "description": desc,
        "source_url": share_url,
        "direct_url": direct,
        "duration": extract_duration(feed),
        "stats": {
            "点赞": info.get("likeCountFmt") or "-",
            "收藏": info.get("favCountFmt") or "-",
            "转发": info.get("forwardCountFmt") or "-",
            "评论": info.get("commentCountFmt") or "-",
        },
    }


def check_auth() -> dict:
    """检查元宝登录态。纯 HTTP，不启动浏览器。"""
    state = load_state()
    if not state:
        return {"ok": False, "code": "E_AUTH_REQUIRED",
                "error": "还没有元宝登录态",
                "hint": "运行：python sph2txt.py --login  （或双击「首次登录」脚本）"}
    cookie = cookie_header(state)
    if not cookie:
        return {"ok": False, "code": "E_AUTH_INVALID",
                "error": "登录态文件里没有有效 Cookie",
                "hint": "重新运行：python sph2txt.py --login"}
    try:
        user_id = _pick_user_id(http_get_userinfo(cookie))
    except AppError as exc:
        if exc.code == "E_NETWORK":
            # 网络问题不等于登录过期，不能误导用户去重扫
            return {"ok": False, "code": "E_NETWORK",
                    "error": f"检查登录态时网络失败：{exc.message}",
                    "hint": "网络恢复后重试；不需要重新扫码"}
        return {"ok": False, "code": "E_AUTH_EXPIRED",
                "error": f"登录态已失效（{exc.message}）",
                "hint": "重新运行：python sph2txt.py --login"}
    if not user_id:
        return {"ok": False, "code": "E_AUTH_EXPIRED",
                "error": "登录态已失效（未取到用户信息）",
                "hint": "重新运行：python sph2txt.py --login"}
    return {"ok": True, "userId": user_id}


def resolve(share_url: str) -> dict:
    """视频号链接 → 直链 + 元信息。网络类失败自动重试。"""

    def _do() -> dict:
        auth = check_auth()
        if not auth.get("ok"):
            raise AppError(auth["code"], auth["error"], auth.get("hint", ""))
        cookie = cookie_header(load_state())
        parsed = http_parse_share(share_url, cookie, auth.get("userId", ""))
        token, eid = token_eid(parsed)
        if not token or not eid:
            raise AppError("E_PARSE", "元宝返回结果缺少 token/eid，无法获取视频详情",
                           "换一条链接试试；若持续出现，可能是接口变化")
        feed, err = fetch_feed_info(token, eid)
        if err or not feed:
            raise AppError("E_FEED", f"获取视频详情失败：{err or '空响应'}",
                           "稍后重试；或确认该视频是否可公开访问")
        profile = build_profile(share_url, feed)
        if not profile["direct_url"]:
            raise AppError("E_STREAM_EMPTY", "视频详情已返回，但没有可下载的视频流",
                           "可能是私密/付费内容，或接口字段变化")
        return profile

    return _retry(_do, label="解析")


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def _host_allowed(host: str) -> bool:
    import ipaddress
    host = (host or "").lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(host == s or host.endswith("." + s) for s in MEDIA_SUFFIXES)


def validate_media_url(raw: str) -> str:
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except (TypeError, ValueError, AttributeError):
        raise AppError("E_UNTRUSTED_MEDIA", "视频地址格式异常，已停止下载") from None
    if parts.scheme.lower() != "https" or not host or not _host_allowed(host):
        raise AppError("E_UNTRUSTED_MEDIA",
                       f"视频地址域名不在可信清单内：{host or '未知'}",
                       "为安全起见已停止。若确认可靠，请联系维护者评估")
    return raw


def download_media(url: str, dest: Path, label: str = "") -> Path:
    """分块下载，支持断点续传，校验大小上限。"""
    validate_media_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    have = 0
    if part.is_file():
        have = part.stat().st_size
        if have > MAX_BYTES:
            part.unlink()
            have = 0

    headers = {"User-Agent": DESKTOP_UA, "Accept": "*/*"}
    if have:
        headers["Range"] = f"bytes={have}-"

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = _opener().open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        raise AppError("E_DOWNLOAD", f"下载失败：HTTP {exc.code}",
                       "视频可能已过期，重新解析该链接即可") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AppError("E_DOWNLOAD", f"下载连接失败：{getattr(exc, 'reason', exc)}") from None

    with resp:
        status = getattr(resp, "status", 200)
        if have and status != 206:
            have = 0
            part.unlink(missing_ok=True)
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0) + have
        except (TypeError, ValueError):
            total = 0
        if total > MAX_BYTES:
            raise AppError("E_DOWNLOAD", f"视频超过大小上限 {human_size(MAX_BYTES)}")

        written = have
        started = time.time()
        last = 0.0
        with part.open("ab" if have else "wb") as fh:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                if written > MAX_BYTES:
                    fh.close()
                    part.unlink(missing_ok=True)
                    raise AppError("E_DOWNLOAD", "视频超过大小上限，已中止")
                now = time.time()
                if now - last > 0.4:
                    last = now
                    speed = (written - have) / max(now - started, 0.001) / 1048576
                    if total:
                        say(f"      {human_size(written)} / {human_size(total)}"
                            f"  {written / total * 100:5.1f}%  {speed:4.1f} MB/s")
                    else:
                        say(f"      {human_size(written)}  {speed:4.1f} MB/s")

    if total and part.stat().st_size != total:
        raise AppError("E_DOWNLOAD",
                       f"下载不完整：期望 {human_size(total)}，实际 {human_size(part.stat().st_size)}",
                       "重跑同一命令会断点续传")
    os.replace(part, dest)
    if label:
        say(f"      已保存 {dest.name}  {human_size(dest.stat().st_size)}")
    return dest


# ---------------------------------------------------------------------------
# 音频抽取
# ---------------------------------------------------------------------------

def find_ffmpeg() -> str:
    """优先级：imageio-ffmpeg 自带 > 系统 ffmpeg。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    for name in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise AppError("E_NO_FFMPEG", "找不到 ffmpeg",
                   "运行 python sph2txt.py --setup 会自动装好，无需手动安装")


def extract_wav(src: Path, wav: Path) -> float:
    """抽 16k 单声道 PCM，返回时长（秒）。"""
    ffmpeg = find_ffmpeg()
    wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav.with_suffix(".part.wav")
    tmp.unlink(missing_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1024:
        err = (proc.stderr or "").strip()[:300]
        tmp.unlink(missing_ok=True)
        raise AppError("E_NO_AUDIO",
                       f"抽取音频失败（该视频可能没有音轨）：{err}",
                       "纯画面无配音的视频无法转写")
    os.replace(tmp, wav)
    with wave.open(str(wav), "rb") as f:
        return f.getnframes() / f.getframerate()


# ---------------------------------------------------------------------------
# SenseVoice 识别
# ---------------------------------------------------------------------------

_RECOGNIZER = None


def resolve_model_dir() -> Path | None:
    candidates = [
        Path(os.environ.get("SPH_MODEL_DIR", "")).expanduser() if os.environ.get("SPH_MODEL_DIR") else None,
        MODEL_DIR,
        Path.home() / ".cache" / "mp4-transcript" / "sensevoice",
    ]
    for path in candidates:
        if path and (path / "model.int8.onnx").is_file() and (path / "tokens.txt").is_file():
            return path
    return None


def get_recognizer(model_dir: Path, threads: int):
    global _RECOGNIZER
    if _RECOGNIZER is not None:
        return _RECOGNIZER
    try:
        import sherpa_onnx
    except ImportError:
        raise AppError("E_NO_DEPS", "缺少 sherpa-onnx",
                       "运行 python sph2txt.py --setup 自动安装") from None
    _RECOGNIZER = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_dir / "model.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=max(1, threads),
        language="zh",
        use_itn=True,
        debug=False,
    )
    return _RECOGNIZER


def _energy_db(f, sr: int) -> "object":
    """流式算出 20ms 帧的 RMS 能量（dB）。不整段载入内存，81 分钟也只要几 MB。"""
    import numpy as np

    hop = max(1, int(sr * ENERGY_HOP))
    f.setpos(0)
    chunks: list = []
    carry = np.zeros(0, dtype=np.float32)
    while True:
        raw = f.readframes(hop * 200)
        if not raw:
            break
        block = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if carry.size:
            block = np.concatenate((carry, block))
        usable = (block.size // hop) * hop
        carry = block[usable:] if usable < block.size else np.zeros(0, dtype=np.float32)
        if usable:
            frames = block[:usable].reshape(-1, hop)
            rms = np.sqrt(np.mean(frames * frames, axis=1))
            chunks.append(20.0 * np.log10(np.maximum(rms, 1e-6)))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _silence_threshold(db) -> float:
    """自适应静音阈值：取能量分布的低位噪声底 + 一段余量。"""
    import numpy as np

    if db.size < 10:
        return -50.0
    low = float(np.percentile(db, 10))
    high = float(np.percentile(db, 90))
    span = high - low
    if span < 6.0:                       # 全程背景音/音乐，动态范围小
        return low + max(2.5, span * 0.35)
    return low + span * 0.20


def _silence_at(db, idx: int, threshold: float) -> float:
    """从 idx 向两侧扩展，量出这段静音有多长（秒）。"""
    n = db.size
    lo = idx
    while lo > 0 and db[lo - 1] < threshold:
        lo -= 1
    hi = idx
    while hi + 1 < n and db[hi + 1] < threshold:
        hi += 1
    return (hi - lo + 1) * ENERGY_HOP


def plan_cuts(db, total_sec: float) -> tuple[list[float], list[float]]:
    """在每个 30s 锚点 ±3s 内找能量最低点（换气处）下刀。

    返回 (切点秒列表, 各切点处静音时长)。静音时长后续用于判断段落边界。
    """
    import numpy as np

    n = db.size
    if n < 10:
        return [], []
    threshold = _silence_threshold(db)
    cuts: list[float] = []
    pauses: list[float] = []
    prev = 0.0
    anchor = CHUNK_SEC
    while anchor < total_sec - MIN_CHUNK_SEC:
        lo_sec = max(prev + MIN_CHUNK_SEC, anchor - BOUNDARY_SEARCH)
        hi_sec = min(total_sec, anchor + BOUNDARY_SEARCH, prev + MAX_CHUNK_SEC)
        lo = max(0, int(lo_sec / ENERGY_HOP))
        hi = min(n, max(lo + 2, int(hi_sec / ENERGY_HOP)))
        if lo >= n - 1 or hi <= lo:
            break
        idx = lo + int(np.argmin(db[lo:hi]))
        cut = idx * ENERGY_HOP
        if cut <= prev:                  # 理论上不会发生，兜底防死循环
            break
        cuts.append(cut)
        pauses.append(_silence_at(db, idx, threshold))
        prev = cut
        anchor += CHUNK_SEC
    return cuts, pauses


def transcribe(wav: Path, threads: int = 4, on_progress=None) -> tuple[list[dict], float]:
    """分块识别，返回 (items, 音频时长)。items 含 start/end/text/pause。"""
    import numpy as np

    model_dir = resolve_model_dir()
    if model_dir is None:
        raise AppError("E_NO_MODEL", "找不到 SenseVoice 模型",
                       "运行 python sph2txt.py --setup 自动下载（约 229 MB）")
    recognizer = get_recognizer(model_dir, threads)

    with wave.open(str(wav), "rb") as f:
        if f.getnchannels() != 1 or f.getsampwidth() != 2 or f.getframerate() != 16000:
            raise AppError("E_ASR", "音频格式异常（需要 16k 单声道 16bit）")
        sr = f.getframerate()
        frames_total = f.getnframes()
        total_sec = frames_total / sr

        cuts, pauses = plan_cuts(_energy_db(f, sr), total_sec)
        bounds = [0.0] + cuts + [total_sec]
        seg_pause = [0.0] + pauses          # seg_pause[i] = 第 i 块前的静音时长

        total_chunks = len(bounds) - 1
        results: list[dict] = []
        for batch_start in range(0, total_chunks, DECODE_BATCH):
            indices = list(range(batch_start, min(batch_start + DECODE_BATCH, total_chunks)))
            streams, metas = [], []
            for idx in indices:
                begin = int(round(bounds[idx] * sr))
                end = int(round(bounds[idx + 1] * sr))
                count = max(0, end - begin)
                if count <= 0:
                    continue
                f.setpos(begin)
                raw = f.readframes(count)
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                stream = recognizer.create_stream()
                stream.accept_waveform(sr, samples)
                streams.append(stream)
                metas.append((bounds[idx], bounds[idx + 1], seg_pause[idx]))
            if not streams:
                continue
            recognizer.decode_streams(streams)
            for stream, (start, end, pause) in zip(streams, metas):
                text = clean_text(stream.result.text)
                if text:
                    results.append({"start": start, "end": end,
                                    "text": text, "pause": pause})
            if on_progress:
                on_progress(min(batch_start + len(indices), total_chunks), total_chunks)
        return results, total_sec


def _split_sentences(text: str) -> list[str]:
    """按句末标点切成句子，保留标点。"""
    out = [m.group().strip() for m in _SENT_RE.finditer(text or "")]
    return [s for s in out if s]


def _split_by_marker(sentence: str) -> list[str]:
    """句子内部若夹着「第X杯/首先/最后」这类结构词，也在此处断开。"""
    if len(sentence) < 50:
        return [sentence]
    marks = [m.start() for m in _PARA_HEAD_ANY.finditer(sentence) if m.start() >= 15]
    if not marks:
        return [sentence]
    out: list[str] = []
    last = 0
    for pos in marks:
        if pos - last < 15:
            continue
        out.append(sentence[last:pos])
        last = pos
    out.append(sentence[last:])
    return [s for s in out if s.strip()]


def _to_units(items: list[dict]) -> list[dict]:
    """把识别块拆成句子级单元，并标注：是否为段落开头候选、前面停顿多长。"""
    units: list[dict] = []
    for it in items:
        pause = it.get("pause") or 0.0
        sentences = _split_sentences(it.get("text") or "")
        for si, sent in enumerate(sentences):
            pieces = _split_by_marker(sent)
            for pi, piece in enumerate(pieces):
                units.append({
                    "text": piece,
                    "pause": pause if (si == 0 and pi == 0) else 0.0,
                    "head": bool(_PARA_HEAD.match(piece)),
                })
    return units


def _join_units(units: list[dict]) -> str:
    """把句子单元拼成一段；英文单词被切开时补空格。"""
    out = ""
    for u in units:
        text = u["text"]
        if out and out[-1].isascii() and out[-1].isalnum() \
                and text and text[0].isascii() and text[0].isalnum():
            out += " "
        out += text
    return out


def _balance(units: list[dict], max_chars: int) -> list[list[dict]]:
    """一节过长时，按句子边界均分成若干段，避免切出孤零零的短尾巴。"""
    total = sum(len(u["text"]) for u in units)
    if total <= max_chars or len(units) <= 1:
        return [units]
    parts = -(-total // max_chars)
    target = total / parts
    out: list[list[dict]] = []
    cur: list[dict] = []
    acc = 0
    for u in units:
        cur.append(u)
        acc += len(u["text"])
        if acc >= target and len(out) < parts - 1:
            out.append(cur)
            cur, acc = [], 0
    if cur:
        out.append(cur)
    return out


def build_text(items: list[dict], timestamps: bool = False) -> str:
    """按语义分段输出纯文本。

    两步走：
      1. 分节 —— 以「第二杯 / 首先 / 接下来 / 最后」这类结构词，
         或明显长停顿（>= PARA_PAUSE_SEC）为界；
      2. 节内再平衡 —— 一节超过 PARA_MAX_CHARS 时按句子边界均分，
         避免「掐着上限切、留下一句孤段」。
    """
    if timestamps:
        lines = [f"[{format_clock(it['start'])}] {it['text']}" for it in items]
        return "\n".join(lines) + "\n" if lines else ""

    sections: list[list[dict]] = []
    cur: list[dict] = []
    used = 0
    for unit in _to_units(items):
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        item = {"text": text, "pause": unit.get("pause") or 0.0, "head": unit.get("head")}
        if cur and ((item["head"] and used >= PARA_HEAD_MIN) or
                    (item["pause"] >= PARA_PAUSE_SEC and used >= PARA_MIN_CHARS)):
            sections.append(cur)
            cur, used = [], 0
        cur.append(item)
        used += len(text)
    if cur:
        sections.append(cur)

    paras: list[str] = []
    for section in sections:
        for part in _balance(section, PARA_MAX_CHARS):
            text = _join_units(part).strip()
            if text:
                paras.append(text)
    return "\n\n".join(paras) + "\n" if paras else ""


# ---------------------------------------------------------------------------
# 单条处理
# ---------------------------------------------------------------------------

def process_one(share_url: str, out_dir: Path, *, keep_video=False,
                timestamps=False, threads=4, index: int = 0, total: int = 1) -> dict:
    tag = f"[{index}/{total}] " if total > 1 else ""
    say("")
    say(f"{tag}① 解析链接  {share_url}")

    profile = resolve(share_url)
    title = sanitize(profile["title"])
    dur = profile.get("duration") or 0
    say(f"      标题：{first_line(profile['title'])}")
    if profile.get("author"):
        say(f"      作者：{profile['author']}"
            + (f"    时长：{format_clock(dur)}" if dur else ""))
    stats = {k: v for k, v in (profile.get("stats") or {}).items() if v not in ("-", None, "")}
    if stats:
        say("      数据：" + "  ".join(f"{k} {v}" for k, v in stats.items()))

    stem = f"{index:02d}_{title}" if total > 1 and index else title
    # 过程文件全部落系统临时目录，输出目录只留最终 txt
    work = Path(tempfile.mkdtemp(prefix="sph-work-"))
    video_dir = out_dir / "videos" if keep_video else work
    mp4 = video_dir / f"{stem}.mp4"
    wav = work / f"{stem}.wav"

    try:
        say("② 下载视频…")
        if mp4.is_file() and mp4.stat().st_size > 100 * 1024:
            say(f"      复用已下载文件 {mp4.name}")
            download_path = mp4
        else:
            download_path = download_media(profile["direct_url"], mp4)

        say("③ 抽取音频…")
        audio_sec = extract_wav(download_path, wav)
        if dur <= 0:
            dur = audio_sec
        say(f"      时长 {format_clock(audio_sec)}")

        say("④ 本地识别（SenseVoice，不上传）…")

        def progress(i, n):
            say(f"      识别 {i}/{n} 块")

        items, _ = transcribe(wav, threads=threads, on_progress=progress)
        if not items:
            raise AppError("E_ASR_EMPTY", "识别结果为空",
                           "该视频可能全程无对白，或音质过低")

        text = build_text(items, timestamps)
        if not text.strip():
            raise AppError("E_ASR_EMPTY", "识别结果为空", "换一条链接试试")

        out_dir.mkdir(parents=True, exist_ok=True)
        txt_path = out_dir / f"{stem}.txt"
        txt_path.write_text(text, encoding="utf-8")
        chars = sum(len(it["text"]) for it in items)
        if timestamps:
            paras = len([ln for ln in text.splitlines() if ln.strip()])
        else:
            paras = len([b for b in text.split("\n\n") if b.strip()])

        say("")
        say(f"      完成：{chars} 字 / {paras} 段 / {format_clock(dur)}")
        say(f"      逐字稿：{txt_path}")

        return {"ok": True, "url": share_url, "title": first_line(profile["title"]),
                "txt": str(txt_path), "chars": chars, "paragraphs": paras,
                "duration": round(dur, 1)}

    finally:
        # 无论是否保留 mp4，临时目录（含 wav / 未保留的 mp4）一律删除
        shutil.rmtree(work, ignore_errors=True)
        _prune_empty(out_dir)


def _prune_empty(out_dir: Path) -> None:
    """兼容老版本留下的空 _work 目录。"""
    work_root = out_dir / "_work"
    if work_root.is_dir() and not any(work_root.iterdir()):
        try:
            work_root.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------

def collect_urls(args) -> list[str]:
    raw: list[str] = list(args.urls or [])
    for u in args.url or []:
        raw.append(u)
    if args.links_file:
        path = Path(args.links_file).expanduser()
        if not path.is_file():
            raise AppError("E_NO_LINK", f"清单文件不存在：{path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)

    seen, urls = set(), []
    for item in raw:
        for token in str(item).split():
            token = token.strip().strip('"\'，,；;')
            if not token:
                continue
            if not re.search(r"https?://", token):
                raise AppError("E_INVALID_LINK", f"不是有效链接：{token}",
                               "需要形如 https://weixin.qq.com/sph/xxxxx 的分享链接")
            if token not in seen:
                seen.add(token)
                urls.append(token)
    if not urls:
        raise AppError("E_NO_LINK", "没有提供视频号链接",
                       "用法：python sph2txt.py \"<链接>\"")
    return urls


# ---------------------------------------------------------------------------
# 登录（playwright）
# ---------------------------------------------------------------------------

LOGIN_JS = r"""
() => {
  const webApi = window.$webApi;
  if (!webApi?.getYbCommonHeaders || !webApi?.setContextualRequestHeaders) {
    return {ready: false, loggedIn: false};
  }
  return new Promise(async (resolve) => {
    try {
      const request = {
        url: "/api/getuserinfo",
        headers: {
          ...webApi.getYbCommonHeaders(),
          "Accept": "application/json, text/plain, */*",
          "X-Requested-With": "XMLHttpRequest"
        }
      };
      await webApi.setContextualRequestHeaders(request);
      const response = await fetch(request.url, {
        method: "GET", credentials: "include", headers: request.headers
      });
      resolve({ready: true, loggedIn: response.ok, status: response.status});
    } catch (e) {
      resolve({ready: true, loggedIn: false, error: String(e)});
    }
  });
}
"""


async def _launch_browser(p, headless: bool):
    """依次尝试系统 Edge / Chrome / playwright 内置 Chromium（异步版）。"""
    errors = []
    for channel in ("msedge", "chrome", None):
        try:
            kwargs = {"headless": headless}
            if channel:
                kwargs["channel"] = channel
            return await p.chromium.launch(**kwargs), (channel or "chromium")
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0][:80]}")
    raise AppError(
        "E_NO_BROWSER",
        "无法启动浏览器：" + " | ".join(errors),
        "请安装 Microsoft Edge 或 Google Chrome；或运行 python sph2txt.py --setup 安装内置浏览器",
    )


async def _login_session(headless: bool, shot_path: str | None, wait_sec: int) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser, used = await _launch_browser(p, headless)
        context = await browser.new_context(
            user_agent=DESKTOP_UA, viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto(f"{YUANBAO_ORIGIN}/", wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_function(
                "() => !!(window.$webApi && window.$webApi.getYbCommonHeaders)", timeout=12000)
        except Exception:
            pass

        # 元宝登录弹窗是微信官方 iframe (open.weixin.qq.com/connect/qrconnect)。
        # 两种登录方式：① 本机记住了微信 → 绿色「微信快捷登录」按钮（手机确认即可）；
        #               ② 否则 → 二维码扫码。优先走①，同时把弹窗截图留作扫码兜底。
        quick_clicked = False
        try:
            await page.wait_for_selector('iframe[src*=qrconnect]', timeout=30000)
            frame = page.frame_locator('iframe[src*=qrconnect]')
            # 先看有没有快捷登录按钮。
            # 注意：iframe 里这个按钮用普通 .click() 会被遮挡而静默失效，
            # 必须用 JS 的 b.click() 直接触发（实测有效）。
            try:
                quick = frame.locator('button.js_quick_login_btn').first
                await quick.wait_for(state="visible", timeout=6000)
                await quick.evaluate("b => b.click()")
                quick_clicked = True
                say("[快捷登录] 已点击「微信快捷登录」，请在手机微信点『确认登录』")
            except Exception:
                pass

            # 截图兜底：若能切回扫码模式，单独截二维码；否则截整页弹窗
            if shot_path:
                try:
                    if quick_clicked:
                        # 快捷登录已点，尝试切回扫码界面拿纯净二维码
                        try:
                            other = frame.locator('button.js_switchToNormal').first
                            await other.click(timeout=3000)
                            await page.wait_for_timeout(1500)
                        except Exception:
                            pass
                    qr = frame.locator('img.js_qrcode_img').first
                    await qr.wait_for(state="visible", timeout=8000)
                    for _ in range(40):
                        if await qr.evaluate("img => img.naturalWidth"):
                            break
                        await page.wait_for_timeout(500)
                    await qr.screenshot(path=shot_path, timeout=8000)
                    say(f"[二维码] 备用二维码已截图：{shot_path}")
                except Exception:
                    await page.screenshot(path=shot_path)
                    say(f"[截图] 登录弹窗已截图：{shot_path}")
        except Exception:
            if shot_path:
                try:
                    await page.screenshot(path=shot_path)
                except Exception:
                    pass

        login = await page.evaluate(LOGIN_JS)
        deadline = time.time() + wait_sec
        check_no = 0
        while not login.get("loggedIn") and time.time() < deadline:
            await page.wait_for_timeout(3000)
            check_no += 1
            login = await page.evaluate(LOGIN_JS)
            # 快捷登录场景下若 15 秒还没登录，可能是确认弹窗被关，尝试再点一次
            if quick_clicked and check_no == 5 and not login.get("loggedIn"):
                try:
                    await frame.locator('button.js_quick_login_btn').first.evaluate("b => b.click()")
                    say("[快捷登录] 再次点击「微信快捷登录」，请留意手机确认")
                except Exception:
                    pass

        state = None
        if login.get("loggedIn"):
            try:
                state = await context.storage_state()
            except Exception:
                state = None
        await browser.close()
        return {"login": login, "state": state, "browser": used}


def do_login(headless=False, shot_path=None, wait_sec=180) -> int:
    say(f"启动元宝登录（浏览器：{'无头' if headless else '有头'}）…")
    try:
        result = asyncio.run(_login_session(headless, shot_path, wait_sec))
    except AppError as exc:
        say("")
        say(f"[失败] {exc.message}")
        if exc.hint:
            say(f"        {exc.hint}")
        return 1
    except Exception as exc:
        say("")
        say(f"[失败] 登录流程出错：{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
        say("        如果是无头模式，改用有头：python sph2txt.py --login")
        return 1

    login = result.get("login") or {}
    if login.get("loggedIn") and result.get("state"):
        save_state(result["state"])
        say("")
        say(f"[成功] 登录态已保存：{STATE_FILE}")
        say(f"       浏览器：{result.get('browser')}")
        say("       以后直接贴链接即可出稿，无需再扫码。")
        return 0
    say("")
    say("[失败] 未检测到登录。请重试并确认在浏览器窗口里完成了扫码确认。")
    if headless:
        say("        无头模式下扫码较难操作，建议改用有头：python sph2txt.py --login")
    return 1


# ---------------------------------------------------------------------------
# 环境准备 / 体检
# ---------------------------------------------------------------------------

REQUIRED_PKGS = [("sherpa-onnx", "sherpa_onnx"), ("numpy", "numpy")]
OPTIONAL_PKGS = [("imageio-ffmpeg", "imageio_ffmpeg"), ("playwright", "playwright")]


def _pkg_ok(module_name: str) -> bool:
    import importlib
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def _pip_install(packages: list[str]) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *packages]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        say("  [失败]  pip 安装失败，输出尾部：")
        say("          " + (proc.stderr or proc.stdout or "").strip()[-500:].replace("\n", "\n          "))
        say("          可手动执行：" + " ".join(cmd))
        return False
    return True


def _has_system_browser() -> bool:
    """检测系统自带 Chrome / Edge，有的话不必下载 playwright 内置 Chromium。"""
    if IS_WIN:
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
        ]
        return any(p.is_file() for p in candidates)
    mac_paths = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ]
    if any(p.is_file() for p in mac_paths):
        return True
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(name):
            return True
    return False


def _download_file(url: str, dest: Path, expected: int) -> None:
    if dest.is_file() and dest.stat().st_size == expected:
        return
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.is_file() else 0
    if have > expected:
        part.unlink()
        have = 0
    headers = {"User-Agent": f"sph-transcript/{VERSION}"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        status = getattr(resp, "status", 200)
        if have and status != 206:
            have = 0
        written = have
        started = time.time()
        last = 0.0
        with part.open("ab" if have else "wb") as fh:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                written += len(block)
                now = time.time()
                if now - last > 0.3 or written >= expected:
                    last = now
                    speed = (written - have) / max(now - started, 0.001) / 1048576
                    sys.stdout.write(
                        f"\r      {dest.name}  {written/1048576:6.1f}/{expected/1048576:.0f} MB"
                        f"  {written/expected*100:5.1f}%  {speed:4.1f} MB/s   ")
                    sys.stdout.flush()
        sys.stdout.write("\n")
    if part.stat().st_size != expected:
        raise RuntimeError(f"大小不符：期望 {expected}，实际 {part.stat().st_size}")
    os.replace(part, dest)


def download_models(force=False) -> bool:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not force and resolve_model_dir() == MODEL_DIR:
        say("  [OK]    SenseVoice 模型已就绪")
        return True

    say(f"  下载 SenseVoice 模型到 {MODEL_DIR}（共约 229 MB，只需一次）")
    for name, expected in MODEL_FILES.items():
        dest = MODEL_DIR / name
        if dest.is_file() and dest.stat().st_size == expected:
            say(f"  [OK]    {name} 已存在")
            continue
        ok = False
        for label, template in model_sources():
            url = template.format(repo=MODEL_REPO, name=name)
            try:
                say(f"  [下载]  {name}  <- {label}")
                _download_file(url, dest, expected)
                say(f"  [OK]    {name} 完成")
                ok = True
                break
            except Exception as exc:
                say(f"          失败：{type(exc).__name__}: {str(exc)[:120]}")
        if not ok:
            say(f"  [失败]  {name} 所有源都下载失败")
            say("          手动方案一：浏览器打开下列地址下载，放进目标目录")
            for label, template in model_sources():
                say(f"            {template.format(repo=MODEL_REPO, name=name)}")
            say(f"          目标目录：{MODEL_DIR}")
            say("          手动方案二：从已装好的电脑拷贝整个 sensevoice 目录，")
            say("                     或用 --import-model <目录或 zip> 导入")
            return False
    return True


def import_model(src: str) -> int:
    """从已有目录或 zip 导入模型，供离线分发。"""
    import zipfile
    src_path = Path(src).expanduser()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if src_path.is_dir():
        copied = []
        for name in MODEL_FILES:
            candidate = src_path / name
            if not candidate.is_file():
                candidate = src_path / "sensevoice" / name
            if candidate.is_file():
                shutil.copy2(candidate, MODEL_DIR / name)
                copied.append(name)
        if len(copied) == len(MODEL_FILES):
            say(f"[OK] 已从目录导入模型：{MODEL_DIR}")
            return 0
        say(f"[失败] 目录里缺少模型文件（找到 {copied or '无'}），需要 {list(MODEL_FILES)}")
        return 1

    if src_path.is_file() and zipfile.is_zipfile(src_path):
        with zipfile.ZipFile(src_path) as zf:
            names = {Path(n).name: n for n in zf.namelist()}
            missing = [n for n in MODEL_FILES if n not in names]
            if missing:
                say(f"[失败] zip 里缺少：{missing}")
                return 1
            for name in MODEL_FILES:
                with zf.open(names[name]) as fsrc, open(MODEL_DIR / name, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
        say(f"[OK] 已从 zip 导入模型：{MODEL_DIR}")
        return 0

    say(f"[失败] 无法识别的导入源：{src_path}")
    return 1


def run_setup() -> int:
    say("=" * 62)
    say(f"  视频号逐字稿 · 环境准备  (v{VERSION})")
    say("=" * 62)

    say("")
    say("① Python")
    if sys.version_info < (3, 9):
        say(f"  [失败]  当前 {sys.version_info.major}.{sys.version_info.minor}，需要 3.9+")
        return 1
    say(f"  [OK]    {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        f"  ({sys.executable})")

    say("")
    say("② Python 依赖")
    missing_req = [p for p, m in REQUIRED_PKGS if not _pkg_ok(m)]
    missing_opt = [p for p, m in OPTIONAL_PKGS if not _pkg_ok(m)]

    if not missing_req and not missing_opt:
        say("  [OK]    全部已安装")
    else:
        if missing_req:
            say(f"  安装必需包：{' '.join(missing_req)}")
            if not _pip_install(missing_req):
                return 1
            say("  [OK]    必需包完成")
        if missing_opt:
            say(f"  安装可选包：{' '.join(missing_opt)}")
            if _pip_install(missing_opt):
                say("  [OK]    可选包完成")
            else:
                say("  [警告]  可选包安装失败，不影响出稿；只影响首次扫码登录")

    say("")
    say("③ 浏览器（仅首次扫码登录需要）")
    if _has_system_browser():
        say("  [OK]    检测到系统自带 Edge 或 Chrome，无需额外下载")
    elif _pkg_ok("playwright"):
        say("  [提示]  未检测到 Edge/Chrome，正在下载内置 Chromium（约 130 MB）…")
        proc = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            say("  [OK]    内置浏览器已就绪")
        else:
            say("  [警告]  内置浏览器下载失败；请安装 Microsoft Edge 或 Google Chrome 后重试")
    else:
        say("  [警告]  未检测到浏览器且 playwright 不可用，扫码登录可能失败")

    say("")
    say("④ ffmpeg")
    try:
        say(f"  [OK]    {find_ffmpeg()}")
    except AppError as exc:
        say(f"  [失败]  {exc.message}")
        say(f"          {exc.hint}")
        return 1

    say("")
    say("⑤ 识别模型")
    if not download_models():
        return 1

    say("")
    say("⑥ 元宝登录态")
    auth = check_auth()
    if auth.get("ok"):
        say(f"  [OK]    已登录（userId={auth.get('userId')}）")
    else:
        say(f"  [待办]  {auth.get('error')}")
        say(f"          下一步执行：python \"{Path(__file__).resolve()}\" --login")

    say("")
    say("=" * 62)
    say("  环境准备完成。")
    if not auth.get("ok"):
        say("  还差一步：完成微信扫码登录（--login）。")
    say("=" * 62)
    return 0


def run_doctor() -> int:
    rows = []
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    rows.append(("Python", py, sys.version_info >= (3, 9), "需要 3.9+"))

    for label, mod in REQUIRED_PKGS:
        ok = _pkg_ok(mod)
        rows.append((label, "已安装" if ok else "缺失", ok,
                     "运行 --setup 自动安装"))
    for label, mod in OPTIONAL_PKGS:
        rows.append((label, "已安装" if _pkg_ok(mod) else "缺失（可选）", True,
                     "仅 --login 需要"))

    try:
        rows.append(("ffmpeg", find_ffmpeg(), True, ""))
    except AppError:
        rows.append(("ffmpeg", "未找到", False, "运行 --setup"))

    model = resolve_model_dir()
    if model:
        size = (model / "model.int8.onnx").stat().st_size
        rows.append(("SenseVoice 模型", f"{model} ({human_size(size)})", True, ""))
    else:
        rows.append(("SenseVoice 模型", "未找到", False, "运行 --setup 下载（229 MB）"))

    state = load_state()
    if not state:
        rows.append(("元宝登录态", "未登录", False, "运行 --login 扫码"))
    else:
        auth = check_auth()
        if auth.get("ok"):
            rows.append(("元宝登录态", f"有效 (userId={auth.get('userId')})", True, ""))
        else:
            rows.append(("元宝登录态", "已过期", False, auth.get("hint", "运行 --login 重扫")))

    say("=" * 62)
    say(f"  环境体检  (v{VERSION}, {sys.platform})")
    say("=" * 62)
    all_ok = True
    for label, value, ok, hint in rows:
        all_ok &= ok
        mark = "OK  " if ok else "缺失"
        say(f"  [{mark}] {label:<16} {value}")
        if not ok and hint:
            say(f"         → {hint}")
    say("=" * 62)
    if all_ok:
        say("  全部就绪。可以直接出稿。")
    else:
        say("  有缺失项，按上面的提示补齐。")
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sph2txt",
        description="视频号链接 → 逐字稿（元宝解析 + 本地 SenseVoice，免费无 API Key）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python sph2txt.py --setup\n"
            "  python sph2txt.py --login\n"
            "  python sph2txt.py \"https://weixin.qq.com/sph/xxxxx\"\n"
            "  python sph2txt.py \"链接1\" \"链接2\" --out D:/逐字稿\n"
            "  python sph2txt.py --links-file links.txt --keep-video\n"
        ),
    )
    ap.add_argument("urls", nargs="*", help="一条或多条视频号分享链接")
    ap.add_argument("--url", action="append", help="链接（可重复，等价于位置参数）")
    ap.add_argument("--links-file", help="链接清单文件，每行一条")
    ap.add_argument("--out", help="输出目录（默认 ./逐字稿）")
    ap.add_argument("--timestamps", dest="timestamps", action="store_true",
                    help="每段前加 [00:00:00] 时间码（默认不加，输出纯净分段文本）")
    ap.add_argument("--keep-video", action="store_true", help="保留 mp4 到 <out>/videos/")
    ap.add_argument("--threads", type=int, default=0, help="识别线程数（默认 CPU 核数一半）")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印结果（仅标准输出，不落盘）")
    ap.add_argument("--check", action="store_true", help="检查登录态")
    ap.add_argument("--doctor", action="store_true", help="环境体检")
    ap.add_argument("--setup", action="store_true", help="准备环境（装依赖 + 下模型）")
    ap.add_argument("--import-model", help="从已有目录或 zip 导入模型（离线安装用）")
    ap.add_argument("--login", action="store_true", help="扫码登录元宝")
    ap.add_argument("--headless", action="store_true", help="登录用无头模式（配合 --shot）")
    ap.add_argument("--shot", help="无头登录时把二维码截图保存到该路径")
    ap.add_argument("--wait", type=int, default=180, help="登录等待秒数（默认 180）")
    ap.add_argument("--version", action="version", version=f"sph2txt {VERSION}")
    ap.set_defaults(timestamps=False)
    return ap


def main(argv=None) -> int:
    _init_console()
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.import_model:
        return import_model(args.import_model)
    if args.setup:
        return run_setup()
    if args.doctor:
        return run_doctor()
    if args.check:
        auth = check_auth()
        say(json.dumps(auth, ensure_ascii=False))
        return 0 if auth.get("ok") else 1
    if args.login:
        return do_login(headless=args.headless, shot_path=args.shot, wait_sec=args.wait)

    try:
        urls = collect_urls(args)
    except AppError as exc:
        say(f"[错误] {exc.message}")
        if exc.hint:
            say(f"       {exc.hint}")
        return 2

    out_dir = Path(args.out).expanduser().resolve() if args.out else Path.cwd() / "逐字稿"
    threads = args.threads or max(1, (os.cpu_count() or 4) // 2)

    say("=" * 62)
    say(f"  视频号逐字稿  v{VERSION}   共 {len(urls)} 条")
    say(f"  输出目录：{out_dir}")
    if args.keep_video:
        say("  保留视频：是")
    say("=" * 62)

    results, failures = [], []
    started = time.time()
    for i, url in enumerate(urls, 1):
        try:
            results.append(process_one(
                url, out_dir,
                keep_video=args.keep_video, timestamps=args.timestamps,
                threads=threads, index=i, total=len(urls),
            ))
        except AppError as exc:
            say("")
            say(f"[失败] {exc.message}")
            if exc.hint:
                say(f"       {exc.hint}")
            failures.append({"url": url, "code": exc.code, "error": exc.message})
        except KeyboardInterrupt:
            say("\n[中断] 用户取消")
            failures.append({"url": url, "code": "E_INTERRUPT", "error": "用户中断"})
            break
        except Exception as exc:
            say("")
            say(f"[失败] 未预期错误：{type(exc).__name__}: {str(exc)[:200]}")
            say("       请把这段错误截图反馈给维护者")
            failures.append({"url": url, "code": "E_UNKNOWN", "error": str(exc)[:200]})

    elapsed = time.time() - started
    say("")
    say("=" * 62)
    say(f"  汇总：成功 {len(results)} / 失败 {len(failures)} / 共 {len(urls)}"
        f"    用时 {elapsed:.0f}s")
    for r in results:
        say(f"    ✓ {r['chars']:>6} 字  {r['txt']}")
    for f in failures:
        say(f"    ✗ [{f['code']}] {f['url']}")
        say(f"      {f['error']}")
    say("=" * 62)

    if args.json:
        say(json.dumps({"ok": not failures, "results": results, "failures": failures},
                       ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
