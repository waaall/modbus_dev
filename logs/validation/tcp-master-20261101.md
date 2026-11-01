# TCP 主站源码验证（2026-11-01）

## 范围与环境

- 基线提交：`80f0d6b173faaf923b19b6fb68709989ed5fc8da`；验证对象为其后的本轮未提交修改。
- macOS arm64，Python 3.12.11、PyModbus 3.11.4、pySerial 3.5、PySide6 6.7.2。
- 范围：配置/权限、共享主站流程、独立 TCP 引擎、CLI/GUI worker、双窗口配套、原有完整回归。
- 仅使用本机临时回环端口、确定性独立字节对端、隔离 PTY 和离屏 Qt，不连接现场设备。

## 结果

| 检查 | 结果 | 详细证据 |
| --- | --- | --- |
| 最终完整回归 | 374 项：369 通过、5 项 Windows 专属跳过；37 项新增 TCP 主站测试全部通过 | `results/tcp-master-20261101/unittest-complete.log` |
| 协议与策略 | 四功能码、固定报文、混合类型/排列、读回、分片/粘连、错误事务号/字段、异常、固定截止、部分发送、停止、重连和事务号容量轮换 | 同上，`test_tcp_master` 系列 |
| GUI | 只读默认、显式写权限、草稿恢复、双语、计数、停止后未知写入提示、小屏连接字段可达性 | 同上，`test_tcp_master_gui` 系列 |
| 双窗口实际联调 | 两个独立 worker 与现有 TCP 从站配套，四功能码运行；主站 10 个有效 ACK、20 个有效读应答，读回不一致为 0；主从正常停止 | `results/tcp-master-20261101/gui-pair-wide/` |
| 配置与翻译 | 15 个任务示例校验通过（不含固定报文向量）；356 条翻译校验/编译通过 | `results/tcp-master-20261101/examples.json`、`translations-final.log` |

双窗口目录包含完整配置、分片事件日志、摘要、状态快照及中英文连接/数据/趋势截图，已检查新增字段及显示布局。
独立真实 socket 对端另覆盖分片响应与错误事务号后匹配，不只用自家主从站互证。

## 初次失败与复测

- 沙箱拒绝回环监听、Qt CPU 能力检测受限：分别保留 `targeted-initial.log`、`translations-initial.log`；
  获准在本机权限下重跑后通过，不作为产品协议失败。
- 首轮完整回归中的旧 GUI 用例假设下拉末项始终是 RTU 主站，并隐含写操作草稿；新增角色及其只读默认后该假设失效。
  已按实际末项校验键盘选择，再显式选择 RTU/读写/写操作验证数据源。原记录为 `unittest-first-host.log`、`unittest-final.log`。
- 一轮新增 GUI 用例已收到读取计数，但未等到独立的 5Hz 数值刷新，提前断言空快照；已等待计数与数值两条异步链路就绪。
  原记录为 `unittest-final-retest.log`，最终复测单独保存在 `unittest-complete.log`，未覆盖失败证据。

上述相对详细路径均以 `results/tcp-master-20261101/` 为前缀。

## 未覆盖

未进行 Windows、麒麟、真实 TCP 设备、物理 RS485、长时间压力或原生桌面验收；离屏与模拟测试不替代这些范围。
未重建 `dist/`、未进行 Cython/发布包验收；现有旧包不会自动获得新角色。

复现完整回归：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v`。
设计及使用入口见 [TCP 主站需求与设计](../../docs/tcp-master.md)、[用户手册](../../docs/user-manual.md#tcp-主从站本机配套)。
