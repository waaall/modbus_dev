# 麒麟本机打包

在麒麟电脑或麒麟虚拟机中直接 clone 本仓库，使用统一入口 `build_release.py` 原生构建。
后续通过 `git pull` 更新源码，再运行同一入口。

## 环境准备

- 构建基线：Kylin Linux Advanced Server V10 SP3 2403，x86_64，系统 glibc 2.28。
  其他麒麟版本或架构需要单独确定基线并验收。
- 独立 Python 3.11/3.12，推荐 3.12，包含 `venv`、pip 和匹配的 `Python.h` 开发头文件。
  使用独立安装或 Miniforge 环境中的解释器，不替换系统 Python。
- Git、GCC 和 C/C++ 编译工具，以及本机桌面所需的字体、OpenGL/EGL、X11/xcb、
  xkbcommon、fontconfig 和 DBus 等系统库。
- 在已登录的麒麟桌面终端运行打包，以便原有流程验收实际 GUI。

发布构建的 Python 依赖统一由 `requirements/release.txt` 锁定，包含 PySide6 6.9.3 自带的 Qt 库。
6.9.3 的 Linux x86_64 wheel 支持 glibc 2.28，见 [PyPI 发布文件](https://pypi.org/project/PySide6/6.9.3/#files)。
源码 GUI 安装 `pip install -e ".[gui]"` 接受 PySide6 `>=6.9.3,<7`，可以使用环境中已安装的兼容新版；
构建便携包时使用以下独立 venv 和精确锁定依赖。
构建环境应使用本项目自己的 venv。运行打包前清除其他项目设置的 `PYTHONPATH`、
`QT_PLUGIN_PATH`、`QML*` 等开发环境变量，Qt 平台选择使用当前桌面的默认值。

下文的 `python3.12` 可替换为独立 Python 的完整路径。若系统缺少工具或桌面库，
通过麒麟软件源安装相应组件；缺少动态库时可用 `yum provides '*/缺失库名'` 查询提供者。

## 获取源码并打包

把 `<仓库地址>` 替换为本项目的 Git 仓库地址，在麒麟桌面终端运行：

```bash
git clone <仓库地址> modbus_dev
cd modbus_dev
python3.12 -m venv .venv-build-kylin-x86_64
.venv-build-kylin-x86_64/bin/python -m pip install -r requirements/release.txt
.venv-build-kylin-x86_64/bin/python build_release.py --target kylin-x86_64 --test-timeout 900
```

原有发布流程会自动完成源码回归、Cython 编译、编译后回归、双入口 PyInstaller 打包、
归档解压以及 CLI、worker、TCP、隔离 PTY 和原生/离屏 GUI 验收，全部通过后才将归档放入 `dist/`。
`--test-timeout 900` 设置每轮回归的上限为 900 秒，可省略并使用默认的 600 秒。

若使用 Miniforge Python，编译时提示缺少 Conda 编译器包装器，可显式选择已安装的系统 GCC：

```bash
CC=gcc .venv-build-kylin-x86_64/bin/python build_release.py --target kylin-x86_64 --test-timeout 900
```

建议发布时使用干净的 Git 提交。构建信息会记录提交号、未提交状态和实际源码摘要。

## 更新后重新打包

在已有仓库目录中运行，继续使用本项目的构建 venv：

```bash
git pull --ff-only
.venv-build-kylin-x86_64/bin/python -m pip install -r requirements/release.txt
.venv-build-kylin-x86_64/bin/python build_release.py --target kylin-x86_64 --test-timeout 900
```

构建器不会覆盖已有同名归档；源码和构建输入未变时，可直接使用之前已验收的产物。

## 产物与验证

- `dist/ModbusDev-<版本>-kylin-x86_64-<源码摘要>.tar.gz`：交付包，相邻保存 `.sha256` 和 `.validation.json`。
- `build/releases/`：编译中间文件和源码/编译后回归日志。
- `results/release-kylin-x86_64-*/`：实际解压、GUI 截图、通信日志和详细验收回执；失败证据保留在对应目录。

完整解压交付包后，在 `ModbusDev/` 内运行 `./ModbusDev` 启动 GUI，或运行 `./modbus-dev --help` 使用 CLI。
保留相邻的 `_internal/`，运行该便携包无需另装 Python。

目标机仍需匹配的系统和桌面运行库。真实串口/RS485、真实 TCP 设备及长期运行需在目标设备上另行验收。
完整发布机制见[目录式便携包构建与验收](packaging.md)，启动和配置见[便携版使用说明](portable-quickstart.md)。
