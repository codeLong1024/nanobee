# 变更日志（Changelog）

本项目所有显著变更记录于此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

- **钉钉通道**：richText 入站消息不再被静默丢弃。
  钉钉 Stream 回调的 richText 项是裸 `{"text": ...}`（不带 `type` 字段），
  原判据 `item.get("type") == "text"` 导致所有文本项被跳过 → 内容为空 →
  消息被判空丢弃，agent 完全不触发（单聊富文本与群聊 @ 均命中，用户侧表现
  为"机器人没有任何回复"）。现按「全面对齐 SDK 判据」修复：文本项
  `"text" in item`、媒体项 `"downloadCode" in item`（与
  `dingtalk_stream` SDK 的 `get_text_list` / `get_image_list` 一致），
  两条判据彼此独立（同项同时含文本与下载码时两者都保留），保留
  `fileName`，并对 `text=None` 容错。
  已通过真实钉钉环境端到端验证（dws 群 @ 富文本注入 + dws 单聊富文本注入）。

### Changed

- **钉钉通道**：流式回复日志降噪，遵循「高频明细进 TRACE，DEBUG 只留状态
  与异常」。
  `stream_content` 逐 chunk 明细（完整内容与成功响应体）由 DEBUG 降为
  `TRACE`；`finish_streaming` 的 body（含完整 msgContent，与回复正文重复）
  同步降级。DEBUG 仅保留一次性状态行与**非 200 响应**（QpsLimit/500 等故障
  仍默认可见）。需完整回放时将实例配置 `logging.level` 设为 `TRACE`
  （loguru 原生级别，文件 sink 直接透传，无需改动 schema）。
  实测（DEBUG 级，5 个 chunk + 终态 + 1 个 500 异常）：相关日志 12 行 → 2 行。

### Added

- `tests/test_channel_dingtalk.py::TestRichTextParsing`：richText 解析
  回归测试 9 条（裸 text 项、`process()` 不再丢弃、群 @ 报文、媒体项与
  `fileName` 保留、文本+下载码同项双保留、显式 `type` 形态兼容、
  `text=None` 容错、空 richText 列表、`msgtype=text` 路径不受影响）。
- `tests/test_card_stream_log_level.py::TestStreamRespLogLevel`：流式
  响应日志分级契约测试 2 条（200 响应不落 DEBUG、非 200 响应必落 DEBUG
  且携带 status 与 body）。
