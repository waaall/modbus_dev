# Modbus Dev

独立的 Modbus 通信测试与回放工具，当前正在按阶段实施。

- [用户手册与常见场景](docs/user-manual.md)
- [Modbus RTU 基本概念](docs/modbus-basics.md)
- [需求](docs/requirements.md)
- [架构设计](docs/design.md)
- [TCP 主站需求、设计与联调](docs/tcp-master.md)
- [实施状态与验证计划](docs/implementation.md)

## 当前可用能力

各通信角色支持逐点 `int16`、`uint16`、`int32`、`uint32`、`float32` 及混合排列。
整数支持常量/固定步长三角波，float32 支持常量/正弦/三角波；不做比例、偏移或工程换算。
任务文件仅接受显式的配置版本 4，旧版本提示不兼容，不再迁移；完整规则和混合类型示例见[通用数值类型](docs/numeric-types.md)。

**新增 RTU FC03/04/06/16、主从站只读 / 读写权限，以及 5Hz 实时数值、2Hz 最近十秒趋势曲线。**
保持区保存写入值，输入区独立生成波形；具体操作、兼容规则和两机测试步骤见
[RTU 寄存器读写与实时监视](docs/rtu-register-monitor.md)。TCP 与 RTU 使用相同的独立两区规则，已有便携包需另行重建。

RTU 主站可选读取、逐个 / 批量写入、FC03 写后读回；最多八个独立常量/正弦/三角波测点、连续/严格策略、端口枚举、
绝对周期调度、停止与重连、原始报文及有界异步日志。CLI 和 GUI 共用 SessionRunner。
RTU 从站支持 FC03/04/06/16 和独立保持区 / 输入区；TCP 从站同样支持独立两区与 FC03/04/06/16；默认只读，写入需显式开放权限。
TCP 主站支持同样的逐点 FC03/04/06/16、数据源、写后读回与监视，默认只读；
一个会话连接一个目标 IP/端口/Unit ID，整轮共用交易预算，连接超时独立设置。
本机配套分别运行两个窗口/进程，不在同一会话同时运行主从站；使用方法见[TCP 主站联调](docs/user-manual.md#tcp-主从站本机配套)。
PySide6 单会话 GUI 已实现配置编辑/加载/保存、启动/停止、状态、计数、日志预览和结果目录。
**macOS arm64 已完成 Cython 目录式便携包及本机实包验收；Windows x64 / 麒麟 x86_64
仅有构建适配，待对应电脑验收。DYN/CSV 离线输入继续暂缓。validate 只校验配置，不代表功能已可运行。**

```sh
python -m pip install -e .
modbus-dev validate examples/rtu-master.json
modbus-dev ports
# 先修改示例中的端口和设备参数；run 会实际写寄存器。输出目录必须不存在。
modbus-dev run examples/rtu-master.json --output results/my-first-run
# TCP 只读从站默认监听 127.0.0.1:1502；Ctrl+C 停止。输出目录必须不存在。
modbus-dev run examples/tcp-slave.json --output results/tcp-first-run
# 另一个终端启动 TCP 只读主站，与上面的从站配套；Ctrl+C 停止。
modbus-dev run examples/tcp-master.json --output results/tcp-master-first-run
# RTU 只读从站：先修改示例串口；与主站使用不同端口或不同设备。
modbus-dev run examples/rtu-slave.json --output results/rtu-slave-first-run
python -m unittest discover -s tests -v
```

## 桌面界面

```sh
python -m pip install -e ".[gui]"
python -m modbus_dev gui
# 安装后也可使用桌面入口：modbus-dev-gui
```

GUI 依赖可选，锁定 PySide6 6.7.2；普通 CLI 安装不需要 Qt。
界面采用统一浅色主题、分组配置标签页、关键计数和日志/详细统计工作区，配色和尺寸统一在
`src/modbus_dev/ui/theme.py` 管理。Windows 使用 GDI 字体后端、中文字体回退及完整字体像素对齐；
由 Qt 处理逐显示器 DPI 与分数缩放，窗口按可用桌面调整大小，小屏保留滚动操作。
详见 [GUI 设计与高 DPI 验证](docs/gui-design.md)。源码采用可编辑安装时，关闭并重新启动 GUI 即可加载界面更新。
默认选择本机 TCP 只读从站。可切换 TCP 主站、RTU 主站/从站、加载现有 JSON 示例、校验后启动。
“其他设置 → 界面”支持跟随系统、简体中文和 English；停止通信后切换立即生效，无需重启软件。
全部配置在通信停止收尾完成后开放修改，普通参数由下一轮启动使用。
自动保存兼容版本缺失或不匹配时，通信设置与语言恢复默认并覆盖旧设置；保留结果父目录路径和全部历史结果，不迁移旧草稿。
未运行时修改任意配置会立即自动保存，下次打开软件在配置兼容版本一致时恢复全部设置，保持未连接；无需手动保存才能记住。
配置分为“连接与时序 / 数据设置 / 其他设置”。数据页通过紧凑的 1～8 数字标签切换测点，每次显示一页，
各自启用、命名、设置类型、逐点排列、零基起始地址和适用的数据源；禁用点保留并灰显参数，但不参与通信和地址占用。
每点独立配置类型和排列，不再继承全局编码，数据周期共用；地址采用数字框，不再输入逗号列表。
同一区域内启用点的寄存器范围重叠会即时提示，至少启用一个点才能启动。
RTU 主站按各测点独立选择的操作执行通信，纯读取不先发送写入。任务文件仅支持 schema_version=4，其他版本或无版本文件提示不兼容，不自动改写。
此改动目前仅在源码中生效，已有 `dist/` 便携包未重新构建。
多测点配置示例见 [`examples/tcp-four-points.json`](examples/tcp-four-points.json)，
可直接在 GUI 中加载；其中前三点分别为常量、正弦、另一常量，第四点为禁用的预留配置。
结果父目录下每次创建独立时间戳目录，运行期间禁止修改配置；停止后可以修改并再次启动。
关闭窗口会先请求工作进程收尾；超过 5 秒强制结束并标记证据可能不完整。
工作进程崩溃时显示异常，已有结果目录另存 gui-worker-error.json，不伪造正常摘要。

界面只显示最多 5 Hz 的状态和批量日志预览，最多保留 2000 行；原始报文写入 `events*.jsonl` 分片。
“其他设置 → 日志保留”可配置分片数量，默认最多 100 片（包含当前文件），每片不超过 32 MiB。
超限删除最旧分片；历史淘汰、日志队列丢失和界面预览丢失分别展示，不把滚动窗口宣称为完整会话证据。
界面预览丢失与原始日志丢失分别计数。最终性能统计保存在 summary.json，并显示于结束日志。
此前 Windows GUI 阶段 87 项测试通过，验证原生高 DPI/字体、Qt 离屏渲染、工作进程和本机 TCP 读数；
覆盖 100%～200% 及当时 300% 缩放；该轮属于源码验证，Windows 发布包验收见下文，跨显示器拖动和麒麟桌面仍待验收。
最新 macOS 便携包状态见下节，GUI 虚拟串口联调结果见通信验证记录。

开发基线 Python 3.10+；锁定 PyModbus 3.11.4 和 pySerial 3.5，CLI 不需要 Qt。
此前 Windows / 麒麟源码验证的具体 Python、Qt 版本见对应归档；未在系统 Python 3.7 上运行新包。
示例 COM11 仅为演示值，validate 不打开串口，run 会打开指定串口。
Linux 使用自己的设备路径；源码不限制 GUI 平台，发布包需要分平台构建验证。

软件独立安装、运行和验证。CLI 与 GUI 共用通用业务模块，协议能力优先复用成熟库。

实施顺序：TCP 只读从站 → RTU 只读从站 → GUI → DYN/CSV 离线数据。
TCP 示例显式配置独立保持区初值与输入区数据源；波形使用 FC04 读取。
只允许完整点读取（例如 address=0/count=8）；半点、未映射地址或跨空洞返回异常 02。
数据按 period_ms 推进，与客户端轮询频率无关；duration_s=0 持续运行。
TCP 会话摘要使用 source_updates、slave_request、rx、tx 等计数；rx/tx 是驱动调用记录数，不是报文数。

RTU 从站按权限响应四种寄存器操作；合法广播写入执行但不应答，其他站号静默忽略。CRC 错误、短帧、超过 256 字节的帧整帧丢弃，
等待下一次静默后恢复，不从噪声中扫描嵌入请求。接收间隔依据驱动交付时刻，不能视为物理总线时间。
仅面向单主站请求/应答测试；Windows 虚拟 COM 与麒麟 PTY 已完成实测，详见最新通信验证记录。多点 RS485 与物理设备仍待验收。
持续策略下串口打开失败、读写异常或短写至少间隔 1 秒重连；严格策略遇到这些故障停止。
重连不重置数据时钟；无轮询时数据也继续更新。timeout_ms 限制从站单次写等待（另有 50ms 上限），
不是主站等待回复的超时；接收等待最多 10ms，并受下一数据更新时间约束。

## 目录式便携包

macOS arm64、Windows x64 已生成无需安装 Python 的目录式便携包，产物在 `dist/`。
macOS 完整解压 `.tar.gz` 后打开 `ModbusDev.app`，CLI 为
`ModbusDev.app/Contents/MacOS/modbus-dev`；Windows 完整解压 `.zip` 后打开 `ModbusDev/ModbusDev.exe`，
CLI 为相邻的 `modbus-dev.exe`。两个入口共享包内运行时，GUI 经独立 CLI worker 通信。
GUI 的结果和启动诊断默认写入用户数据目录，不修改应用包。

所有 26 个应用模块经 Cython 编译。源码与编译后各收集 114 项回归，均通过 113 项、跳过
1 项 Windows 专属测试；实际归档解压后验证 TCP、隔离 PTY RTU 主从站、停止与异常恢复，
GUI 覆盖原生 Cocoa、离屏和 LaunchServices 应用包启动。Windows 源码及编译后各通过 112 项、
跳过 2 项 POSIX 专属测试；实际解压包通过双入口、TCP、原生/离屏 GUI 和 100%～200% DPI 验收。
发布版 CLI/worker 另经用户指定的 HHD COM11/COM12 验证 RTU 主从站、写入读回和串口占用恢复。
麒麟发布包尚未实测。macOS 当前仅使用 ad-hoc 签名，未做 Developer ID 签名和公证；Windows 未签名。
本机验收不等于已完成异机外部分发验收。

- [构建步骤、入口与平台验收边界](docs/packaging.md)
- [便携版使用说明](docs/portable-quickstart.md)
- [本轮 macOS 实包验收记录](logs/validation/packaging-macos-20261023.md)
- [本轮 Windows 实包验收记录](logs/validation/packaging-windows-20261023.md)

## 运行结果

调度容差默认是周期的 10%，配置字段 `schedule_tolerance_percent`，允许 0～100 的有限数值。
以下调度容差和 ACK 关联说明用于 RTU 主站；从站按独立数据时钟更新快照。
例如 `"period_ms": 100, "schedule_tolerance_percent": 10` 表示相邻周期首笔实际发送间隔最多为 110ms（含边界）。
超过 110ms 才产生 `schedule_late` WARNING 和首页“调度迟发”计数；首笔没有参照，不判迟发。
比较实际串口写调用开始时刻；同周期的后续分组和读回不重复判定。跳槽或重连后仍比较实际间隔，不按漏过的槽位数放宽上限。
相对计划时刻的偏移只保存在详细 `tx` 日志和汇总分布中，不单独触发迟发告警。
日志及摘要以 `schedule_late_basis=cycle_first_send_interval` 标识此口径；无此标记的旧结果仍按当时的计划偏移规则解释。
`summary.json` 的 `run.timing` 保存平均首发间隔、实际周期发送频率和计数；不足两次首发时，实测间隔和频率为 null。
它不会移动绝对时间基准，也不延长 `timeout_ms`：后者仍是相对计划槽位的整周期交易截止预算。
连续模式偶发通信异常继续，落后整周期跳槽不补发；严格模式仍在交易失败或落后整周期时停止。
本次调度配置不改变 RTU 应答关联不确定的取证规则。

Ctrl+C 请求停止并收尾。退出码：0 按策略完成，1 运行失败，2 配置/依赖错误，130 用户停止。
连续模式允许通信异常，退出码 0 不代表全部交易成功，需查看 counters 和 ack_correlation_uncertain。
输出目录含 config.json、events.jsonl / events-000002.jsonl 等分片、summary.json；命令结束后 stdout 输出结果 JSON。
原始收发、警告、错误及堆栈写入事件文件，通信线程不等待磁盘写入。
队列溢出记录丢失数量和序号范围，并将 evidence_complete 标记为 false。
达到单片 32 MiB 时自动切换新片，不再因此停止任务；超出保留数量时删除最旧分片，
摘要单独记录淘汰的文件数、记录数、生产者序号范围和当前文件索引，并标记 retention_truncated=true、evidence_complete=false。
历史淘汰本身不使会话失败；文件创建、写入、刷新、关闭或淘汰删除失败仍会停止任务，避免静默丢证据或超出磁盘保留上限。
默认日志窗口最多约 3.125 GiB（不含配置和摘要）；限制只针对单个会话，旧会话结果目录不会自动删除。

当前版本配置可通过 JSON 或 GUI 设置保留数量（1～10000）：

```json
{
  "schema_version": 4,
  "mode": "tcp-slave",
  "duration_s": 0,
  "logging": {"max_files": 100}
}
```

读取整份保留日志时应按序号合并所有分片，可复用 `modbus_dev.reporting.segments.event_paths(directory)`。
首个 `events.jsonl` 可能已淘汰，不能只读取该文件。实时状态只传固定大小统计，最终 `summary.json` 的
`segments.files` 保存当前保留分片的名称、字节数、记录数及生产者序号范围；队列丢失仍使用原有 `dropped` 计数。
RTU 超时后无法仅凭相同 ACK 确认属于哪次请求，因此后续有效回复保守标记 ambiguous；重连不会清除此状态。
首页“有效 ACK / 有效读应答”包含普通与关联不确定的有效应答，表示预算内通过校验和请求字段检查的帧数，
不代表每笔应答都能唯一关联。明细仍分别保留 ack、ack_ambiguous、read、read_ambiguous；
汇总 run.reply_summary 提供合计。首次关联风险告警一次，后续有效且关联不确定的交易以 DEBUG 保留完整日志，
不重复刷告警；新的超时、残帧、异常应答仍独立告警。严格模式继续拒绝关联不确定的应答。
TX 曲线保留实际完整提交驱动的值，确认状态单独显示；ACK 超时或关联不确定不会擦除已知的 TX 点，也不刷新其时间。

- [协议库选型与边界](docs/pymodbus-evaluation.md)

## 验证记录

需求、设计、使用说明、验证方法与验收标准保留在 `docs/`；每轮验证报告和精简 JSON 摘要归档到
[`logs/validation/`](logs/validation/README.md)，纳入版本管理。原始日志、截图及完整通信证据仍输出到
`results/`，由 Git 忽略。报告中的复现命令和 `results/...` 路径均以项目根目录为基准。

- [最新 Windows / 麒麟实际通信验证](logs/validation/validation-20261023.md)
- [macOS Apple Silicon 实际通信与原生 GUI 验证](logs/validation/validation-macos-20261023.md)
- [macOS arm64 Cython 便携包实包验收](logs/validation/packaging-macos-20261023.md)
- [Windows x64 Cython 便携包实包验收](logs/validation/packaging-windows-20261023.md)
- [M2 历史验证记录](logs/validation/validation-m2.md)

逐点操作、独立区域、50ms 周期下限及红黄预算规则见 [独立测点与周期预算](docs/point-configuration.md)。新版示例：[八点主站](examples/rtu-master-independent-points.json)、[独立两区 TCP 从站](examples/tcp-independent-points.json)。
