"""幻灯片规划的回归测试。

覆盖三类真实长视频里暴露过的问题：素材阶段把浮点数当列表下标、没有分段时
用词数当时长，以及最要紧的——每段只出一张卡，于是一张静态图要顶一百秒。
最后一类只能靠"卡片密度"和"卡片时长"的断言守住，肉眼看成片太慢。
"""

import pytest
from html.parser import HTMLParser
from types import SimpleNamespace

from app.services import longform, task
from app.services.longform import Section, SectionPlan


class _VisibleText(HTMLParser):
    """收集幻灯片里观众真正读到的文字。

    ``slide_html`` 的页面家具（缩略标签、页码、进度条、A/B 栏标）在任何卡片上
    都存在，把它们算进"有内容"会让空白卡也通过断言，所以这里按 class 跳过。
    """

    FURNITURE = {"topbar", "kicker", "counter", "progress", "accent", "label"}

    def __init__(self):
        super().__init__()
        self._stack: list[bool] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        classes = set(str(dict(attrs).get("class") or "").split())
        inherited = self._stack[-1] if self._stack else False
        self._stack.append(
            inherited or tag == "style" or bool(classes & self.FURNITURE)
        )

    def handle_endtag(self, tag):
        if self._stack:
            self._stack.pop()

    def handle_data(self, data):
        if not (self._stack and self._stack[-1]) and data.strip():
            self.chunks.append(data.strip())


def _visible_text(html_text: str) -> str:
    parser = _VisibleText()
    parser.feed(html_text)
    return " ".join(parser.chunks)


def _plan(sections):
    return SectionPlan(
        band=longform.LENGTH_BANDS["medium"],
        target_seconds=600,
        language="en",
        outline=sections,
    )


def _params(**overrides):
    values = {
        "video_subject": "Ancient concrete",
        "video_language": "en",
        "video_aspect": "16:9",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_compare_card_keeps_content_by_splitting_bullets():
    """``compare`` 卡的内容在 ``bullets`` 里时要拆到两栏，而不是渲染空框。

    真实跑里模型给的正是这种形状：4 条要点、``left``/``right`` 为空。渲染层
    只读 ``left``/``right``，于是屏幕上只剩 "A" / "B" 两个标签。
    """
    from app.services import slide_source

    slides = slide_source.coerce_slides(
        [
            {
                "section": 1,
                "kind": "compare",
                "bullets": [
                    "Relies on steel rebar",
                    "Uses volcanic ash binders",
                    "Cracks under chemical stress",
                    "Strengthens over time",
                ],
            }
        ]
    )

    assert len(slides) == 1
    slide = slides[0]
    assert slide.kind == "compare"
    left, right = slide_source._compare_panels(slide)
    assert left == ["Relies on steel rebar", "Uses volcanic ash binders"]
    assert right == ["Cracks under chemical stress", "Strengthens over time"]


def test_typed_card_without_its_field_downgrades_to_bullets():
    """拿不到版式主字段的卡降级成要点卡，而不是留一张空白卡。"""
    from app.services import slide_source

    slides = slide_source.coerce_slides(
        [
            {"section": 1, "kind": "stat", "stat_caption": "caption only"},
            {"section": 1, "kind": "quote", "attribution": "attribution only"},
            {"section": 1, "kind": "compare", "left": "one side only"},
        ]
    )

    # 前两张没有任何正文，直接丢弃；第三张降级后文字还在。
    assert len(slides) == 1
    assert slides[0].kind == "section"
    assert slides[0].bullets == ["one side only"]


def test_every_planned_card_renders_readable_content():
    """混合版式的卡片渲染出来都必须看得见正文。

    断言直接读渲染后的 HTML：字段检查会漏掉"字段有值但版式不读它"这一类，
    而那正是空白卡片的来源。
    """
    from app.services import slide_source

    slides = slide_source.coerce_slides(
        [
            {"section": 1, "kind": "section", "title": "Chapter", "bullets": ["One fact"]},
            {"section": 1, "kind": "stat", "stat": "50 years", "bullets": ["unused"]},
            {"section": 1, "kind": "stat", "stat_caption": "no number here"},
            {"section": 1, "kind": "compare", "bullets": ["Steel rebar", "Ash binders"]},
            {"section": 1, "kind": "quote", "quote": "Narration claim", "attribution": "Pliny"},
            {"section": 1, "kind": "section", "title": "Title only"},
        ]
    )

    assert len(slides) == 5
    for slide in slides:
        text = _visible_text(slide_source.slide_html(slide, "16:9"))
        assert text.strip(), f"{slide.kind} card rendered without readable content"
    # 数字卡只剩数字时也是正文，不能被当成空白卡丢掉。
    assert any(slide.stat == "50 years" for slide in slides)


def test_section_durations_are_attached_with_integer_indices(monkeypatch):
    """每张卡必须归属正确的段，且该段所有卡的时长之和等于段时长。

    旧实现用 ``cursor = 0.0`` 累加后再 ``slides[cursor]``，第一次循环就抛
    ``TypeError: list indices must be integers or slices, not float``，整条
    长视频在素材阶段失败。现在下标直接来自 ``enumerate`` 与 ``section.index``，
    同时按卡片张数均分该段时长。
    """
    from app.services import slide_source

    sections = [
        Section(
            index=1,
            title="One",
            text="First sentence here. Second sentence follows. Third one closes.",
            duration=30.0,
        ),
        Section(
            index=2,
            title="Two",
            text="Another sentence. And one more for the second section.",
            duration=15.0,
        ),
    ]
    monkeypatch.setattr(
        task.llm,
        "generate_slide_plan",
        lambda **_kwargs: [
            {"section": 1, "kind": "section", "title": "One", "bullets": ["first point"]},
            {"section": 1, "kind": "stat", "stat": "2,000 years", "stat_caption": "of service"},
            {"section": 2, "kind": "section", "title": "Two", "bullets": ["second point"]},
        ],
    )

    slides = task._plan_slide_content(
        "task-id", _params(), "First section text. Second section text.", _plan(sections)
    )

    for section in sections:
        owned = [slide for slide in slides if slide.section_index == section.index]
        assert owned, f"section {section.index} produced no cards"
        assert sum(slide.duration for slide in owned) == pytest.approx(section.duration)
        assert max(slide.duration for slide in owned) <= (
            slide_source.TARGET_CARD_SECONDS + 0.01
        )
    # 卡片顺序必须跟段顺序一致，否则画面会跑到讲述前面。
    assert [slide.section_index for slide in slides] == [1, 1, 2]


def test_slide_plan_tops_up_to_target_density(monkeypatch):
    """模型只给一张时，兜底规划要把每段补到约 15 秒一张的密度。"""
    from app.services import slide_source

    narrative = " ".join(
        [
            "The harbor walls survived two millennia of salt and waves.",
            "Volcanic ash reacted with lime to form a durable binder.",
            "Modern concrete relies on inert ingredients instead.",
            "Roman engineers mixed their mortar while it was still hot.",
            "The sea itself supplied the chemicals that healed cracks.",
            "That mechanism is now the blueprint for greener concrete.",
        ]
    )
    sections = [Section(index=1, title="Section", text=narrative, duration=90.0)]
    monkeypatch.setattr(
        task.llm,
        "generate_slide_plan",
        lambda **_kwargs: [{"section": 1, "kind": "section", "title": "Section"}],
    )

    slides = task._plan_slide_content(
        "task-id", _params(), narrative, _plan(sections)
    )

    expected = slide_source.cards_for_duration(90.0)
    assert len(slides) == expected
    assert sum(slide.duration for slide in slides) == pytest.approx(90.0)
    assert all(slide.duration <= slide_source.TARGET_CARD_SECONDS + 0.01 for slide in slides)


def test_short_script_without_sections_keeps_single_card(monkeypatch):
    """短视频配幻灯片源保持原行为：一张卡，没有独立时长。"""
    monkeypatch.setattr(task.llm, "generate_slide_plan", lambda **_kwargs: [])

    slides = task._plan_slide_content(
        "task-id", _params(), " ".join(["word"] * 200), None
    )

    assert len(slides) == 1
    assert slides[0].duration == 0
    assert slides[0].section_index == 1


def test_short_script_without_sections_estimates_target_from_words(monkeypatch):
    """没有分段时按词数反推时长，而不是把词数当成秒数。

    200 词的脚本按 2.5 词/秒约 80 秒；旧实现会得到 200 秒，幻灯片停留时间
    跟着翻倍。
    """
    captured = {}

    def _fake_plan_slides(sections, *, target_seconds, per_section=1):
        captured["target_seconds"] = target_seconds
        return []

    monkeypatch.setattr(task.llm, "generate_slide_plan", lambda **_kwargs: [])
    monkeypatch.setattr(task.slide_source, "plan_slides", _fake_plan_slides)

    task._plan_slide_content(
        "task-id", _params(), " ".join(["word"] * 200), None
    )

    assert captured["target_seconds"] == 80


def _write_slide_png(path):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (24, 32, 48)).save(path)
    return str(path)


def test_slides_are_converted_into_playable_video_clips(tmp_path):
    """幻灯片必须变成真正的视频片段。

    这是第三次真实长视频跑挂的那一步：``get_slide_materials`` 把 PNG 路径
    直接交给 ``combine_videos``，MoviePy 把它当视频探测，抛
    ``Error passing `ffmpeg -i` command output``，整条任务在最后一阶段失败。
    截图本身是好的，缺的只是图片→视频的转换。
    """
    from moviepy import VideoFileClip

    from app.services import slide_source

    png = _write_slide_png(tmp_path / "slides" / "slide-001.png")

    clips = slide_source.slides_to_video_clips(
        [png], output_dir=str(tmp_path), video_aspect="16:9", clip_seconds=1.0
    )

    assert len(clips) == 1
    assert clips[0].endswith(".mp4")
    clip = VideoFileClip(clips[0])
    try:
        assert abs(clip.duration - 1.0) < 0.4
        assert list(clip.size) == [1920, 1080]
    finally:
        clip.close()


def test_slide_to_video_conversion_is_resumable(tmp_path, monkeypatch):
    """已经转换过的片段直接复用，续跑不必重编整批幻灯片。"""
    from app.services import slide_source

    png = _write_slide_png(tmp_path / "slides" / "slide-001.png")
    first = slide_source.slides_to_video_clips(
        [png], output_dir=str(tmp_path), clip_seconds=1.0
    )
    assert first

    def _explode(*_args, **_kwargs):
        raise AssertionError("existing clip must be reused")

    monkeypatch.setattr(slide_source.subprocess, "run", _explode)
    again = slide_source.slides_to_video_clips(
        [png], output_dir=str(tmp_path), clip_seconds=1.0
    )

    assert again == first


def test_slides_to_video_clips_uses_card_durations_and_owners(tmp_path):
    """每张卡的片段长度等于它覆盖的口播时长，并带出分段归属。

    这是长视频"一张图停 99 秒"的直接修复点：片段若仍按 5 秒渲染，拼接阶段
    只会反复循环同一张图；归属若丢失，几十张卡就分不进各自的段。
    """
    from moviepy import VideoFileClip

    from app.services import slide_source

    first = _write_slide_png(tmp_path / "slides" / "slide-001.png")
    second = _write_slide_png(tmp_path / "slides" / "slide-002.png")

    clips = slide_source.slides_to_video_clips(
        [first, second],
        output_dir=str(tmp_path),
        video_aspect="16:9",
        clip_seconds=5.0,
        durations=[2.0, 3.0],
        section_indices=[1, 2],
    )

    assert len(clips) == 2
    assert clips.section_owners == [1, 2]
    assert "-002.0s.mp4" in clips[0]
    assert "-003.0s.mp4" in clips[1]
    for path, expected in zip(clips, (2.0, 3.0)):
        clip = VideoFileClip(path)
        try:
            assert abs(clip.duration - expected) < 0.4
        finally:
            clip.close()


def test_zero_duration_falls_back_to_configured_clip_length(tmp_path):
    """``duration=0`` 表示"没有独立时长"（短视频路径），退回固定片段长度。"""
    from app.services import slide_source

    png = _write_slide_png(tmp_path / "slides" / "slide-001.png")

    clips = slide_source.slides_to_video_clips(
        [png],
        output_dir=str(tmp_path),
        clip_seconds=1.0,
        durations=[0.0],
        section_indices=[1],
    )

    assert len(clips) == 1
    assert "-001.0s.mp4" in clips[0]
    assert clips.section_owners == [1]


def test_material_split_follows_card_ownership():
    """按卡的段归属分组，而不是"前 N 张给前 N 段"。

    旧切分把第 3 段之后的卡全塞给最后一段，长视频中后段因此没有自己的画面。
    """
    from app.services import longform_pipeline

    sections = [
        Section(index=1, title="One", duration=60.0),
        Section(index=2, title="Two", duration=60.0),
        Section(index=3, title="Three", duration=60.0),
    ]
    buckets = longform_pipeline.assign_materials_to_sections(
        _plan(sections),
        ["a.mp4", "b.mp4", "c.mp4", "d.mp4"],
        total_seconds=180.0,
        clip_duration=5,
        section_owners=[1, 2, 2, 3],
    )

    assert buckets == [["a.mp4"], ["b.mp4", "c.mp4"], ["d.mp4"]]


def test_material_split_fills_section_without_owned_cards():
    """某段没有归属素材时复用最多的一段，而不是留一段纯字幕画面。"""
    from app.services import longform_pipeline

    sections = [
        Section(index=1, title="One", duration=60.0),
        Section(index=2, title="Two", duration=60.0),
    ]
    buckets = longform_pipeline.assign_materials_to_sections(
        _plan(sections),
        ["a.mp4", "b.mp4"],
        total_seconds=120.0,
        clip_duration=5,
        section_owners=[1, 1],
    )

    assert buckets[0] == ["a.mp4", "b.mp4"]
    assert buckets[1] == ["a.mp4", "b.mp4"]


def test_longform_final_video_passes_slide_owners(monkeypatch):
    """阶段 6 必须把幻灯片的段归属透传给素材分桶。"""
    from app.services import longform_pipeline, slide_source, task

    captured = {}

    def _spy(plan, videos, total_seconds, clip_duration, section_owners=None):
        captured["owners"] = section_owners
        captured["videos"] = list(videos)
        return [[] for _ in plan.outline]

    monkeypatch.setattr(longform_pipeline, "assign_materials_to_sections", _spy)
    monkeypatch.setattr(task, "_final_video_output_path", lambda *_a: "final.mp4")
    monkeypatch.setattr(
        task.section_render, "render_sections", lambda **_kwargs: None
    )

    sections = [Section(index=1, title="One", duration=30.0)]
    materials = slide_source.SlideClipList(["a.mp4", "b.mp4"], section_owners=[1, 1])

    result = task.generate_longform_final_video(
        "task-id",
        _params(
            video_clip_duration=5,
            match_materials_to_script=True,
            video_concat_mode="sequential",
        ),
        _plan(sections),
        materials,
        "audio.mp3",
        "subtitle.srt",
        30.0,
    )

    assert captured["owners"] == [1, 1]
    assert captured["videos"] == ["a.mp4", "b.mp4"]
    assert result == ([], [], [])


def test_max_clip_seconds_keeps_whole_slide_cards():
    """幻灯片片段的切片上限必须抬到卡片最大时长，否则一张 15 秒的卡只播 5 秒。"""
    from app.services import section_render, slide_source

    slides = _params(video_source="slides", video_clip_duration=5)
    local = _params(video_source="local", video_clip_duration=4)

    assert section_render._max_clip_seconds(slides) == int(
        slide_source.MAX_CARD_SECONDS
    )
    assert section_render._max_clip_seconds(local) == 4


def test_get_slide_materials_returns_video_clips(tmp_path, monkeypatch):
    """素材阶段对外交付的必须是视频片段，不是 PNG。"""
    from app.services import slide_source

    png = _write_slide_png(tmp_path / "slides" / "slide-001.png")
    monkeypatch.setattr(task, "_plan_slide_content", lambda *_a, **_k: [object()])
    monkeypatch.setattr(task.utils, "task_dir", lambda _task_id: str(tmp_path))
    monkeypatch.setattr(
        slide_source, "render_slides", lambda *_a, **_k: [png]
    )

    clips = task.get_slide_materials(
        "task-id",
        _params(video_clip_duration=1),
        "A short script.",
    )

    assert clips
    assert all(clip.endswith(".mp4") for clip in clips)
