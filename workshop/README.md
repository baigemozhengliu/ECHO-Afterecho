# Afterecho 0.2.0

ECHO 虚拟音乐库插件。导入歌单，搜索音源，在 ECHO 播放。

## 安装

1. 安装 [ECHO](https://github.com/Moekotori/ECHO)，已验证版本：Steam 26.10.4。
2. 安装 [Python 3.12，Windows 64 位](https://www.python.org/downloads/release/python-31210/)。勾选 **Add python.exe to PATH**，重启 ECHO。
3. 下载 [Afterecho-0.2.0.zip](https://github.com/baigemozhengliu/ECHO-Afterecho/raw/refs/heads/main/downloads/Afterecho-0.2.0.zip)。ECHO → 创意工坊 → 导入本地包。不要导入 GitHub 的 Source code ZIP。
4. 启用插件，允许其运行本机服务。首次启动联网安装依赖，等待“服务就绪”。
5. 插件 → 连接 ECHO。按参数新建 **Subsonic** 连接，执行同步。

初始为一个空白歌单。包内没有音乐、用户歌单、账号或缓存。

## 使用

- 导入 TXT / JSON，或搜索添加。
- 仅启用的歌单挂载到 ECHO。
- 增删歌曲、切换歌单后，在 ECHO 执行完整同步。
- 备份：歌单 → 导出。
- TXT：UTF-8，每行一首，Tab 分列。

```text
歌名	歌手	专辑
歌曲 A	歌手甲 | 歌手乙	专辑 A
```

## 存储

`%LOCALAPPDATA%\Afterecho\`

| 路径 | 内容 |
|---|---|
| `data\playlists.sqlite` | 歌单 |
| `data\source-index.sqlite` | 匹配记录 |
| `data\audio-cache` | 音频缓存 |
| `data\subsonic-auth.json` | 本机连接凭据 |
| `runtime` | 独立 Python 环境和 Bridge |
| `setup.log` / `bridge.log` | 安装 / 运行日志 |

服务仅监听 `127.0.0.1:18765`。删除插件不删除数据。0.1.x 开发版数据不自动迁移，请导出后导入。

## 依赖

Python 和 ECHO 自行安装。下列依赖由首次启动从 PyPI 下载到插件独立环境，不修改系统 Python 包。

| 项目 | 固定版本 | 项目 / 下载 | 许可 |
|---|---|---|---|
| MusicDL | 2.14.0 | [GitHub](https://github.com/CharlesPikachu/musicdl) · [PyPI](https://pypi.org/project/musicdl/2.14.0/) | PolyForm Noncommercial；另见下方条款差异 |
| FastAPI | 0.142.2 | [GitHub](https://github.com/fastapi/fastapi) · [PyPI](https://pypi.org/project/fastapi/) | MIT |
| Uvicorn | 0.54.0 | [GitHub](https://github.com/Kludex/uvicorn) · [PyPI](https://pypi.org/project/uvicorn/) | BSD-3-Clause |
| OpenCC Python | 0.1.7 | [GitHub](https://github.com/yichen0831/opencc-python) · [PyPI](https://pypi.org/project/opencc-python-reimplemented/) | Apache-2.0 |

安装失败：插件 → 说明 → 重试启动。详情见 `setup.log`。

开发者手动配置：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## 许可

本项目自有代码采用 PolyForm Noncommercial 1.0.0。用于个人非商业研究、试验。

FastAPI、Uvicorn、OpenCC 允许按各自条款修改和分发，需保留相关许可、版权等通知。

**MusicDL 条款存在差异**：LICENSE 允许符合条件的非商业修改、分发；README 另要求再分发、捆绑及衍生打包先取得明确许可。不能据此无条件断言“非盈利即可捆绑”。本 ZIP 不包含 MusicDL 或其第三方依赖代码，仅由用户本机向上游安装依赖。若计划将其代码或二进制合包发布，先向作者取得书面许可。

详见 [许可核查](LICENSE-REVIEW.md)、[使用声明](DISCLAIMER.md)。

## 已知限制

- 仅验证 Windows x64、Python 3.12、ECHO Steam 26.10.4。
- ECHO 远程连接需手动建立，修改歌单后需同步。
- ECHO 26.10.4 个别目录可能显示内部 ID；专辑页播放、输入法仍有兼容问题。
- 音源可用性随上游变化。

## 构建

```powershell
py -3.12 scripts/build.py
```

产物：`dist/Afterecho-0.2.0.zip`。将此文件上传 GitHub Release。源码提交仓库，运行目录和个人数据不提交。
