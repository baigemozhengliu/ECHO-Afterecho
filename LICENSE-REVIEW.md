# 许可核查

核查日期：2026-10-06。结论以所列版本和上游文件为限。

| 项目 | 许可 | 修改 / 非商业使用 | 随包分发 |
|---|---|---|---|
| MusicDL 2.14.0 | PolyForm Noncommercial 1.0.0 | LICENSE 明示允许非商业目的下修改和使用 | README 另要求明确许可；本包不携带其代码 |
| FastAPI 0.142.2 | MIT | 允许 | 保留版权和许可 |
| Uvicorn 0.54.0 | BSD-3-Clause | 允许 | 保留版权、条款、免责；不得暗示背书 |
| OpenCC Python 0.1.7 | Apache-2.0 | 允许 | 保留许可和适用通知，标注修改 |

MusicDL 的 PolyForm Noncommercial 含用途限制，不应称为无条件自由使用或 OSI 意义的开源许可。非盈利身份与非商业用途不是同一个判断。本项目声明不能改变上游许可。

MusicDL [LICENSE](https://github.com/CharlesPikachu/musicdl/blob/master/LICENSE) 与 [README 免责声明](https://github.com/CharlesPikachu/musicdl#%EF%B8%8F-disclaimer) 的分发要求不一致。计划合包其源代码或二进制前，应向作者取得明确许可。本次只发布独立插件及 Bridge 代码，依赖在用户机器上由 pip 向 PyPI 获取；未将 MusicDL 或传递依赖复制进安装 ZIP。

上游安装的传递依赖含不同许可证，见 DEPENDENCIES.md。不能用本项目的许可覆盖它们。后续如分发依赖副本，须按具体制品重新核查，包含 GPL/LGPL 组件的源码提供义务等。

## 原文

- [MusicDL](https://github.com/CharlesPikachu/musicdl/blob/master/LICENSE)
- [FastAPI](https://github.com/fastapi/fastapi/blob/master/LICENSE)
- [Uvicorn](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md)
- [OpenCC Python](https://github.com/yichen0831/opencc-python/blob/master/LICENSE.txt)
- [PolyForm Noncommercial](https://polyformproject.org/licenses/noncommercial/1.0.0/)

许可快照在 licenses/。音乐、歌词、封面的权利不包含在这些软件许可中。发行前仍须遵守适用法律和平台条款。
