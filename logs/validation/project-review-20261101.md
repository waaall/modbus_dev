# 项目审查与补充验证（2026-11-01）

## 范围与环境

- 基线：`8b573b8b5fe5d66d20413e59f5bdbe278ad4df55`，审查前工作区干净。
- macOS arm64，Python 3.12.11；PyModbus 3.11.4、pySerial 3.5、PySide6 6.7.2。
- 审查配置、数值编码、请求规划、调度、RTU/TCP 引擎、会话收尾、日志背压、worker 与 GUI 生命周期及发布验证入口。
- 只使用本地回环 TCP、隔离 PTY 和离屏 Qt；未打开现有串口或操作现场设备。本轮未修改业务代码或已有测试。
- 完整结果、临时补测脚本与原始失败证据：`results/review-20261101/`（Git 忽略）。

## 主要发现

### 1. P1：macOS 客户端断连可终止整个 TCP 会话

位置：`src/modbus_dev/engines/tcp_slave.py:70`。

原生 `KqueueSelector` 在同一轮可为同一个 socket 返回独立的读、写就绪项。读取 EOF 后 `_drop()` 已移除客户端，后续残留就绪项仍执行 `self.clients[sock]`，触发未捕获的 `KeyError`。真实回环连接发送完整请求后 `shutdown(SHUT_WR)`，实际 `SessionRunner` 返回 `state=failed / exit_code=1`；重复 5 次均复现。会话收尾会同时关闭监听器和其他客户端，不是单个客户端隔离失败。

建议：处理每项就绪事件前确认客户端仍有效，跳过已注销的残留项；补充原生 kqueue 双就绪和断连隔离回归。

### 2. P2：连续分片请求继承上一笔计时，正常客户端被误关闭

位置：`src/modbus_dev/engines/tcp_slave.py:78-79, 90-105`。

`pending_since` 只在收数据前缓冲为空时设置、全部消费为空时清除。若每次交付均包含“上一笔尾部 + 下一笔头部”，缓冲持续非空，新请求一直沿用最早请求的时间。真实 TCP 补测中每笔请求仅等待约 160ms，配置超时 1000ms，第 7 笔仍被以 `Incomplete request deadline` 断开。顺序请求跨越同样总时长正常，真正未完成的慢请求仍按固定期限关闭，排除了简单网络抖动解释。

建议：在完成一帧、开始处理新的残余帧时维护新请求的起始时间；同一未完成帧的后续碎片仍不得延长其期限。

### 3. P2：接受连接的暂态错误升级为整个会话失败

位置：`src/modbus_dev/engines/tcp_slave.py:60-68`。

监听器分支的 `accept()` 位于客户端异常隔离之外。分别注入一次 `BlockingIOError(EAGAIN)` 和 `ConnectionAbortedError(ECONNABORTED)` 后，实际会话都返回 `failed / exit_code=1`，即使策略为 continuous 也不继续服务。本项是故障注入确认，未声称在本机自然连接流量中触发了这两种系统错误。

建议：局部处理可恢复的接受连接错误并继续轮询，保留真正监听器故障的失败语义；清理注册失败的已接受 socket。

### 4. P2：客户端仅关闭发送方向时丢弃已排队响应

位置：`src/modbus_dev/engines/tcp_slave.py:72-76`。

客户端发送完整 FC03 请求后半关闭发送方向，仍可合法等待接收响应；当前代码将读 EOF 视为双向关闭，直接丢弃 `state.outgoing`。为避免第 1 项崩溃掩盖此问题，单独使用真实 `SelectSelector` 回环连接验证：请求已被处理，客户端收到空字节而非 FC03 响应。本轮未在 Windows/Linux 的原生选择器上另行实测。

建议：区分读方向 EOF 与完整连接失效，停止接收后先完成已有响应的有界发送，再关闭连接。该修复不能替代第 1 项的残留事件检查。

### 5. 验证工具：RTU 协议矩阵仍断言旧的完整测点读取规则

位置：`tools/verify_backend_communications.py:246-247`。

`check_protocol()` 对 TCP、RTU 均要求 `(address=0,count=1)` 和 `(address=1,count=2)` 返回异常 02，但当前 RTU 契约允许原始单寄存器/部分点读取。运行现有矩阵时 `rtu-slave-run`、`rtu-slave-worker` 因此失败；独立报文证据为请求 `010300000001840a`、合法响应 `01030241488822`。这两项不应被报告为 RTU 运行时回归，也不能把整套矩阵标记通过。失败使后续 RTU 噪声恢复检查在这两项中没有执行。

建议：按传输角色区分完整测点边界预期，增加验证工具自身的现行协议契约回归；不要修改引擎去满足旧断言。

## 验证结果

| 验证 | 结果 | 详细结果 |
| --- | --- | --- |
| 原有完整回归 | 326 项：321 通过、5 项 Windows 专属跳过 | `results/review-20261101/unittest-host.log` |
| TCP 边界补测 | 6 个方法：2 通过、4 失败；其中 accept 方法含 2 个失败子用例，共 5 个失败断言 | `results/review-20261101/tcp-boundaries-expanded.log` |
| macOS 会话崩溃重复验证 | 5/5 复现 | `results/review-20261101/kqueue-repeated.log` |
| 独立 CLI/worker 通信矩阵 | 8 场景：6 通过、2 因旧 RTU 断言失败 | `results/review-20261101/backend-matrix/report.json` |
| RTU 故障矩阵 | 6/6 通过：100ms、四点、200ms 正弦、读回、故障注入、严格失败 | `results/review-20261101/rtu-fault-matrix/report.json` |

初次沙箱运行因 Qt 无法检测 NEON 而中止；初次 TCP 补测被沙箱拒绝回环监听。两次失败的原始日志分别保留为 `unittest-initial.log`、`tcp-boundaries-initial.log`，正常本机权限复测另存文件，不将环境限制算作产品缺陷。

主要复现命令（项目根目录）：

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python results/review-20261101/probe_tcp_boundaries.py
.venv/bin/python tools/verify_backend_communications.py --pty \
  --output results/review-20261101/backend-matrix-rerun \
  --commit "$(git rev-parse HEAD)" --seconds 3
.venv/bin/python tools/verify_virtual_serial.py --pty \
  --output results/review-20261101/rtu-fault-matrix-rerun
```

补测按正确行为断言，当前代码预期失败，不能以脚本非零退出误判为探针运行故障。正式修复后应将相应回归纳入 `tests/`。本轮未构建或重新验收 `dist/` 发布包，未验证 Windows、麒麟、物理 RS485、标准设备与长期运行；离屏 GUI 和 PTY 不代替这些验收。

## 后续 TCP 修复与复测

用户确认后修复上述 4 类 TCP 运行问题。生产代码仅修改 `src/modbus_dev/engines/tcp_slave.py`，
未修改 RTU 引擎、公共寄存器区、请求规划、配置模型或会话调度逻辑。
TCP 接受、接收、发送、超时清理分别封装；半关闭响应有固定排空期限，过期残帧不再补齐执行。
实际 TX 数值仍在完整响应提交 socket 后发布，不因排队、部分发送或写阻塞提前发布。
协议行为说明补充到 `docs/design.md`。

新增 11 项正式回归：`tests/test_tcp_boundaries.py` 的 10 项，以及 `tests/test_tcp_slave.py` 的
真实会话多客户端半关闭测试。包含残留读写事件、接受异常/注册失败、连续分片、慢速碎片、过期写入、
半关闭完整读写响应、残余尾帧、固定排空期限和部分/阻塞写入。新增边界用例先在原代码上确认失败，
失败记录保留在 `results/tcp-fix-20261101/regression-before.log`。

最终结果（同一 macOS arm64 环境）：

| 验证 | 结果 | 详细结果 |
| --- | --- | --- |
| 最终完整回归 | 337 项：332 通过、5 项 Windows 专属跳过，包含全部 11 项新增用例 | `results/tcp-fix-20261101/unittest-final.log` |
| 审查时原始 TCP 复现脚本 | 6/6 通过，包含真实 kqueue 会话及接受错误注入 | `results/tcp-fix-20261101/original-probes-after.log` |
| 独立 RTU 故障矩阵 | 6/6 通过，未发现 TCP 修复导致的 RTU 回归 | `results/tcp-fix-20261101/rtu-fault-matrix/report.json` |

原始审查失败证据未覆盖，后续详细结果独立保存于 `results/tcp-fix-20261101/`。
上述第 5 项 RTU 验证脚本旧断言不属于本次 TCP 修复范围，未修改，也未将其旧矩阵重新标记为通过。
`dist/` 发布包未重建，源码安装需退出并重启 GUI/worker 后生效；本次复测仍不代替 Windows、麒麟和物理 RS485 验收。
