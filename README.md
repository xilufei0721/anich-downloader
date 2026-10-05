# AniCh Downloader

从 [AniCh](https://github.com/Sle2p/AniCh)（免费动漫应用）提取番剧资源并下载的 Windows 工具：支持搜索番剧、查看剧集、选择线路、批量下载，附带现代深色图形界面。

> 仅供个人学习与技术研究使用。请勿用于商业用途或侵犯版权，下载后请于 24 小时内删除。

## 功能

- 番剧搜索、剧集列表（AniCh API，内置多节点自动轮换）
- 视频线路解析（自动解码新版 API 的 protobuf 字节数组响应与混淆 base64）
- 批量下载：多选集 + 线路失效自动切换下一条
- 支持 mp4 直链下载；m3u8 线路自动调用 yt-dlp
- 图形界面（tkinter，纯标准库，无需第三方依赖）
- 一键打包为独立 exe，可分发到未安装 Python 的电脑

## 安装与使用

### 方式一：图形界面（推荐）

需要 Python 3.9+（自带 tkinter 即可，无需安装第三方库）：

```powershell
pythonw anich_gui.pyw
```

操作流程：

1. 输入番剧名 → 点【搜索】→ 双击结果加载剧集
2. 右侧勾选要下载的集数（可 Ctrl/Shift 多选，或点【全选】）
3. 选中一集 → 点【刷新线路】→ 选择线路（mp4 最省事）
4. 点【浏览...】选择保存位置 → 点【开始下载】

### 方式二：命令行

```powershell
python anich_dl.py search "葬送的芙莉莲"   # 搜索番剧,得到 ID
python anich_dl.py episodes 32339         # 列出剧集
python anich_dl.py play 32339 1           # 查看某集全部线路
python anich_dl.py download 32339 1 --line 1   # 下载(第 1 条线路)
```

### 打包为 exe

双击 `build.bat`（自动安装 PyInstaller 并打包），产物在 `dist\AniChDownloader.exe`。
也可以运行 `zip_release.ps1` 直接生成带使用说明的便携版 zip。

## 技术要点

- 新版 AniCh API 节点 `anich.sends.eu.org` 返回 JSON 字节数组，内容为 protobuf 消息
- 视频 URL 使用插入 `A0` 字符的混淆 base64（将 `A0` 还原为 `0` 后按标准 base64 解码）
- 纯标准库实现：urllib + 手写 protobuf wire 解码器，无运行时依赖

## 致谢

- [Sle2p/AniCh](https://github.com/Sle2p/AniCh) - 原应用与 API 设计
- [MakotoArai-CN/A2P](https://github.com/MakotoArai-CN/A2P) - 新版 API 与混淆编码的逆向参考

## 免责声明

本项目仅为技术学习目的，接口可能随时变更。请尊重版权，勿将下载内容用于商业用途或传播。
