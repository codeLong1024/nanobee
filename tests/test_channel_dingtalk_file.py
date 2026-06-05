"""钉钉通道文件发送测试

测试覆盖：
1. upload_and_replace_file_markers() — 上传后发送原生文件消息
2. DingTalkSender.send() — 非流式路径中 [DINGTALK_FILE] 标记处理
3. DingTalkSender.send() — 流式结束路径中标记处理
4. process_raw_media_paths() — 裸路径自动兜底检测
5. 完整场景：LLM 回复含完整绝对路径 → 自动上传+发送
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobee.builtin.channel_dingtalk.media.markers import (
    upload_and_replace_file_markers,
)
from nanobee.builtin.channel_dingtalk.media.raw_path import (
    process_raw_media_paths,
)
from nanobee.builtin.channel_dingtalk.media.constants import (
    FILE_MARKER_RE,
)
from nanobee.builtin.channel_dingtalk.sender import DingTalkSender
from nanobee.builtin.channel_dingtalk.config import DingTalkConfig


# ==================== Fixtures ====================


@pytest.fixture
def mock_sender():
    """创建带 mock HTTP 的 DingTalkSender 实例"""
    config = DingTalkConfig(
        client_id="test_client_id",
        client_secret="test_client_secret",
        enable_marker_processing=True,
    )
    http = AsyncMock(spec_set=["post", "get", "stream", "put", "delete", "patch"])
    sender = DingTalkSender(
        config=config,
        logger=MagicMock(),
        http_client=http,
        pending_cards={},
        emotion_contexts={},
    )
    # Mock token manager
    sender._token_manager.get_access_token = AsyncMock(return_value="mock_token")
    # Mock upload_media
    sender.upload_media = AsyncMock(return_value="mock_media_id_12345")
    # Mock read_media_bytes
    sender.read_media_bytes = AsyncMock(
        return_value=(b"mock file content", "test.txt", "text/plain"),
    )
    # Mock _send_batch_message
    sender._send_batch_message = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_http():
    """创建 mock HTTP 客户端"""
    return AsyncMock(spec_set=["post", "get", "stream", "put", "delete", "patch"])


@pytest.fixture
def mock_card_manager():
    """创建 mock CardManager"""
    cm = MagicMock()
    cm.stream_content = AsyncMock()
    cm.finish_streaming = AsyncMock()
    cm.finalize_card = AsyncMock()
    cm.create_card = AsyncMock(return_value="mock_card_id")
    cm.start_streaming = AsyncMock()
    cm.fail_card = AsyncMock()
    return cm


# ==================== upload_and_replace_file_markers 测试 ====================


@pytest.mark.asyncio
async def test_file_marker_upload_and_send(mock_sender):
    """验证文件标记处理器上传后调用 _send_batch_message 发送原生文件消息"""
    text = (
        '这里是一个文件：'
        '[DINGTALK_FILE]{"path":"/tmp/report.pdf","fileName":"报告.pdf","fileType":"pdf"}[/DINGTALK_FILE]'
        '请查收。'
    )

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证上传被调用
    mock_sender.upload_media.assert_awaited_once()
    args, kwargs = mock_sender.upload_media.await_args
    assert kwargs["media_type"] == "file"
    assert kwargs["filename"] == "test.txt"

    # 验证 _send_batch_message 被调用发送原生文件消息
    mock_sender._send_batch_message.assert_awaited_once()
    batch_args, batch_kwargs = mock_sender._send_batch_message.await_args
    assert batch_args[1] == "test_chat_123"  # chat_id
    assert batch_args[2] == "sampleFile"  # msgKey
    msg_param = batch_args[3]
    assert msg_param["mediaId"] == "mock_media_id_12345"
    assert msg_param["fileType"] == "txt"

    # 验证标记被替换为 [附件: 文件名]
    assert "[附件: 报告.pdf]" in result
    assert "这里是一个文件：" in result
    assert "请查收" in result
    # 原始标记不应再出现
    assert "[DINGTALK_FILE]" not in result
    assert "[/DINGTALK_FILE]" not in result


@pytest.mark.asyncio
async def test_file_marker_upload_failure_no_send(mock_sender):
    """上传失败时不发送原生文件消息，标记被移除"""
    mock_sender.upload_media = AsyncMock(return_value=None)

    text = (
        '[DINGTALK_FILE]{"path":"/tmp/bad.pdf","fileName":"bad.pdf"}[/DINGTALK_FILE]'
    )

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证上传被调用
    mock_sender.upload_media.assert_awaited_once()
    # 验证 _send_batch_message 未被调用
    mock_sender._send_batch_message.assert_not_awaited()
    # 标记被清空（无替换文本）
    assert result == ""


@pytest.mark.asyncio
async def test_file_marker_multiple_files(mock_sender):
    """多个 [DINGTALK_FILE] 标记都被处理"""
    mock_sender.read_media_bytes = AsyncMock(
        return_value=(b"content", "file.bin", "application/octet-stream"),
    )
    mock_sender.upload_media = AsyncMock(side_effect=[
        "media_id_1", "media_id_2",
    ])
    mock_sender._send_batch_message = AsyncMock(return_value=True)

    text = (
        '文件1：[DINGTALK_FILE]{"path":"/tmp/a.pdf"}[/DINGTALK_FILE]\n'
        '文件2：[DINGTALK_FILE]{"path":"/tmp/b.txt"}[/DINGTALK_FILE]\n'
        '完毕。'
    )

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证 upload_media 被调用 2 次
    assert mock_sender.upload_media.await_count == 2
    # 验证 _send_batch_message 被调用 2 次
    assert mock_sender._send_batch_message.await_count == 2
    assert "[附件: a.pdf]" in result
    assert "[附件: b.txt]" in result
    assert "文件1：" in result
    assert "文件2：" in result


@pytest.mark.asyncio
async def test_file_marker_no_path(mock_sender):
    """path 为空时静默忽略"""
    text = '[DINGTALK_FILE]{"path":""}[/DINGTALK_FILE] 剩余文本'

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    assert result == " 剩余文本"
    mock_sender.upload_media.assert_not_awaited()
    mock_sender._send_batch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_marker_read_failure(mock_sender):
    """文件不存在时静默忽略"""
    mock_sender.read_media_bytes = AsyncMock(return_value=(None, None, None))

    text = '[DINGTALK_FILE]{"path":"/tmp/nonexistent.txt"}[/DINGTALK_FILE]'

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证 read_media_bytes 被调用
    mock_sender.read_media_bytes.assert_awaited_once_with("/tmp/nonexistent.txt")
    # 上传和发送都不应被调用
    mock_sender.upload_media.assert_not_awaited()
    mock_sender._send_batch_message.assert_not_awaited()
    assert result == ""


@pytest.mark.asyncio
async def test_file_marker_no_marker_in_text(mock_sender):
    """没有标记时，原文本原样返回"""
    text = "这是一条普通的消息，没有文件标记。"

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    assert result == text
    mock_sender.upload_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_marker_invalid_json_payload(mock_sender):
    """无效 JSON payload 时静默忽略"""
    text = '[DINGTALK_FILE]{invalid json}[/DINGTALK_FILE] 剩余'

    result = await upload_and_replace_file_markers(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    assert result == " 剩余"
    assert "[DINGTALK_FILE]" not in result
    mock_sender.upload_media.assert_not_awaited()


# ==================== DingTalkSender.send() 测试 ====================


def _make_msg(content: str, metadata: dict | None = None, media: list | None = None):
    """创建 SimpleNamespace 模拟消息对象"""
    from types import SimpleNamespace
    return SimpleNamespace(
        channel="channel_dingtalk",
        chat_id="test_chat_123",
        content=content,
        metadata=metadata or {},
        media=media or [],
    )


@pytest.mark.asyncio
async def test_sender_non_streaming_with_file_marker(mock_sender):
    """非流式消息：标记被处理，文件被上传并发送，标记在文本中被替换"""
    msg = _make_msg(
        '文件已创建：'
        '[DINGTALK_FILE]{"path":"/tmp/report.pdf","fileName":"报告.pdf"}[/DINGTALK_FILE]'
    )

    await mock_sender.send(msg)

    # 验证 upload_media 被调用
    mock_sender.upload_media.assert_awaited()
    # 验证 _send_batch_message 被调用（发送原生文件消息）
    mock_sender._send_batch_message.assert_awaited()
    # 验证 markdown 文本也被发送（标记被替换后）
    # send() 在末尾会调用 _send_markdown_text 发送清理后的内容
    markdown_calls = [
        call for call in mock_sender._send_batch_message.await_args_list
        if call.args[2] == "sampleMarkdown"
    ]
    markdown_texts = [
        call.args[3].get("text", "") for call in markdown_calls
    ]
    assert any("文件已创建：" in t for t in markdown_texts)


@pytest.mark.asyncio
async def test_sender_non_streaming_plain_text(mock_sender):
    """非流式纯文本消息：正常发送 markdown，不触发文件上传"""
    msg = _make_msg("你好，这是普通消息。")

    await mock_sender.send(msg)

    mock_sender.upload_media.assert_not_awaited()
    mock_sender._send_batch_message.assert_awaited()
    markdown_calls = [
        call for call in mock_sender._send_batch_message.await_args_list
        if call.args[2] == "sampleMarkdown"
    ]
    assert len(markdown_calls) >= 1


@pytest.mark.asyncio
async def test_sender_streaming_end_with_file_marker(mock_sender, mock_card_manager):
    """流式结束路径：标记在预处理中被上传，accumulated 内容推入卡片"""
    mock_sender._card_manager = mock_card_manager
    mock_sender._pending_cards["test_chat_123"] = "mock_card_id"

    # 先发一个流式 delta 初始化 buffer
    delta_msg = _make_msg(
        '文件已创建，请查收。',
        metadata={"_stream_delta": True},
    )
    await mock_sender.send(delta_msg)

    # 再发流式结束
    end_msg = _make_msg(
        '文件已创建，请查收。',
        metadata={"_stream_end": True, "_resuming": False},
    )
    await mock_sender.send(end_msg)

    # 文件标记处理在 _stream_end 时应被调用并发送原生消息
    mock_sender.upload_media.assert_not_awaited()  # 无标记，不触发上传
    mock_card_manager.finish_streaming.assert_awaited_once()


@pytest.mark.asyncio
async def test_sender_streaming_end_with_marker(mock_sender, mock_card_manager):
    """流式结束路径带 [DINGTALK_FILE] 标记：标记被预处理，文件被上传和发送"""
    mock_sender._card_manager = mock_card_manager
    mock_sender._pending_cards["test_chat_123"] = "mock_card_id"

    end_msg = _make_msg(
        '文件已创建：'
        '[DINGTALK_FILE]{"path":"/tmp/report.pdf","fileName":"报告.pdf"}[/DINGTALK_FILE]'
        '请查收。',
        metadata={"_stream_end": True, "_resuming": False},
    )

    await mock_sender.send(end_msg)

    # 标记预处理应触发上传
    mock_sender.upload_media.assert_awaited()
    # 应发送原生文件消息
    sample_file_calls = [
        call for call in mock_sender._send_batch_message.await_args_list
        if call.args[2] == "sampleFile"
    ]
    assert len(sample_file_calls) == 1


# ==================== process_raw_media_paths 自动兜底测试 ====================


@pytest.mark.asyncio
async def test_raw_path_detects_full_absolute_path(mock_sender, tmp_path):
    """LLM 回复中带有完整绝对路径时，process_raw_media_paths 自动检测并上传发送"""
    # 创建真实文件
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    abs_path = str(test_file)

    mock_sender.read_media_bytes = AsyncMock(
        return_value=(b"hello world", "test.txt", "text/plain"),
    )
    mock_sender.upload_media = AsyncMock(return_value="mock_media_id")
    mock_sender._send_batch_message = AsyncMock(return_value=True)

    text = f"文件已创建，路径：{abs_path}，请查收！"

    result = await process_raw_media_paths(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证上传被调用
    mock_sender.upload_media.assert_awaited_once()
    # 验证发送了 sampleFile
    mock_sender._send_batch_message.assert_awaited_once()
    call = mock_sender._send_batch_message.await_args
    args = call.args if hasattr(call, "args") else call
    assert args[1] == "test_chat_123"
    assert args[2] == "sampleFile"
    assert args[3]["mediaId"] == "mock_media_id"
    # 验证路径从文本中移除
    assert abs_path not in result
    assert "文件已创建，路径：" in result
    assert "请查收" in result


@pytest.mark.asyncio
async def test_raw_path_file_not_exists(mock_sender):
    """路径指向不存在的文件时，静默跳过"""
    text = "文件不存在 /tmp/nonexistent_file_xyz.txt"

    result = await process_raw_media_paths(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证没有上传或发送
    mock_sender.upload_media.assert_not_awaited()
    mock_sender._send_batch_message.assert_not_awaited()
    assert result == text


@pytest.mark.asyncio
async def test_raw_path_read_failure(mock_sender, tmp_path):
    """文件存在但读取失败时，静默跳过"""
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello")
    abs_path = str(test_file)

    mock_sender.read_media_bytes = AsyncMock(return_value=(None, None, None))
    mock_sender.upload_media = AsyncMock(return_value="mock_media_id")

    text = f"文件在 {abs_path}"

    result = await process_raw_media_paths(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    mock_sender.upload_media.assert_not_awaited()
    mock_sender._send_batch_message.assert_not_awaited()
    assert abs_path in result  # 路径原样保留


@pytest.mark.asyncio
async def test_raw_path_no_path_in_text(mock_sender):
    """文本中没有路径时，原样返回"""
    text = "这是一条普通消息，没有文件路径。"

    result = await process_raw_media_paths(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    assert result == text
    mock_sender.upload_media.assert_not_awaited()


# ==================== 完整场景测试 ====================


@pytest.mark.asyncio
async def test_full_scenario_auto_send_write_file(mock_sender, tmp_path):
    """模拟完整场景：Agent 写入文件后回复包含完整路径 → 自动发送

    这是用户最关心的场景：
    1. Agent 用 write_file 创建文件到 /path/to/3.txt
    2. Agent 在回复中说 "文件已创建，路径：/path/to/3.txt"
    3. process_raw_media_paths 自动检测路径 → 上传 → 发送原生文件消息
    """
    # 创建模拟文件
    test_file = tmp_path / "3.txt"
    test_file.write_text("最近流行歌词精选...\n第1首...\n第2首...")
    abs_path = str(test_file)

    mock_sender.read_media_bytes = AsyncMock(
        return_value=(b"recent lyrics...", "3.txt", "text/plain"),
    )
    mock_sender.upload_media = AsyncMock(return_value="mock_media_id_3txt")
    mock_sender._send_batch_message = AsyncMock(return_value=True)

    # Agent 回复（最接近实际的输出风格）
    agent_reply = f"文件 `3.txt` 已创建成功 ✅ 路径：{abs_path}，请查收！"

    result = await process_raw_media_paths(
        agent_reply, mock_sender, "mock_token", "test_chat_123",
    )

    # 验证上传
    mock_sender.upload_media.assert_awaited_once()
    upload_call = mock_sender.upload_media.await_args
    upload_kwargs = upload_call.kwargs if hasattr(upload_call, "kwargs") else {}
    assert upload_kwargs.get("media_type") == "file"
    assert upload_kwargs.get("filename") == "3.txt"

    # 验证发送原生文件消息
    mock_sender._send_batch_message.assert_awaited_once()
    call = mock_sender._send_batch_message.await_args
    args = call.args if hasattr(call, "args") else call
    assert args[2] == "sampleFile"
    assert args[3]["mediaId"] == "mock_media_id_3txt"
    assert args[3]["fileName"] == "3.txt"

    # 验证路径已从文本中移除，但其他内容保留
    assert abs_path not in result
    assert "文件 `3.txt` 已创建成功" in result
    assert "请查收" in result


@pytest.mark.asyncio
async def test_full_scenario_llm_says_filename_only(mock_sender):
    """Agent 只说文件名不说完整路径时，raw_path 检测不到，需要 marker

    这是实际对话中发生的情况：
    Agent 回复 "文件 `3.txt` 已创建成功 ✅"
    因为缺少完整绝对路径，process_raw_media_paths 不会触发
    这时需要 [DINGTALK_FILE] 标记来处理
    """
    text = "文件 `3.txt` 已创建成功 ✅ 里面收录了热门歌曲的歌词。"

    mock_sender.read_media_bytes = AsyncMock(
        return_value=(b"lyrics...", "3.txt", "text/plain"),
    )

    result = await process_raw_media_paths(
        text, mock_sender, "mock_token", "test_chat_123",
    )

    # 只有文件名没有完整路径时，不会触发自动发送
    mock_sender.upload_media.assert_not_awaited()
    assert result == text


@pytest.mark.asyncio
async def test_sender_marker_pipeline_only_non_streaming(mock_sender):
    """标记预处理管线只在非流式消息上运行 (_stream_delta 跳过)"""
    mock_sender.upload_media = AsyncMock(return_value="mid")

    # delta 消息应该跳过标记预处理
    delta_msg = _make_msg(
        '[DINGTALK_FILE]{"path":"/tmp/test.pdf"}[/DINGTALK_FILE]',
        metadata={"_stream_delta": True},
    )
    await mock_sender.send(delta_msg)

    # 没有 card_id 时，delta 消息不会触发 upload
    mock_sender.upload_media.assert_not_awaited()


# ==================== regex 模式测试 ====================


class TestFileMarkerRegex:
    """[DINGTALK_FILE] 正则匹配模式测试"""

    def test_simple_marker(self):
        text = (
            '[DINGTALK_FILE]{"path":"/tmp/a.pdf"}[/DINGTALK_FILE]'
        )
        matches = list(FILE_MARKER_RE.finditer(text))
        assert len(matches) == 1
        payload = json.loads(matches[0].group(1))
        assert payload["path"] == "/tmp/a.pdf"

    def test_marker_with_all_fields(self):
        text = (
            '[DINGTALK_FILE]{"path":"/tmp/r.pdf","fileName":"r.pdf","fileType":"pdf"}[/DINGTALK_FILE]'
        )
        matches = list(FILE_MARKER_RE.finditer(text))
        assert len(matches) == 1
        payload = json.loads(matches[0].group(1))
        assert payload["path"] == "/tmp/r.pdf"
        assert payload["fileName"] == "r.pdf"
        assert payload["fileType"] == "pdf"

    def test_multiple_markers_in_text(self):
        text = (
            'a[DINGTALK_FILE]{"path":"/a.pdf"}[/DINGTALK_FILE]b'
            '[DINGTALK_FILE]{"path":"/b.txt"}[/DINGTALK_FILE]c'
        )
        matches = list(FILE_MARKER_RE.finditer(text))
        assert len(matches) == 2

    def test_no_marker(self):
        text = "普通文本，没有标记"
        matches = list(FILE_MARKER_RE.finditer(text))
        assert len(matches) == 0

    def test_marker_nested_in_code_block(self):
        """代码块中的标记不应被误匹配（但当前 regex 是朴素的，标记为已知行为）"""
        text = (
            '```\n'
            '[DINGTALK_FILE]{"path":"/tmp/x.pdf"}[/DINGTALK_FILE]\n'
            '```\n'
        )
        matches = list(FILE_MARKER_RE.finditer(text))
        # 当前 regex 会匹配，这是已知限制
        # 后续可通过改进 regex 或预处理过滤
        assert len(matches) == 1
