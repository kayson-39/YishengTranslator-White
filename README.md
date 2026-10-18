# 译声实时翻译 0.10.2 · 白色新版

免费的 Windows 实时字幕与翻译工具，由 Codex 协助开发与维护。支持系统声音和麦克风、日语识别、本地或 API 翻译、悬浮字幕、历史回看和自定义背景。

## 下载

到 [Releases](https://github.com/kayson-39/YishengTranslator-White/releases/latest) 下载 Windows x64 安装器 `YishengTranslator-White-0.10.2-Setup.exe`，校验值见同一页的 `SHA256SUMS.txt`。本仓库同时提供完整源码包与源码目录。

## 这一版改了什么

- 白色主界面与分类设置；保留图片导入、拖入背景、模糊、亮度和透明度设置。
- 悬浮窗滚轮回看本次会话全部字幕，回看时不被新字幕打断，点击“回到最新”继续跟随。
- 工具按钮放在浮窗内部；A−/A＋调字号，锁定与鼠标穿透分开控制。
- 修复低透明度下日文字形异常、描边覆盖文字以及浮窗重绘残留问题。
- 整句翻译优先，过时草稿主动取消；失败重试后继续下一句，主窗口显示队列并提供“补译”。
- 优化 GPU 引擎启动，显示实际使用的引擎和回退原因。
- 透明度调节不再逐格重排全部历史，减少译文更新时的重复刷新。

## 安装与使用

Windows 10 1809 或更新版本 / Windows 11，x64。运行安装器后启动软件，选择实际播放声音的输出设备，点击“开始聆听”。

本安装器默认安装到独立的 YishengTranslatorWhite 目录，并使用“译声实时翻译（白色新版）”快捷方式，便于与旧版对比。不会导入旧版私人配置，请在设置中选择自己的模型/API。

安装器包含 Python 环境、Parakeet 日语识别模型（0.6B）、静音检测、轻量翻译和 CPU/Vulkan/CUDA 推理引擎。默认自然口语翻译使用混元 Hy-MT2 7B Q4_K_M；首次安装需要下载约 4.62 GB 模型，已有完整模型会跳过，失败可在设置中继续下载或导入自己的 GGUF。下载过程支持断点续传和校验。

字幕保留量可输入正整数或勾选“保留全部”，指本次会话。主窗口“全部记录”可搜索，SRT/TXT 导出包含会话记录。关闭或清空前请先导出需要保留的字幕。

浮窗顶部拖动移动，边缘调整大小，滚轮回看。开启鼠标穿透后，在主窗口设置中关闭穿透即可恢复操作。透明度仅调整底色，文字保持清晰。

API、Ollama、自定义 GGUF、术语表及字体设置均保留。翻译性能取决于所选模型和硬件；本地模型无需 API 费用，云端服务是否收费由服务商决定。

## 从源码运行

源码包不含模型权重、运行环境、个人配置、API 密钥、日志或自定义背景。建议 Python 3.12：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts/download_models.py
.venv\Scripts\python.exe -m jpsub.main
```

在设置中下载推理引擎及翻译模型，或配置云端 API。原创程序代码为 MIT，第三方模型、依赖与素材保留各自许可，详见 THIRD_PARTY_NOTICES.md。
