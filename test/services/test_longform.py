"""长视频分段流水线的纯逻辑矩阵。

只覆盖不碰网络、不写磁盘的决策函数：档位与时长换算、脚本切分、字幕时间轴
合并、素材额度分配、模型输出解析。每一项都钉住一条具体的失败模式——这些
地方出错时症状是"成片能出但内容是坏的"，比崩溃难查得多。

媒体渲染（``section_render.render_sections``）依赖真实 ffmpeg 和素材，另在
``test_longform_render.py`` 里用合成素材覆盖。
"""

import datetime

import pytest
from edge_tts import SubMaker
from edge_tts.srt_composer import Subtitle

from app.services import longform, section_materials, tts_chunking
from app.services.longform import Section


def _submaker(base=0.0, cues=3, span=1.0):
    """造一份时间轴从 ``base`` 开始的 SubMaker，模拟单个分段的 TTS 结果。"""
    maker = SubMaker()
    maker.subs = []
    maker.offset = []
    maker.cues = [
        Subtitle(
            index,
            datetime.timedelta(seconds=base + index * span),
            datetime.timedelta(seconds=base + index * span + 0.8),
            f"c{index}",
        )
        for index in range(cues)
    ]
    maker.subs = [f"s{index}" for index in range(cues)]
    maker.offset = [
        (
            int((base + index * span) * 10_000_000),
            int((base + index * span + 0.8) * 10_000_000),
        )
        for index in range(cues)
    ]
    return maker


class TestLengthBands:
    def test_short_band_is_not_sectioned(self):
        """short 必须保持非分段，否则所有历史短视频任务都会改走新路径。"""
        assert longform.resolve_band("short").sectioned is False

    def test_medium_and_long_are_sectioned(self):
        assert longform.resolve_band("medium").sectioned is True
        assert longform.resolve_band("long").sectioned is True

    def test_unknown_band_degrades_to_short(self):
        """渠道名/API 里的拼写错误不该让整条视频失败。"""
        assert longform.resolve_band("bogus").key == "short"
        assert longform.resolve_band(None).key == "short"
        assert longform.is_longform("bogus") is False

    def test_band_case_and_whitespace_tolerated(self):
        assert longform.resolve_band("  LONG ").key == "long"

    @pytest.mark.parametrize(
        "target,expected_low,expected_high",
        [(1500, 19, 21), (720, 9, 11), (480, 6, 8)],
    )
    def test_section_count_tracks_target_length(self, target, expected_low, expected_high):
        count = longform.plan_section_count(target)
        assert expected_low <= count <= expected_high
        assert count <= longform.MAX_SECTIONS

    def test_section_count_is_bounded(self):
        """段数等于 LLM 调用次数，必须有上限。"""
        assert longform.plan_section_count(10_000_000) == longform.MAX_SECTIONS

    def test_short_target_still_gets_minimum_sections(self):
        assert longform.plan_section_count(10) >= longform.MIN_LONGFORM_SECTIONS

    def test_explicit_target_is_clamped_to_band(self):
        """medium 档不能要 40 分钟：分段数是按档位算的，超出会生成不足的文案。"""
        assert longform.resolve_target_seconds("medium", 2400) == 900
        assert longform.resolve_target_seconds("medium", 60) == 480

    def test_explicit_target_inside_band_is_kept(self):
        assert longform.resolve_target_seconds("medium", 600) == 600

    def test_non_numeric_target_falls_back_to_band_default(self):
        assert longform.resolve_target_seconds("long", "abc") == 1500

    def test_channel_target_used_only_when_no_explicit_value(self):
        assert longform.resolve_target_seconds("medium", None, 800) == 800
        assert longform.resolve_target_seconds("medium", 600, 800) == 600

    def test_word_budget_scales_with_length(self):
        assert longform.words_for_target(60) < longform.words_for_target(1500)
        # 中文字数口径必须明显高于英文词数，否则中文脚本会被写得过短。
        assert longform.words_for_target(60, cjk=True) > longform.words_for_target(60)


class TestSplitForTts:
    def test_short_script_is_one_chunk(self):
        assert len(tts_chunking.split_for_tts("一句话。")) == 1

    def test_empty_text_yields_no_chunks(self):
        assert tts_chunking.split_for_tts("") == []
        assert tts_chunking.split_for_tts("   \n\n  ") == []

    def test_long_script_is_split(self):
        paragraph = "这是第一句话。这是第二句话，稍微长一点用于测试断句行为。" * 10
        text = "\n\n".join([paragraph] * 6)
        assert len(tts_chunking.split_for_tts(text)) > 1

    def test_no_chunk_exceeds_the_limit(self):
        """超限意味着撞服务商硬上限，这是长视频失败的头号原因。"""
        paragraph = "句子用来撑长度，必须超过单次请求上限才会触发切分。" * 20
        text = "\n\n".join([paragraph] * 8)
        chunks = tts_chunking.split_for_tts(text)
        assert chunks
        assert max(len(chunk) for chunk in chunks) <= tts_chunking.MAX_CHUNK_CHARS

    def test_no_text_is_lost(self):
        """丢字等于丢内容：旁白会少一段而任务看起来完全成功。"""
        paragraph = "第一句话内容在这里。第二句话也在这里，用来加长段落。" * 10
        text = "\n\n".join([paragraph] * 6)
        chunks = tts_chunking.split_for_tts(text)
        assert len(chunks) > 1, "fixture must actually split"
        rejoined = "".join(chunk.replace("\n", "") for chunk in chunks)
        assert rejoined == text.replace("\n", "")

    def test_large_enough_to_be_meaningful(self):
        """守住上面两条测试的前提：脚本本身必须真的长于单次请求上限，
        否则"没有块超限"和"没有字丢失"都是空断言。"""
        paragraph = "第一句话内容在这里。第二句话也在这里，用来加长段落。" * 10
        text = "\n\n".join([paragraph] * 6)
        assert len(text) > tts_chunking.MAX_CHUNK_CHARS

    def test_paragraphs_are_preferred_split_points(self):
        """段落边界同时是字幕匹配边界，切在这里字幕才能逐段对上。"""
        first = "甲段落内容。" * 40
        second = "乙段落内容。" * 40
        chunks = tts_chunking.split_for_tts(f"{first}\n\n{second}", max_chars=420)
        assert chunks[0].startswith("甲")
        assert chunks[-1].startswith("乙")

    def test_tiny_fragments_are_merged(self):
        """碎片单独发一次请求纯属浪费，而且打断韵律。"""
        text = "\n\n".join(["短。", "也短。", "还是短。"] * 8)
        chunks = tts_chunking.split_for_tts(text, max_chars=200)
        assert len(chunks) < len(text.split("\n\n"))


class TestMergeSubMakers:
    def test_offsets_are_shifted_per_chunk(self):
        merged = tts_chunking.merge_sub_makers(
            [(_submaker(0.0), 10.0), (_submaker(0.0), 10.0)]
        )
        starts = [cue.start.total_seconds() for cue in merged.cues]
        assert starts[:3] == [0.0, 1.0, 2.0]
        assert starts[3:] == [10.0, 11.0, 12.0]

    def test_srt_indexes_are_renumbered_globally(self):
        """每份 SubMaker 的序号都从 0 开始；不重排会写出 "1,1,2,2" 的非法 SRT。"""
        merged = tts_chunking.merge_sub_makers(
            [(_submaker(0.0), 10.0), (_submaker(0.0), 10.0), (_submaker(0.0), 10.0)]
        )
        assert [cue.index for cue in merged.cues] == list(range(1, 10))

    def test_cues_stay_chronological_and_non_overlapping(self):
        merged = tts_chunking.merge_sub_makers(
            [(_submaker(0.0), 7.5), (_submaker(0.0), 12.0)]
        )
        starts = [cue.start.total_seconds() for cue in merged.cues]
        assert starts == sorted(starts)
        for earlier, later in zip(merged.cues, merged.cues[1:]):
            assert earlier.end <= later.start

    def test_legacy_subs_and_offset_are_shifted(self):
        """Azure/Gemini 路径读的是 subs/offset，不平移这些字段它们的字幕会全错。"""
        merged = tts_chunking.merge_sub_makers(
            [(_submaker(0.0), 10.0), (_submaker(0.0), 10.0)]
        )
        assert merged.subs == ["s0", "s1", "s2"] * 2
        second_chunk_start = merged.offset[3][0] / 10_000_000
        assert second_chunk_start == 10.0

    def test_single_chunk_is_returned_usable(self):
        merged = tts_chunking.merge_sub_makers([(_submaker(0.0), 5.0)])
        assert len(merged.cues) == 3

    def test_no_usable_parts_returns_none(self):
        assert tts_chunking.merge_sub_makers([(None, 5.0), (None, 5.0)]) is None
        assert tts_chunking.merge_sub_makers([]) is None


class TestOutlineAndTermsParsing:
    def test_plain_array_outline(self):
        payload = [{"title": "开场", "intent": "抓住观众"}, {"title": "正文"}]
        plan = longform.build_plan(
            band=longform.LENGTH_BANDS["medium"],
            target_seconds=720,
            outline_payload=payload,
        )
        assert len(plan.outline) == longform.plan_section_count(720)
        assert plan.outline[0].title == "开场"

    def test_dict_wrapped_outline(self):
        payload = {"sections": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}
        plan = longform.build_plan(
            band=longform.LENGTH_BANDS["long"],
            target_seconds=1500,
            outline_payload=payload,
        )
        assert [section.title for section in plan.outline[:3]] == ["A", "B", "C"]

    def test_missing_sections_are_padded(self):
        """模型少给几段时补空段，逐段写文案阶段会补齐，不能直接少几段就开跑。"""
        plan = longform.build_plan(
            band=longform.LENGTH_BANDS["medium"],
            target_seconds=720,
            outline_payload=[{"title": "只有一��"}],
        )
        assert len(plan.outline) == longform.plan_section_count(720)

    def test_string_entries_accepted(self):
        plan = longform.build_plan(
            band=longform.LENGTH_BANDS["medium"],
            target_seconds=720,
            outline_payload=["甲", "乙", "丙"],
        )
        assert plan.outline[0].title == "甲"

    def test_garbage_payload_yields_empty_outline(self):
        plan = longform.build_plan(
            band=longform.LENGTH_BANDS["medium"],
            target_seconds=720,
            outline_payload="not json at all",
        )
        assert plan.outline == []

    def test_parse_json_block_handles_code_fence(self):
        assert longform.parse_json_block('```json\n[{"a": 1}]\n```') == [{"a": 1}]

    def test_parse_json_block_handles_think_block(self):
        raw = '<think>let me think about it</think>[{"title": "x"}]'
        assert longform.parse_json_block(raw) == [{"title": "x"}]

    def test_parse_json_block_handles_prose_around_json(self):
        raw = 'Here is the outline:\n[{"title": "x"}]\nHope that helps!'
        assert longform.parse_json_block(raw) == [{"title": "x"}]

    def test_parse_json_block_returns_none_on_garbage(self):
        assert longform.parse_json_block("no json whatsoever") is None
        assert longform.parse_json_block("") is None
        assert longform.parse_json_block(None) is None

    def test_nested_section_terms(self):
        buckets = section_materials.coerce_section_terms(
            [["a", "b"], ["c"]], 2
        )
        assert buckets == [["a", "b"], ["c"]]

    def test_flat_section_terms_are_spread(self):
        """一维返回是常见失败模式：不能把 20 个关键词全塞进第一段。"""
        buckets = section_materials.coerce_section_terms(
            ["a", "b", "c", "d"], 2
        )
        assert len(buckets) == 2
        assert sum(len(bucket) for bucket in buckets) == 4
        assert all(bucket for bucket in buckets)

    def test_section_terms_padded_to_count(self):
        buckets = section_materials.coerce_section_terms([["a"]], 3)
        assert len(buckets) == 3
        assert buckets[0] == ["a"]

    def test_flatten_terms_dedupes_and_keeps_order(self):
        """顺序即最终时间线上素材出现的顺序，重复词会让下载器重复拉同一批。"""
        sections = [
            Section(index=1, terms=["a", "b"]),
            Section(index=2, terms=["b", "c"]),
        ]
        assert section_materials.flatten_terms(sections) == ["a", "b", "c"]


class TestClipBudget:
    def _sections(self, durations):
        return [Section(index=i + 1, duration=d) for i, d in enumerate(durations)]

    def test_budget_matches_total_duration(self):
        sections = self._sections([60, 30, 90, 120])
        budget = section_materials.clip_budget(sections, 300, clip_duration=5)
        assert sum(budget) == 60

    def test_longer_sections_get_more_clips(self):
        """平均分配会让长段落反复回放同一批素材。"""
        sections = self._sections([30, 270])
        budget = section_materials.clip_budget(sections, 300, clip_duration=5)
        assert budget[1] > budget[0] * 5

    def test_long_section_is_not_starved_by_the_cap(self):
        """上限必须足够高，否则一个 5 分钟的段落会拿不到够铺满的镜头。
        这条曾经真的失败过：上限 24 让 270 秒的段落只拿到 24 个镜头。"""
        sections = self._sections([30, 270])
        budget = section_materials.clip_budget(sections, 300, clip_duration=5)
        # 270 秒、5 秒一镜需要 54 个。
        assert budget[1] >= 270 // 5

    def test_every_section_gets_a_floor(self):
        """低于 3 个镜头观众能明显感到画面重复。"""
        sections = self._sections([5, 5, 5, 5])
        budget = section_materials.clip_budget(sections, 20, clip_duration=5)
        assert all(count >= section_materials.MIN_CLIPS_PER_SECTION for count in budget)

    def test_budget_is_capped(self):
        sections = self._sections([900, 900])
        budget = section_materials.clip_budget(sections, 1800, clip_duration=1)
        assert all(
            count <= section_materials.MAX_CLIPS_PER_SECTION for count in budget
        )

    def test_zero_durations_still_produce_a_usable_budget(self):
        """时长回填失败时不能让整条视频卡在素材阶段。"""
        sections = self._sections([0, 0, 0])
        budget = section_materials.clip_budget(sections, 0, clip_duration=5)
        assert len(budget) == 3
        assert all(count >= section_materials.MIN_CLIPS_PER_SECTION for count in budget)

    def test_empty_sections(self):
        assert section_materials.clip_budget([], 300, clip_duration=5) == []


class TestScriptAssembly:
    def test_sections_joined_with_blank_lines(self):
        """段落分隔必须和短视频路径一致，否则字幕逐段匹配会整段失败。"""
        sections = [
            Section(index=1, text="第一段。"),
            Section(index=2, text="第二段。"),
            Section(index=3, text="第三段。"),
        ]
        assert longform.render_sections_as_script(sections) == (
            "第一段。\n\n第二段。\n\n第三段。"
        )

    def test_empty_sections_are_skipped(self):
        sections = [
            Section(index=1, text="有内容。"),
            Section(index=2, text="   "),
            Section(index=3, text="也有内容。"),
        ]
        assert longform.render_sections_as_script(sections) == "有内容。\n\n也有内容。"
