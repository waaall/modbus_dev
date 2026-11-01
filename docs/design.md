# Modbus Dev 架构设计

日期：2026-10-22。状态：设计基线；库版本和硬件性能须以验证记录为准。

M2 实施补充：当前 Python 3.10+，锁定 PyModbus 3.11.4 / pySerial 3.5；
RTU 采用库编码加受限 deadline 适配器，见 [选型记录](pymodbus-evaluation.md)。
单会话 GUI 和 RTU/TCP 只读从站已接入；轮转日志仍是设计目标，当前日志为单文件 32 MiB 严格证据模式。
串口读写采用有界等待，但操作系统打开串口及线程调度不提供硬实时保证。

## 1. 架构与依赖方向

TCP 主站与 RTU 主站共用业务流程，传输引擎分别实现；新增连接超时、固定整轮截止时间、
写入结果未知及复用边界见 [TCP 主站需求与设计](tcp-master.md)。不增加多会话或通用插件框架。

```text
CLI ─────────────────┐
                     ├─ application：单会话、运行策略、停止、事件、结果
GUI → worker process ┘       ├─ sources：常量/正弦/三角波/DYN
                             ├─ mapping：点表和数值编码
                             ├─ engine：交易接口与库适配器
                             └─ reporting：异步记录与摘要
engine → PyModbus / pySerial → OS
```

核心仅依赖通用领域模块与声明的第三方库，不导入 GUI 或 Qt。平台判断只放在适配器/能力检测中。
不实现动态插件发现、HTTP 服务、多会话管理或通用消息总线。
核心可直接被 Python 调用，CLI 与 worker 采用同一个用例入口；入口只负责参数转换与展示，不复制业务逻辑。

## 2. 模块

| 模块 | 职责 | 禁止承担的职责 |
|---|---|---|
| domain | 不可变任务配置、点表、样本、交易结果、事件、错误代码 | UI、文件路径推断、系统 I/O |
| application | 会话状态机、策略、调度、停止与资源收尾 | CRC、Qt 控件、设备预设 |
| sources | 读取/生成值、原始时间和质量；准备阶段验证 DYN | 串口交易、隐式时间平移 |
| numeric / mapping | 公共类型宽度与编解码、地址覆盖检查、相邻完整测点分组 | 工程换算、决定失败后继续或停止 |
| engines | 标准交易与从站服务；库类型转换成领域结果 | GUI 刷新、业务验收结论 |
| reporting | 有界日志写入、原始记录、摘要、CSV | 阻塞通信循环等待磁盘 |
| cli / gui | 参数/表单转换、状态展示、用户控制 | 复制业务规则或协议实现 |

## 3. 协议库复用决策

优先评估 PyModbus 的主站和从站能力，使用 pySerial 作为串口驱动，不重写完整 Modbus 栈。
库的具体版本精确锁定到验证版本；其 API 不按传统语义版本保证，升级必须跑协议和时序回归。
库异常、PDU 和 datastore 类型仅允许出现在 engines 内，不泄漏到应用层接口。

进入生产路径前必须验证：
1. 明确关闭隐式重试（例如 retries=0），应用层拥有唯一的重试/重连策略。
2. 超时是否覆盖完整交易、外部 deadline 是否真正能终止等待，取消后如何清理缓冲和关联状态。
3. trace_packet 的时间含义、分段行为、迟到 ACK 匹配、异常码和错误 CRC 可见性。
4. Windows 是否引入 flush 轮询；9600/8N1 下单点/四点 100 ms 交易是否满足预算。
5. 从站是否能拒绝所有非允许功能码，以及坏质量点能否正确返回异常，而不是旧值。

时序和失败语义通过独立对端、固定报文及确定性时钟测试验证。
若库确实不能满足特定诊断行为，只允许保留一个范围受限的专用 RTU 后端，并记录测试证据与原因；
不得仅为省事同时维护两个通用协议栈。在线帧处理和离线取证重组共享编码原语，不共享容错政策。

参考：https://pymodbus.readthedocs.io/en/stable/source/client.html
参考：https://github.com/pymodbus-dev/pymodbus

## 4. 数据源与复用边界

当前八点实现使用 `PointConfig` 分别保存启用、名称、地址、数值类型、排列与 `SourceConfig`，
`TaskConfig` 保留设备级连接、站号和共用周期；区域、操作、读回及有效排列在每点保存。
禁用点不进入采样、映射、从站可读范围或线路预算。
数据源按同一个绝对槽位分别计算启用点；地址与值绑定后排序，不以排序后的地址重新索引原始数值。
任务文件只接受显式 schema_version=4；其他版本或缺少版本号直接拒绝，不展开旧共享源或全局操作。缺少类型时仍为 float32。
数值类型、宽度和排列仅在 domain.numeric 定义，配置、映射和实时解码复用该契约；
不新增比例、偏移或工程换算。格式、继承与原始诊断边界见[通用数值类型](numeric-types.md)。

数据源统一输出值、时间、质量和通道标识，通信引擎不感知文件格式。
DYN 解码应使用独立样例验证值、时间、质量、CRC 错误、缺口和边界；明确支持的格式与版本。
Modbus 回放要求时间连续，不默认拼接缺口或丢弃质量信息。
滤波、降采样与解码分离，采用可独立测试的纯数据处理接口，不内置现场点名或路径。
成熟第三方库优先复用；新增依赖记录版本、许可与验证结果，通用能力只维护一套实现。

## 5. 会话与调度

状态：idle → starting → running ↔ reconnecting → stopping → stopped；不可恢复错误进入 failed。
单一 SessionRunner 拥有通信资源；同一进程禁止二次启动。运行配置不可变。
停止令牌贯穿准备、等待、读写和重连；所有阻塞调用有上限。停止后先释放通信，再收尾日志和摘要。

持续发数：perf_counter 绝对槽位；落后整周期跳至未来槽位；不追赶；异常后限频重连。
调度容差 schedule_tolerance_percent 默认 10%，可配置为 0～100；相邻周期首笔实际写调用间隔与
`period_ms + period_ms * schedule_tolerance_percent / 100` 比较。等于上限正常，超过才产生
schedule_late WARNING 及计数；首笔没有参照，不判迟发。告警本身不停止任务，严格模式也不因该告警退出。
读回和后续分组不作为新的周期首发；跳槽、串口不可用或重连后仍比较两次实际首发的间隔，不按槽位差放宽上限。
统计的“发送”是 write 调用开始，包含短写或失败尝试，不代表 UART 第一位发出或写入得到确认。
容差不延长交易 deadline，也不移动原有绝对时间基准；发送间隔偏短只反映在间隔分布中，不计为调度迟发。

原始 tx 事件增加 scheduled_s、schedule_offset_ms、cycle_first_send；首发另外保存
cycle_interval_ms、previous_cycle_sent_s、previous_cycle_slot。首笔的间隔与前次引用为 null。
相对计划偏移只作为 DEBUG 详细日志和 send_lateness_ms / cycle_lateness_ms 分布，不单独产生告警。
send_interval_ms 继续统计所有请求间隔，cycle_interval_ms 仅统计相邻周期首发间隔。
会话启动、schedule_late 告警及 run.timing 汇总标明 cycle_first_send_interval 口径，避免与旧日志的计划偏移告警混淆。
告警详情中的 interval_excess_ms 是实际间隔减名义周期，不是计划偏移；详细 tx 的 schedule_offset_ms 保留后者。
run.timing 保存名义周期、容差、间隔上限、首发及间隔数量、mean_cycle_interval_ms 和 actual_cycle_rate_hz；
实测频率为 `1000 / mean_cycle_interval_ms`，涵盖首末发送之间的断流时间，不足两笔时均值和频率为 null。
严格回放：每个源样本按顺序消费；落后整周期或交易失败即失败，禁止悄悄丢源样本。
从站：独立数据时钟更新寄存器快照；读请求不推动源时间；启动/结束时刻语义显式记录。
当前从站在首次连接尝试后建立数据时钟；持续模式断线期间继续更新，重连不重新计时。
RTU 从站复用 TCP 的只读规则；持续模式串口 I/O 失败至少间隔 1 秒重连，严格模式失败退出。
RTU 接收只接受静默划定且 CRC 正确的完整帧；最大 256 字节，错误帧整帧丢弃。
静默以驱动交付时间计算，不宣称可测量物理线上的字符间隔；多点 RS485 需额外验收。

TCP 每个未完成请求独立使用固定接收期限，取 `max(1000ms, timeout_ms)`；同一请求的后续碎片不延长期限，
完成前一帧并开始下一帧时重新计时。过期的残帧在处理补齐数据前撤销，不执行过期写入。
客户端只关闭发送方向时，不再接收新请求或保留未完成尾帧，但先有界发送已处理请求的响应；
响应排空期限自读 EOF 起固定计算，同样取 `max(1000ms, timeout_ms)`，部分发送不延期。
仅完整响应提交 socket 后发布 TX 数值。接受连接的暂态错误只影响当次接受操作，真正监听器故障仍使会话失败；
同批就绪事件若指向已注销的客户端则忽略，不影响其他连接。上述 TCP 生命周期规则不改变 RTU 的分帧、超时或重连行为。

计划预算 = 所有请求/响应线路时间 + 必要静默 + 读回 + 设备/调度余量。
静默按实际串口格式计算；收到驱动交付数据后重新计时，持续噪声等待不突破 deadline。
write 返回时间、应答交付时间、源时间、计划时间分别保存。Windows 高精度计时请求成对释放。
一旦关联不确定，后续有效 ACK 仍保留 ambiguous，不能因重开串口就宣称严格关联恢复。
该标记是会话历史取证状态，不等于后续每笔新发生通信失败。首次 ack_correlation_uncertain 为 WARNING；
后续有效 ack_ambiguous / read_ambiguous 交易按 DEBUG 完整记录，错误交易仍按 WARNING，严格模式判定不变。
普通和关联不确定的分类计数不改名、不合并覆盖；domain.transactions.valid_reply_counts 仅在 GUI 刷新或结束汇总时计算合计，
不在通信路径重复累计合计值或增加队列。首页使用 valid_ack / valid_read，run.reply_summary 保存两项合计，均不宣称逐请求唯一确认。

LiveValues 将发送值 quality 与应答 confirmation 分开。确认更新只改变匹配发送时间的现有行，
不更新 sample_id / sampled_s、不重复入通信点队列；未发出的请求不改写上一笔的确认。
每次新提交重置该点 confirmation。部分寄存器及非有限值不因 ACK 成功被提升为有效数值，RX 的关联不确定门控保持原样。
worker 每个状态批次附带固定大小的 ack_correlation_uncertain 历史标记，GUI 在本会话内保留提示，重新配置新会话时清空。
没有新增线程、通信等待、补发或队列；保留原缓冲容量、批次上限和停止收尾流程。

## 6. 日志与 UI 性能

事件信封：schema_version、session_id、sequence、utc、monotonic_ns、level、code、phase、message、details。
语义事件包括 transaction、schedule_missed、reconnect、protocol_exception、ack_ambiguous、log_overflow。
文件记录详细上下文和堆栈；UI 展示简短原因、关键参数和定位提示，可展开详情。

通信线程只构造有界事件并非阻塞入队；专门写入线程批量 JSONL，按时间/大小刷新。
默认队列容量 8192，刷新间隔 200 ms，单文件不超过 32 MiB；通过 `logging.max_files` 配置最多保留的分片数，默认 100，范围 1～10000，包含当前文件。
分片写入独立封装在 reporting.segments；当前配置省略 logging 时沿用默认值，配置与最终摘要均记录保留策略。
首片为 events.jsonl，后续使用递增且不复用的 events-000002.jsonl 等名称，切片前刷新并关闭旧片，一条 JSONL 记录不跨片。
达到数量上限时先删除最旧片再新建，保证文件数不瞬时越界；单条记录若超过分片大小，先报错而不淘汰历史。
队列满时丢弃事件必须累计数量/级别/序号范围，恢复时写入缺口记录；最终摘要独立保存丢失计数。
关键错误保留独立的最后错误状态，不依赖已经拥塞的普通队列；摘要标记 evidence_complete=false。
磁盘故障作为 log_write_failed 进入失败/停止流程，使用 stderr/worker 状态兜底，不能静默继续假装成功。
滚动淘汰不停止会话，但将 retention_truncated 标为 true、evidence_complete 标为 false；
摘要 segments 单独记录淘汰文件数、记录数及生产者序号范围，不混入队列 dropped，也不把它当成通信故障。
最终摘要提供当前分片索引，实时状态不传长文件列表，避免日志保留配置较大时撑爆 worker 管道。
文件创建、刷新、关闭、写入或旧片删除失败均按 log_write_failed 停止；不自动删除其他会话目录。

GUI 不订阅全量报文：worker 单独产生最多 5 Hz 状态快照和批量警告。
UI 最多保留 2000 行日志，单批有上限，重复告警合并；完整证据保存在文件。
GUI 与 worker 使用版本化 JSONL 控制/状态协议；stdout 专用于协议，stderr 用于启动故障。
当前启动配置经临时 JSON 文件交给 worker，运行中 stdin 接收 schema_version=1、command=stop；
stdout 的 type 为 status 或 result。GUI 只展示状态/结果，工作进程通过同一个 SessionRunner 执行会话。
预览队列容量 100，输出队列容量 2；满时替换旧状态并记录预览日志丢失，原始文件证据独立保留。
输出线程使用无 Python 缓冲锁的管道写入，避免界面不读管道时阻塞工作进程解释器退出。
进程管道同样有界且异步发送，不允许 GUI 卡顿反向阻塞通信。停止先请求，超时才升级终止，并标记异常结束。

## 7. 配置、CLI 与交付

统一入口 `python -m modbus_dev` / `modbus-dev`；规划 validate、run、ports、analyze 子命令。
GUI 通过相同配置模型保存/加载；配置含 schema_version，未知字段和版本拒绝，禁止静默拼错生效。
配置不含现场默认串口或硬编码业务路径；示例明确标注仅用于测试。
运行目录保存 config.json、events*.jsonl、summary.json，CSV 为导出产物。
退出码：0 按策略正常完成；1 运行失败；2 配置/依赖错误；130 用户中断。持续模式通信异常计数不等于退出失败。

GUI 为可选依赖；核心最低 Python 版本在库评估后确定，GUI 可有更高依赖要求。
跨平台源码、分平台打包；先采用普通 Python + PyInstaller，Cython 不作为必需条件。
界面遵循工业调试台布局：配置/操作、运行状态、交易统计、可展开日志；不以动画和逐帧曲线干扰诊断。

## 8. 实施与验证顺序

M0：需求、设计、依赖选型和时序验收矩阵。
M1：独立包、配置模型、错误/事件契约、绝对调度和日志背压基础及单元测试。
M2：PyModbus 可行性测试/锁定版本，RTU 主站纵向打通；通过正常、迟到、CRC、异常、噪声、重连测试。
M3.1：常量/正弦 TCP 只读从站；独立客户端验证功能码白名单、地址、快照、数据时钟和停止。
M3.2：复用快照的 RTU 只读从站；独立主站与虚拟串口验证 CRC、分帧、站号、广播和资源释放。
M4：单会话 GUI、工作进程、限流展示、停止/崩溃恢复、打包自检。
M5：Windows 和麒麟独立安装验收、物理串口与标准设备验证、100 ms GUI/日志压力对照。
M6（暂缓，GUI 之后）：DYN 与 CSV 离线数据接入；先定义并验证格式、通道、时间、质量、缺口与结束语义。

当前优先级为 TCP 从站 → RTU 从站 → GUI，离线文件数据不作为这三个阶段的前置条件。
CSV 离线导入与现有规划中的运行结果 CSV 导出是不同能力；本轮不实现离线导入。

每阶段状态如实记录，未验证的端口/平台不标记通过。测试明确覆盖有效值、质量、时序和故障处理；记录已验证范围与限制。

逐点配置版本 4、TCP/RTU 统一独立区域与周期预算采用 [独立测点与周期预算](point-configuration.md) 的规则。
