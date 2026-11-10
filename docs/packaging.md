# 目录式便携包构建与验收

## 目标与边界

同一套发布流程，分别在目标系统的 64 位原生 Python 中构建：

| 目标 | 系统 / CPU | 当前验收安排 |
|---|---|---|
| `macos-arm64` | macOS / Apple Silicon | 本机实际构建及解压验收 |
| `windows-x64` | Windows / x64 | 已在 Windows 11 原生构建及解压验收 |
| `kylin-x86_64` | 麒麟 / x86_64 | 麒麟本机原生构建，目标设备单独验收 |

不能在 Mac 上生成 Windows 或麒麟可运行包，不把跨平台 spec 单元测试当实包通过。
macOS 的最低版本、Intel Mac、麒麟其他版本或架构均不由本机验收推导。麒麟应在计划支持的
最老系统基线上构建，并验证 glibc、Qt 平台插件、字体和桌面环境；不修改系统 Python。

## 双入口与管道

- `ModbusDev`：无控制台 GUI，引导至编译后的 `modbus_dev.desktop`。
- `modbus-dev`：有标准流的 CLI，提供 `validate`、`ports`、`run`、`worker`、`gui` 和 `self-check`。
- 两个入口共享目录内的 Python、Qt、第三方依赖和 Cython 模块，不随包携带开发虚拟环境。
- GUI 用 `QProcess` 启动相邻的 CLI `worker`，而不是启动自己或搜索用户 PATH 中的 Python。
  Windows 使用 console 类型 CLI，引导器不丢弃标准流；QProcess 默认不为子进程创建新控制台。
- stdin 只接收 schema_version=1 停止指令，stdout 只输出 JSONL 状态/结果，stderr 用于诊断。
  保留状态限频、预览丢失计数、五秒停止期限和异常证据标记；界面崩溃不等于正常收尾。

macOS 的两个入口均放在 `ModbusDev.app/Contents/MacOS/`，主应用入口显式指向 GUI。
其他平台的两个入口位于便携目录根部，共用 `_internal/`。

## 源码保护

发布前自动检查并编译 `src/modbus_dev/ui/translations/modbus_dev_zh_CN.ts`。
编译后的 `.qm` 复制到本机模块目录并纳入双入口便携包，运行时从包内定位，不依赖当前工作目录。
普通 Python 安装也通过 package-data 携带翻译资源。中文/英文切换及资源加载须在对应产物中验证。
手动维护命令为 `python tools/translations.py --check` 和 `python tools/translations.py --compile`。

编译 `src/modbus_dev/` 除 `__init__.py`、`__main__.py` 外的全部模块，包括 GUI、worker、
运行入口和内置诊断。初始化文件仅允许包说明和接线；发布版不使用源码目录的 `__main__.py`。
启用 Cython `binding=True`、`annotation_typing=False`，保留边界检查、负索引和 Python 除法，
不能为打包而改变通信或浮点行为。

先在副本编译，再从仅含扩展和初始化文件的 native 目录打包。所有受保护模块显式列为
hidden import，并额外收集按平台动态导入的串口后端。审计同时覆盖：

1. PyInstaller Analysis 的 pure 模块列表，禁止业务字节码回退。
2. 两个最终可执行程序的内嵌 PYZ，禁止业务模块混入字节码。
3. 实际应用目录中的扩展完整性、业务源码、历史数据及私有配置。
4. 实际程序 `self-check` 的模块加载位置，禁止从源码或全局环境借用模块。

Cython 提高逆向门槛，不是不可逆加密，也不会隐藏所有字符串。不得分发 `build/` 中的
中间源码和生成的 C 文件，只分发通过验收后进入 `dist/` 的归档与附属记录。

## 构建环境

构建工具需要 Python 3.11+，推荐各平台使用 Python 3.12；应用源码基线仍为 Python 3.10+。
依赖按 `requirements/release.txt` 精确锁定，构建时逐项检查版本。虚拟环境按系统和架构分开，
不得指向全局环境、其他项目的环境或另一个 CPU 架构的 Python。

macOS 需 Xcode Command Line Tools / Clang；Windows 需 Visual Studio C++ Build Tools 和
Windows SDK，建议使用开发者终端；麒麟需匹配 Python 的开发头文件、C 编译器及 Qt 桌面所需系统库。

源码与编译后的两轮完整回归各自默认限时 600 秒。较慢机器可在构建命令中添加
`--test-timeout 900` 调整每轮上限；只接受正整数秒，超时仍使构建失败，不跳过测试或实包验收。
构建输出记录各轮实际耗时，`BUILD_INFO.json` 记录所使用的测试期限。

### macOS arm64

```sh
python3.12 -m venv .venv-build-macos-arm64
.venv-build-macos-arm64/bin/python -m pip install -r requirements/release.txt
.venv-build-macos-arm64/bin/python build_release.py --target macos-arm64
```

### Windows x64（已原生实测）

```powershell
py -3.12 -m venv .venv-build-windows-x64
.venv-build-windows-x64\Scripts\python.exe -m pip install -r requirements/release.txt
.venv-build-windows-x64\Scripts\python.exe build_release.py --target windows-x64
```

构建器会执行实际 GUI 原生 DPI 检查，但 Windows 虚拟/物理 COM 互操作仍需补充，
验收回执对此明确保留待验收状态。现场串口必须明确指定，不自动选择已有设备。

Windows 11 / Python 3.12.10 已完成锁定依赖构建、中文/空格路径解压和实际双入口验收，
实际 GUI 另测 100%、125%、150%、175%、200% 缩放。结果及 COM 补测见
[Windows 实包验收记录](../logs/validation/packaging-windows-20261023.md)。

需要验收已解压包的通信时，可显式指定其 CLI；被测进程会清除开发 Python/Qt 环境和 PATH。
下列 COM11/COM12 仅用于本轮用户指定的 HHD 虚拟串口对，其他电脑必须明确替换为自己的虚拟测试端口：

```powershell
$env:PYTHONPATH = "$PWD/src"
$cli = '已解压目录/ModbusDev/modbus-dev.exe'
.venv-build-windows-x64\Scripts\python.exe tools/verify_backend_communications.py --cli $cli --master COM11 --slave COM12 --output results/packaged-com-new --commit YOUR_COMMIT --seconds 20
.venv-build-windows-x64\Scripts\python.exe tools/verify_backend_communications.py --cli $cli --master COM11 --slave COM12 --output results/packaged-contention-new --commit YOUR_COMMIT --contention-only
```

`--cli` 不与 `--gui-only` 混用；后者通过源码 GUI 驱动工作进程，不能作为发布版 GUI 验收。

### 麒麟 x86_64

在麒麟电脑或虚拟机中直接 clone 本仓库，后续通过 `git pull` 更新。
准备独立 Python 和编译环境后，在已登录的麒麟桌面终端运行统一发布入口；
详细环境准备见[麒麟本机打包](kylin-packaging.md)。

```sh
python3.12 -m venv .venv-build-kylin-x86_64
.venv-build-kylin-x86_64/bin/python -m pip install -r requirements/release.txt
.venv-build-kylin-x86_64/bin/python build_release.py --target kylin-x86_64
```

打包流程的隔离 PTY 验收不替代真实设备与物理串口验收。

## 发布门禁及证据

构建器默认执行全部步骤，不提供绕过实包验收后直接发布的开关：

1. 源码全量回归；Cython 编译后再次运行同一套回归。
2. 双入口 PyInstaller 构建与源码保护审计。
3. 白名单复制公开示例及使用说明，生成构建信息和内容校验清单。
4. Windows 归档为 ZIP；macOS/麒麟归档为 tar.gz，保留 Qt framework 的符号链接和执行权限。
5. 解压到含中文和空格的独立路径，在清除 Python、Qt 开发环境变量和开发 PATH 后运行实际程序。
6. 验证版本、串口枚举、配置模板、固定报文用例格式、全部本机模块、CLI/worker TCP 读取与拒写、正常停止、
   同端口重启、端口占用失败及恢复、父管道断开、配置错误终态。
7. POSIX 使用隔离 PTY 验证实际 RTU 主从站及写入读回；Windows 不以此替代 COM 验证。
8. 实际 GUI 分别以原生和离屏平台启动，经包内 CLI 验证读数、停止、重启、崩溃恢复、
   端口占用恢复和关闭收尾，保存实际渲染截图。
   macOS 额外经 LaunchServices 启动整个 `.app`，并检查 ad-hoc 签名完整性。
9. 验证运行没有修改安装目录，全部成功后才将归档放入 `dist/`。

`BUILD_INFO.json` 记录提交号、工作区是否有未提交修改、源码摘要、目标平台及实际依赖。
未提交的构建不能冒充提交号对应的干净版本。归档名称包含源码摘要，同名归档不会覆盖。

- `build/releases/<目标>-<随机标识>/`：源码/编译后测试日志、编译中间文件、打包日志上下文。
- `results/release-<目标>-<时间>/`：实际解压目录、通信日志、截图与详细验收回执。
- `dist/<应用>-<版本>-<目标>-<源码摘要>.tar.gz` 或 `.zip`：真正交付物。
- 归档旁的 `.sha256`、`.validation.json`：归档校验和与实际执行过的验收结果。

验收失败时保留第一次失败及其后复测证据，不删除首次失败来伪装一次通过。
实际结果可精简归档到 `logs/validation/`，但不提交大体积应用、完整报文或中间源码。

## 分发仍需单独完成的事项

当前 macOS 使用 ad-hoc 签名，未使用 Developer ID，也未公证。正式外部分发前应完成
签名、公证及带隔离属性的异机首次启动验收；不提供删除安全属性来掩盖问题的脚本。
Windows 代码签名、安装器、自动更新、其他 CPU 架构均不在本轮交付范围。
