"""长视频分段 TTS 的失败恢复矩阵。

真实故障（2026-10-28 的 8 分钟冒烟）：前 6 段已经合成成功，第 7 段连续三次
撞上 ``edge_tts stream timed out after 30s`` 后整条旁白被丢弃，11 分钟音频
和 20 分钟渲染时间一起作废。这些用例钉住的是"一段抖动不该毁掉整篇"。

全部走假 ``voice.tts``：不碰网络，也不写真实音频。
"""

import datetime

from edge_tts import SubMaker
from edge_tts.srt_composer import Subtitle

from app.services import tts_chunking


def _submaker(base=0.0, cues=2, span=1.0):
    """造一份时间轴从 ``base`` 开始的 SubMaker，模拟单个分段的 TTS 结果。"""
    maker = SubMaker()
    maker.subs = [f"s{index}" for index in range(cues)]
    maker.offset = [
        (
            int((base + index * span) * 10_000_000),
            int((base + index * span + 0.8) * 10_000_000),
        )
        for index in range(cues)
    ]
    maker.cues = [
        Subtitle(
            index,
            datetime.timedelta(seconds=base + index * span),
            datetime.timedelta(seconds=base + index * span + 0.8),
            f"c{index}",
        )
        for index in range(cues)
    ]
    return maker


def _text(chunks: int) -> str:
    """生成 ``chunks`` 个可区分的自然段，每段都会被切成独立的一次请求。

    每段 130 字符 > ``MIN_CHUNK_CHARS``，且两段相加超过切分上限，因此
    ``split_for_tts`` 不会把它们合并。
    """
    letters = "abcde"
    return "\n\n".join(letters[index] * 130 for index in range(chunks))


class Recorder:
    """假的 TTS 提供方：按段落首字母决定第几次调用失败。"""

    def __init__(self, failures=None, durations=5.0):
        #: ``{"a": 2}`` 表示首字母为 a 的段落前 2 次调用失败。
        self.failures = dict(failures or {})
        self.durations = durations
        self.calls = []
        self.slept = []
        self.concat_order = []

    def key(self, text: str) -> str:
        return text.strip()[0]

    def tts(self, text, voice_name, voice_rate, voice_file):
        key = self.key(text)
        self.calls.append(key)
        remaining = self.failures.get(key, 0)
        if remaining > 0:
            self.failures[key] = remaining - 1
            return None
        return _submaker()

    def duration(self, _path):
        return self.durations

    def concat(self, sources, output_file):
        self.concat_order.extend(path.name for path in sources)
        return True

    def sleep(self, seconds):
        self.slept.append(seconds)


def _install(monkeypatch, recorder):
    monkeypatch.setattr(tts_chunking.voice, "tts", recorder.tts)
    monkeypatch.setattr(tts_chunking.voice, "get_audio_duration", recorder.duration)
    monkeypatch.setattr(tts_chunking, "_concat_audio_files", recorder.concat)
    monkeypatch.setattr(tts_chunking.time, "sleep", recorder.sleep)


def test_retries_only_the_failed_chunk(monkeypatch, tmp_path):
    recorder = Recorder(failures={"b": 1})
    _install(monkeypatch, recorder)
    progress = []

    result = tts_chunking.tts_chunked(
        text=_text(4),
        voice_name="en-US-AvaMultilingualNeural-Female",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
        chunk_dir=str(tmp_path),
        max_chars=tts_chunking.MIN_CHUNK_CHARS,
        on_chunk_done=lambda done, total, _seconds: progress.append((done, total)),
    )

    assert result is not None
    # 4 段 + 重试 b 一次。
    assert recorder.calls == ["a", "b", "c", "d", "b"]
    assert recorder.slept == [2.0]
    # 重试不影响拼接顺序。
    assert recorder.concat_order == [
        "narration-001.mp3",
        "narration-002.mp3",
        "narration-003.mp3",
        "narration-004.mp3",
    ]
    assert progress == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_progress_never_goes_backwards_when_middle_chunks_retry(monkeypatch, tmp_path):
    recorder = Recorder(failures={"a": 1, "d": 1})
    _install(monkeypatch, recorder)
    progress = []

    result = tts_chunking.tts_chunked(
        text=_text(4),
        voice_name="voice",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
        chunk_dir=str(tmp_path),
        max_chars=tts_chunking.MIN_CHUNK_CHARS,
        on_chunk_done=lambda done, total, _seconds: progress.append(done),
    )

    assert result is not None
    # 第一轮只有 b、c 成功；第二轮补上 a、d。若按"当前序号"回报，
    # 第二轮会写出 1、4，进度条就会倒退。
    assert progress == [1, 2, 3, 4]
    assert progress == sorted(progress)


def test_aborts_after_one_round_when_nothing_succeeds(monkeypatch, tmp_path):
    recorder = Recorder(failures={"a": 99, "b": 99})
    _install(monkeypatch, recorder)

    result = tts_chunking.tts_chunked(
        text=_text(2),
        voice_name="voice",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
        chunk_dir=str(tmp_path),
        max_chars=tts_chunking.MIN_CHUNK_CHARS,
    )

    assert result is None
    # 服务全灭时不该再等两轮。
    assert recorder.calls == ["a", "b"]
    assert recorder.slept == []


def test_circuit_breaker_stops_after_three_consecutive_failures(monkeypatch, tmp_path):
    recorder = Recorder(failures={"a": 99, "b": 99, "c": 99})
    _install(monkeypatch, recorder)

    result = tts_chunking.tts_chunked(
        text=_text(5),
        voice_name="voice",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
        chunk_dir=str(tmp_path),
        max_chars=tts_chunking.MIN_CHUNK_CHARS,
    )

    assert result is None
    # 第 4 段本来会成功，但连续三段失败已经足够判定服务不可用。
    assert recorder.calls == ["a", "b", "c"]


def test_gives_up_only_after_all_retry_rounds(monkeypatch, tmp_path):
    recorder = Recorder(failures={"a": 99})
    _install(monkeypatch, recorder)

    result = tts_chunking.tts_chunked(
        text=_text(2),
        voice_name="voice",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
        chunk_dir=str(tmp_path),
        max_chars=tts_chunking.MIN_CHUNK_CHARS,
    )

    assert result is None
    # a 每轮都失败，b 只成功一次后就已完成，不再重复合成。
    assert recorder.calls == ["a", "b", "a", "a"]
    assert recorder.slept == [2.0, 10.0]


def test_single_chunk_still_uses_the_single_request_path(monkeypatch, tmp_path):
    recorder = Recorder()
    _install(monkeypatch, recorder)

    result = tts_chunking.tts_chunked(
        text="一句话。",
        voice_name="voice",
        voice_rate=1.0,
        voice_file=str(tmp_path / "narration.mp3"),
    )

    assert result is not None
    assert recorder.calls == ["一"]
    assert recorder.slept == []
    assert recorder.concat_order == []
