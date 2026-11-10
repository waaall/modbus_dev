# 验证记录归档

本目录保存典型测试的简洁总结与必要的精简 JSON 摘要，纳入 Git 版本管理；详细结果放在 `results/`。

## 记录索引

- [阶段实施记录](../implementation.md)
- [2026-11-10 麒麟虚拟机原生构建与实包验收](kylin-vm-20261110.md)
- [2026-11-01 TCP 主站源码与双窗口配套验证](tcp-master-20261101.md)
- [2026-10-28 发布流程修复与 Windows 实包验收](release-fixes-windows-20261028.md)
- [2026-10-27 通用数值类型源码验证](numeric-types-20261027.md)
- [2026-10-23 ec6829e 提交检查与 Windows 本机验证](review-ec6829e-windows-20261023.md)
- [2026-10-23 四测点独立数据与设置页源码验证](points-20261023.md)
- [2026-10-23 Windows x64 Cython 便携包实包验收](packaging-windows-20261023.md)
- [2026-10-23 macOS arm64 Cython 便携包实包验收](packaging-macos-20261023.md)

- [2026-10-23 Windows / 麒麟实际通信验证](validation-20261023.md)
- [2026-10-23 macOS Apple Silicon 通信与原生 GUI 验证](validation-macos-20261023.md)
- [M2 历史验证记录](validation-m2.md)

## 归档约定

- 需求、设计、使用说明、验证方法与验收标准放在 [`docs/`](../../docs/)。
- 验证报告按日期、平台或阶段区分；配套 JSON 放在与报告同名的目录下。
- 报告保留实测版本、环境、结果、未覆盖范围和原始证据位置；首次失败和复测结果分别保留。
- 原始日志、截图、报文及完整会话数据输出到 `results/`，沿用仓库的 Git 忽略规则；不忽略本归档目录。
- 报告中的复现命令及 `results/...` 路径均以项目根目录为基准；Markdown 链接相对于报告所在位置。
