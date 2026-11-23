# 网络收音机

国内外电台直播流播放器：电视伴音 / 国内广播 / 国外英文广播，支持收藏、跳台、音量、定时关闭、**收音录制**（一键 / 预约 / 自动分段 / 录音库）与英文台**实时英文字幕**。

## 下载安装

| 平台 | 说明 |
|------|------|
| **Windows** | 下载 `installer/网络收音机-setup.exe`，双击安装（无需 Python）。安装后开始菜单 / 桌面均有「网络收音机」。 |
| **安卓（小米等）** | 下载 `android-app/app/build/outputs/apk/debug/app-debug.apk`，传到手机后允许「安装未知应用」即可安装。 |
| **网页 / PWA** | 打开 `web-radio/index.html`，或部署 `web-radio/` 到任意 HTTPS 静态托管；手机浏览器「添加到主屏幕」即得 App 图标。 |

> 录音默认保存在 Windows：`%USERPROFILE%\Music\网络收音机录音`（可在设置里改路径）。

## 功能

- 电台分类：电视伴音（CCTV 等）、国内广播、国外英文广播、收藏
- 播放控制：播放/暂停、上一台/下一台、音量、键盘快捷键（空格 / ←→ / R / C）
- **服务端录制**（桌面版）：直链透传 + HLS 分片解复用，几乎不受浏览器跨域限制
- 网页/PWA 录制：HLS 分片截获、直链 fetch，失败自动走同源/公共代理
- 预约录制、自动分段（15/30/60 分钟）、录音库管理
- 实时字幕（英文台）：本地 Vosk 离线识别，只显示英文字幕（不翻译）
- 定时关闭、锁屏媒体控制（MediaSession）、录制时屏幕常亮

## 目录结构

```
网络收音机/
├── web-radio/          # 网页版 + PWA 前端（index.html / sw.js / manifest）
│   ├── index.html
│   ├── server.js       # 在线版：静态托管 + 同源 /proxy 流代理
│   └── ...
├── radio-desktop/      # Windows 桌面版（Python + pywebview）
│   ├── app.py          # 本地 HTTP 服务、录制引擎、配置
│   ├── asr.py          # 实时字幕 WebSocket 后端（Vosk）
│   ├── 网络收音机.spec  # PyInstaller 打包脚本
│   └── dist/网络收音机.exe
├── android-app/        # 安卓 WebView 壳（打包 web-radio 资源）
├── installer/          # NSIS 安装包脚本 → 网络收音机-setup.exe
├── build-tools/        # 本地构建工具链（JDK / Android SDK / NSIS / Gradle，不入库）
└── 使用说明.md
```

## 从源码构建

### Windows exe

```bat
cd radio-desktop
python -m venv .venv-build
.venv-build\Scripts\pip install pyinstaller pywebview websockets imageio_ffmpeg vosk
.venv-build\Scripts\pyinstaller --noconfirm 网络收音机.spec
```

将 Vosk 模型放到 `radio-desktop/models/vosk-model-*` 后再打包，可获得离线字幕。

### setup.exe

```bat
cd installer
"%NSIS%\Bin\makensis.exe" setup.nsi
:: 产出 网络收音机-setup.exe
```

### Android APK

```bat
:: 需 JDK 17 + Android SDK（platform 34 / build-tools 34）
cd android-app
gradlew assembleDebug
:: 产出 app/build/outputs/apk/debug/app-debug.apk
```

### 在线版

```bash
cd web-radio && node server.js   # 默认 8080，可用 PORT 改端口
```

## 技术要点

- 桌面端：`ThreadingHTTPServer` 托管前端 + `/proxy` 流代理（绕 CORS/防盗链）+ 录制 REST API
- 前端播放：直连优先 → 同源 `/proxy` → 公共代理逐级回落；HLS 走 hls.js
- 安卓：`WebViewAssetLoader` 以 `https://appassets.androidplatform.net` 提供资源，保证 SW/IndexedDB 安全上下文；前台服务通知降低 MIUI 查杀

## 许可

个人学习与自用项目。电台流地址来自公开源，版权归各电台所有。
