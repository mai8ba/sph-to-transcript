<div align="center">
<img src="assets/icon.png" width="128" alt="麦爸">
<h1>视频号逐字稿 · sph-to-transcript</h1>
</div>

微信视频号链接 → 按语义分段的纯净逐字稿。一个文件搞定，全程免费、零 API Key，音视频不出本机。

## 效果

贴一条 `https://weixin.qq.com/sph/xxx` 链接，产出一个 `.txt`：无时间码、无字幕、无 srt/json/wav 过程文件，自动按语义分段，再由 AI 二次润色修掉同音错字。要不要留 mp4 由用户决定（默认不留）。

## 链路

视频号链接 → 元宝 HTTP 解析拿直链 → 分块下载 mp4 → ffmpeg 抽 16k WAV → 能量包络找换气点切分（避免切断字词）→ 本地 SenseVoice 识别 → 语义合并分段 → 输出 txt → AI 润色。

## 首次准备（只做一次）

```bash
# 1. 装依赖 + 下模型（约 229MB）
python scripts/sph2txt.py --setup

# 2. 微信扫码登录元宝（Cookie 只存本机）
python scripts/sph2txt.py --login
```

不想敲命令的，Windows 双击 `一键准备.bat` / `首次登录.bat`，macOS 双击对应 `.command`。

## 出稿

```bash
python scripts/sph2txt.py "https://weixin.qq.com/sph/xxxxx"
python scripts/sph2txt.py "链接1" "链接2" --out ./逐字稿     # 批量
python scripts/sph2txt.py "链接" --keep-video                # 留 mp4
```

体检：`python scripts/sph2txt.py --doctor`。

## 平台

| 平台 | 地址 |
|---|---|
| 腾讯 SkillHub | https://www.skillhub.cn/skills/sph-to-transcript |
| 生财有术 SkillHub | 审核中（skl_sph-to-transcript） |
| WorkBuddy | 见 dist/sph-to-transcript.zip |

## 合规

仅处理本人发布或已获授权的内容；不下载私密/付费内容；不规避平台风控。元宝解析依赖腾讯网页内部接口（非官方公开 API），不宣称永久可用。Cookie 与视频直链仅存本机，不写入日志或仓库。识别全程本地，不上传音视频。

元宝解析逻辑改编自 [Backtthefuture/video-transcript](https://github.com/Backtthefuture/video-transcript)（MIT License）。

MIT © 麦爸
