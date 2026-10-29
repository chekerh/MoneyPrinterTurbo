"""字幕归一化的回归测试。

长视频的逐词时间轴会产生两类肉眼可见的坏字幕：时长为零的碎片（成片里闪
一下，什么都看不清）和停七八秒的长句（阅读节奏和口播脱节）。归一化只在
分段长视频路径启用，所以这些断言是那条路径唯一的守门测试。
"""

import pytest

from app.services import subtitle_normalize
from app.services.section_render import SrtEntry, parse_srt, write_srt


def _cue(start, end, text):
    return SrtEntry(start, end, text)


def test_zero_duration_fragment_is_merged_into_next_cue():
    """零长碎片并入下一条，不再单独占一条要闪一下的字幕。"""
    entries = [
        _cue(0.0, 2.0, "The harbor walls survived"),
        _cue(2.0, 2.0, "two"),
        _cue(2.0, 4.0, "millennia of salt."),
        _cue(4.0, 6.0, "Volcanic ash reacted with lime."),
    ]

    normalized = subtitle_normalize.normalize_entries(entries)

    assert all(entry.end > entry.start for entry in normalized)
    assert any("two" in entry.text for entry in normalized)
    assert any("two millennia" in entry.text for entry in normalized)
    assert normalized[0].start == 0.0


def test_trailing_short_fragment_is_merged_into_previous_cue():
    """末尾碎片没有下一条可并，并回上一条。"""
    entries = [
        _cue(0.0, 3.0, "A complete sentence."),
        _cue(3.0, 3.0, "Tail"),
    ]

    normalized = subtitle_normalize.normalize_entries(entries)

    assert len(normalized) == 1
    assert normalized[0].text.endswith("Tail")


def test_long_cue_is_split_without_zero_length_pieces():
    """超长 cue 按词切分，时间按字符占比分配，且不产出零长片段。"""
    text = " ".join(["word"] * 40)
    entries = [_cue(0.0, 12.0, text)]

    normalized = subtitle_normalize.normalize_entries(
        entries, max_seconds=5.0, max_chars_per_line=200, max_lines=2
    )

    assert len(normalized) >= 3
    assert normalized[0].start == 0.0
    assert normalized[-1].end == pytest.approx(12.0)
    for entry in normalized:
        assert entry.end > entry.start
        assert entry.text.strip()
    # 切分不能丢字。
    assert " ".join(entry.text for entry in normalized).split() == text.split()


def test_lines_wrap_at_word_boundaries():
    """折行只在词边界断开，且不超过两行。"""
    normalized = subtitle_normalize.normalize_entries(
        [_cue(0.0, 4.0, "The sea supplied the healing chemicals.")],
        max_seconds=10.0,
        max_chars_per_line=20,
        max_lines=2,
    )

    assert len(normalized) == 1
    lines = normalized[0].text.split("\n")
    assert len(lines) == 2
    for line in lines:
        assert line.split()  # 每行至少一个完整词
    # 折行不能丢词。
    assert " ".join(lines).split() == "The sea supplied the healing chemicals.".split()


def test_normalize_srt_file_round_trips(tmp_path):
    """原地归一化后仍能被 parse_srt 读回，且时间码有效。"""
    subtitle = tmp_path / "subtitle.srt"
    original = [
        _cue(0.0, 2.0, "The harbor walls survived"),
        _cue(2.0, 2.0, "two"),
        _cue(2.0, 4.0, "millennia of salt and waves."),
    ]
    assert write_srt(original, str(subtitle))

    assert subtitle_normalize.normalize_srt_file(str(subtitle)) is True

    reparsed = parse_srt(str(subtitle))
    assert reparsed
    assert all(entry.end > entry.start for entry in reparsed)
    assert "two millennia" in " ".join(entry.text for entry in reparsed)


def test_normalize_srt_file_rejects_unreadable_input(tmp_path):
    """读不出字幕时返回 False，且不动原文件。"""
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("not an srt", encoding="utf-8")

    assert subtitle_normalize.normalize_srt_file(str(subtitle)) is False
    assert subtitle.read_text(encoding="utf-8") == "not an srt"
