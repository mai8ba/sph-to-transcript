---
name: sph-to-transcript
slug: sph-to-transcript
display_name: 视频号逐字稿
display_name_en: WeChat Channels Video to Transcript
displayName: 视频号逐字稿
description: |
  微信视频号链接 → 按语义分段好的纯净逐字稿（只有一个 .txt，无时间码、无字幕、无过程文件）。
  支持单条与批量，Windows / macOS 通用。
  用腾讯元宝登录态解析视频号链接拿到视频直链，本地 SenseVoice 模型转写，音视频不出本机。
  全程免费、零 API Key，只需首次一次微信扫码。
  触发场景：
  - 用户贴出 weixin.qq.com/sph/ 或 channels.weixin.qq.com 链接，要「逐字稿 / 转文字 / 文案」
  - 用户说「把这个视频号视频转成文字」「这条视频讲了什么，给我文字稿」
  - 用户一次给多条视频号链接，要批量出稿
  不适用：非视频号平台（抖音/小红书/B站请用对应 skill）、要说话人分离、要翻译。
description_zh: |
  面向内容创作者的微信视频号逐字稿工具。粘贴 weixin.qq.com/sph/ 链接，即可解析视频直链、下载并用本地 SenseVoice 模型转写，按语义分段输出一个纯净 txt（无时间码、无字幕、无过程文件），再由 AI 二次润色修正同音错字与断句。全程免费、零 API Key，首次微信扫码登录一次即可，音视频不出本机。当用户贴出视频号链接要「逐字稿 / 转文字 / 文案」或批量出稿时使用；抖音 / 小红书 / B 站请用对应工具。
description_en: |
  A free, local-first transcript tool for WeChat Channels (Shipinhao) videos. Paste a weixin.qq.com/sph/ link and it resolves the stream, downloads the video and transcribes it with an on-device SenseVoice model — no API key and no upload of audio or video to the cloud. The output is a single clean txt file with semantic paragraphs (no timestamps, no subtitles, no intermediate files), followed by an AI polish pass that fixes homophone errors. A one-time WeChat QR login is all that is required. Use when the user shares a WeChat Channels link and asks for a transcript, text version or script, including batch jobs.
summary: 微信视频号链接一键转纯净逐字稿：本地转写、按语义分段、AI 润色。全程免费零 API，音视频不出本机，首次微信扫码即可。
category: media
version: 2.2.0
author: 麦爸
license: MIT
tags:
- 视频号
- 逐字稿
- 转写
- 免费
- 本地识别
---

# 视频号逐字稿

**一个 skill 搞定，不依赖任何其它 skill。** 链路：

```
视频号链接
  → 元宝 HTTP 解析（拿 CDN 直链，纯请求，不开常驻浏览器）
  → urllib 分块下载 mp4（断点续传 / 域名白名单 / 大小上限）
  → ffmpeg 抽 16k 单声道 WAV（落系统临时目录）
  → 能量包络找换气点：把 30s 硬切点挪到静音最低处，避免切断字词
  → 本地 SenseVoice 分块识别（不上传任何音视频）
  → 按语义合并成段落 → 只输出一个 txt
  → 【AI 二次润色】由本助手读初稿、修错别字和断句 → 另存 _润色版.txt
```

全程免费、零 API Key。只有元宝需要登录一次（微信扫码，长期有效）。

**输出极简**：脚本产出的目录里只有 `.txt`。wav、mp4 等过程文件全在系统临时目录，跑完自动删除。
**要不要留 mp4 由用户说了算**——用户没提就默认不留；用户说「视频也要 / 留着视频」才加 `--keep-video`，
mp4 会留在 `<out>/videos/`。润色版 txt 会多存一份，和原始稿放一起。

---

## 快速开始（首次三件事）

```bash
# 1) 定位 Python（Windows / macOS 二选一）
PY="$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe"   # Windows
PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python"            # macOS

# 2) 环境准备：装依赖 + 下 229MB 模型（只做一次）
"$PY" "$HOME/.workbuddy/skills/sph-to-transcript/scripts/sph2txt.py" --setup

# 3) 微信扫码登录元宝（只做一次）
"$PY" "$HOME/.workbuddy/skills/sph-to-transcript/scripts/sph2txt.py" --login
```

体检：`sph2txt.py --doctor`（八项全绿即可出稿）

> **首次准备由本助手代劳**：直接跑 `--setup` 装依赖 + 下模型，再跑 `--login`，
> 让用户本人拿手机微信扫码确认（这步 AI 代不了）。
> 独立安装包里附带的 `一键准备.bat/.command`、`首次登录.bat/.command`
> 是给不用 AI、只想要双击操作的用户准备的；AI 环境用不到，可忽略。

---

## 出稿（两步：脚本出初稿 → AI 润色）

出稿是**两个动作**，都由本助手完成，用户只说一句话：

**第 1 步 · 跑脚本出干净初稿**

```bash
# 单条（默认：无时间码、自动分段）
"$PY" .../sph2txt.py "https://weixin.qq.com/sph/xxxxx"

# 批量（位置参数、--url 重复、或清单文件，三种都行）
"$PY" .../sph2txt.py "链接1" "链接2" "链接3" --out D:/逐字稿
"$PY" .../sph2txt.py --links-file links.txt --out D:/逐字稿

# 用户明确说「视频也要」时，才加 --keep-video
"$PY" .../sph2txt.py "链接" --keep-video
```

**第 2 步 · AI 二次润色（默认做，别跳过）**

脚本跑完，读它生成的 `.txt` 初稿，按 [`references/润色规则.md`](references/润色规则.md)
修掉同音错字、接回断错的句子、删口水词、补标点，**另存 `原文件名_润色版.txt`**。
然后给用户两份文件 + 一句话说明。只有用户明说「要原始稿 / 别改」时才跳过这步。

> 润色是 AI 读一遍稿子顺手做的，不调任何收费接口、不下载模型，所以对用户仍然是免费的、一步到位的。

| 参数 | 说明 |
|---|---|
| `--out <目录>` | 输出目录（默认 `./逐字稿`） |
| `--timestamps` | 每段前加 `[00:00:00]` 时间码。**默认不加**，只有需要定位原片时才用 |
| `--keep-video` | 用户要视频时才加：保留 mp4 到 `<out>/videos/`（默认转写完删除） |
| `--threads N` | 识别线程数（默认 CPU 核数一半） |
| `--json` | 把结果 JSON 打到 stdout（不落盘，便于程序调用） |
| `--links-file <文件>` | 链接清单，每行一条，`#` 开头为注释 |

**产出结构**

```
<out>/
├── 01_标题.txt              # 脚本出的干净初稿，空行分段
├── 01_标题_润色版.txt       # AI 润色后的终稿（第 2 步生成）
├── 02_标题.txt
├── 02_标题_润色版.txt
└── videos/                  # mp4（仅用户要、加了 --keep-video 时才有）
```

批量时文件名带 `01_ 02_` 序号，单条时不带。**没有 srt、没有 json、没有 wav。**

**每次运行结束会打印汇总**（成功/失败/用时/字数/段数）。脚本不把全文打到 stdout，
需要全文时读 txt 文件；把结果展示给用户时要给出**文件路径 + 字数 + 段落预览**。

---

## 分段是怎么做的

两件事，都不依赖额外模型：

**① 切点挪到换气处.** 识别按 30 秒分块，但每个切点不是硬切——先算一条 20ms 粒度的
RMS 能量包络，在锚点 ±3 秒窗口里找能量最低的那一帧下刀。切点落在静音里，
就不会出现「…的副业被老**公**劝退」被切成「…的副业**背老**／**劝**退我也没有放弃」这种丢字。

**② 按语义合并成段.** 两步：

- **分节**：句子以「第一/第二/第三…杯」「首先/其次/接下来/最后」「另外/总之」这类
  结构词开头，或句前停顿 ≥ 1.0 秒，就开新的一节。
- **节内再平衡**：一节超过 280 字，按句子边界均分，不掐着上限切（那样会甩出一句孤段）。

结果就是「讲了几件事 = 几段」，不是「每 30 秒一段」。

---

## 运维命令

| 命令 | 用途 |
|---|---|
| `--setup` | 装依赖 + 下模型（幂等，可反复跑） |
| `--doctor` | 八项体检：Python / 依赖 / 浏览器 / ffmpeg / 模型 / 登录态 等 |
| `--check` | 只查登录态（JSON 输出，退出码 0 表示有效） |
| `--login` | 有头浏览器扫码登录（**唯一推荐的登录方式**） |
| `--import-model <目录或zip>` | 离线导入模型（网盘分发 229MB 模型用） |

---

## 排错

### 登录态过期（`E_AUTH_EXPIRED` / `E_AUTH_REQUIRED`）
元宝 Cookie 会过期。让用户重扫一次：跑 `--login`（独立安装包里也可双击 `首次登录.bat` / `首次登录.command`）。
**这一步必须用户本人在自己屏幕上操作**（微信确认），agent 代劳不了。

> agent 注意：不要在后台会话里折腾无头登录截图（会拍到空白弹窗、反复失败）。
> 登录这步直接交给用户，让他双击登录器在自己电脑上完成，最省事也最稳。

### 网络抖动（`E_NETWORK`）
内置 3 次指数退避重试。仍然失败时**不需要重新扫码**——不要误判成登录过期。

### 解析类错误
| 错误码 | 含义 | 处理 |
|---|---|---|
| `E_PARSE_EMPTY` | 元宝没返回播放地址 | 链接失效 / 私密付费内容 / 接口改版。换一条链接验证 |
| `E_FEED` | 视频详情拿不到 | 稍后重试；确认视频可公开访问 |
| `E_STREAM_EMPTY` | 没有可下载视频流 | 同 `E_PARSE_EMPTY` |
| `E_INVALID_LINK` | 不是合法链接 | 需要 `https://weixin.qq.com/sph/xxx` 形式 |

### 下载类错误
| 错误码 | 含义 | 处理 |
|---|---|---|
| `E_UNTRUSTED_MEDIA` | 直链域名不在白名单 | 安全拦截，不绕过；联系维护者评估 |
| `E_DOWNLOAD` | 下载中断/不完整 | **重跑同一命令**即断点续传 |

### 转写类错误
| 错误码 | 含义 | 处理 |
|---|---|---|
| `E_NO_AUDIO` | 视频无音轨 | 纯画面视频无法转写 |
| `E_NO_MODEL` | 找不到 SenseVoice 模型 | `--setup` 或 `--import-model` |
| `E_NO_DEPS` | 缺 sherpa-onnx | `--setup` |
| `E_NO_FFMPEG` | 找不到 ffmpeg | `--setup` 会装 imageio-ffmpeg 自带二进制 |
| `E_ASR_EMPTY` | 识别结果为空 | 全程无对白，或音质过低 |

### 转写质量
SenseVoice 是离线同音字模型，会错在专有名词和成语上，例如「5位数」→「无位数」、
「郁郁寡欢」→「郁郁寡关」、「天将降大任于斯人也」→「天降降大任于斯人孽」。
这是模型能力边界，不是流程故障。关键术语建议人工校一遍。

切词丢字的毛病已由「换气点下刀」解决；剩余偶发的断句只出现在**全程无停顿的连读段**
（真的是一个字都没换气），无法再靠能量判断——这类和上面的同音字，都由**第 2 步 AI 二次润色**兜底修掉。

### 性能参考（16 核 / CPU）
| 视频时长 | 转写耗时 | 约合 |
|---|---|---|
| 3 分 44 秒 | 16 秒 | 14× |
| 7 分 44 秒 | 18 秒 | 25× |
| 10 分 53 秒 | 22 秒 | 30× |
| 81 分钟 | 约 200 秒 | 24× |

---

## 边界与合规

- **仅处理用户本人发布、或已获明确授权保存/转写的内容。** 用户未说明时先问一次，
  不能把「给了链接」当成自动授权。
- 不下载私密、付费、已删除内容；不做无授权批量搬运；不规避平台限流。
- 元宝解析依赖腾讯网页内部接口，**不是官方公开 API**，接口变化会导致失效。
  不得对外宣称「永久可用」或「官方支持」。
- 元宝 Cookie 只存本机 `~/.workbuddy/credentials/yuanbao_state.json`；
  视频号直链含 token。**两者都不得写入日志、仓库或对外发送。**
- 识别全程本地，不上传音视频；仅解析环节会把**分享链接**发给腾讯元宝。

---

## 文件构成

```
sph-to-transcript/
├── SKILL.md                    # 本文件
├── requirements.txt            # Python 依赖
├── 一键准备.bat / .command      # 双击装环境
├── 首次登录.bat / .command      # 双击扫码
├── scripts/
│   └── sph2txt.py              # 唯一入口（解析/下载/转写/分段/批量/运维）
└── references/
    ├── 使用说明.md
    ├── 免责与合规.md
    └── 润色规则.md          # 第 2 步 AI 润色时读的规则
```

模型缓存：`~/.cache/sph-transcript/sensevoice/`（可用 `SPH_MODEL_DIR` 覆盖）
自建模型镜像：环境变量 `SPH_MODEL_URL`（形如 `https://your.host/path/{name}`）

元宝解析部分的接口调用逻辑改编自 [Backtthefuture/video-transcript](https://github.com/Backtthefuture/video-transcript)（MIT License）。
