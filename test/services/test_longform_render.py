"""分段渲染的端到端冒烟测试：用合成媒体跑通真实 ffmpeg 链路。

不调 LLM、不连素材库、不需要任何 API key：素材、旁白和字幕都用 ffmpeg
现场合成，因此这个测试只验证"分段渲染 + 拼接"这一步本身是否正确——而这正是
长视频最容易坏、又最难靠单元测试发现的地方（分段拼接出来的时间轴对不上、
某段字幕整体错位、拼接后音画不同步，这些症状都要真的跑一遍才看得见）。
"""

import os
import subprocess
import tempfile

import pytest

from app.services import section_render
from app.services.longform import Section
from app.utils import utils

pytestmark = pytest.mark.skipif(
    not utils.check_ffmpeg_ready(), reason="ffmpeg is not available"
)


def _ffmpeg(*args):
    result = subprocess.run(
        [utils.get_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-400:]}")
    return result


def _make_clip(path, color, seconds=3):
    _ffmpeg(
        "-f", "lavfi",
        "-i", f"color=c={color}:s=320x180:r=10:d={seconds}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        path,
    )
    return path


def _make_audio(path, seconds=6, freq=440):
    _ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency={freq}:duration={seconds}",
        "-c:a", "libmp3lame",
        "-q:a", "5",
        path,
    )
    return path


def _make_srt(path, entries):
    lines = []
    for index, (start, end, text) in enumerate(entries, start=1):
        lines.append(str(index))
        lines.append(f"{section_render._format_srt_time(start)} --> "
                     f"{section_render._format_srt_time(end)}")
        lines.append(text)
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


class _Params:
    """只提供 ``_render_one_section`` 会读到的字段。"""

    video_aspect = "16:9"
    video_concat_mode = None
    video_transition_mode = None
    video_clip_duration = 3
    video_clip_speed = 1.0
    n_threads = 2


class TestSectionRenderEndToEnd:
    def test_three_sections_concatenate_into_one_playable_video(self, tmp_path):
        task_dir = str(tmp_path / "task")
        os.makedirs(task_dir, exist_ok=True)

        materials = [
            _make_clip(str(tmp_path / f"clip{i}.mp4"), color, seconds=4)
            for i, color in enumerate(["red", "green", "blue", "yellow", "cyan", "magenta"])
        ]
        audio = _make_audio(str(tmp_path / "narration.mp3"), seconds=9)

        sections = [
            Section(index=1, title="一", duration=3.0),
            Section(index=2, title="二", duration=3.0),
            Section(index=3, title="三", duration=3.0),
        ]
        # 每段两块素材，够 combine_videos 铺满 3 秒。
        buckets = [materials[0:2], materials[2:4], materials[4:6]]

        subtitle = _make_srt(
            str(tmp_path / "sub.srt"),
            [
                (0.0, 2.5, "第一段字幕"),
                (3.0, 5.5, "第二段字幕"),
                (6.0, 8.5, "第三段字幕"),
            ],
        )

        output = section_render.render_sections(
            task_id="longform-smoke",
            params=_Params(),
            sections=sections,
            section_materials=buckets,
            audio_file=audio,
            subtitle_path=subtitle,
        )

        assert output, "section render produced no output"
        assert os.path.exists(output)
        assert os.path.getsize(output) > 1024

        meta = section_render.shorts_cutter.probe_video(
            utils.get_ffmpeg_binary(), output
        )
        # 三段各 3 秒。容忍容器时间基造成的亚秒误差，但整体时长必须落在
        # 合理范围内——明显偏短说明某段没进成片，明显偏长说明拼接重复了。
        assert 7.0 <= meta.duration <= 11.0, f"unexpected duration {meta.duration}"

    def test_failed_section_aborts_instead_of_shipping_a_gap(self, tmp_path):
        """某段没有素材时必须整体失败：拼出一个中间缺几秒的成片，
        比明确失败难查得多。"""
        task_dir = str(tmp_path / "task2")
        os.makedirs(task_dir, exist_ok=True)

        materials = [_make_clip(str(tmp_path / f"m{i}.mp4"), "red", seconds=4) for i in range(3)]
        audio = _make_audio(str(tmp_path / "a.mp3"), seconds=6)

        sections = [
            Section(index=1, title="一", duration=3.0),
            Section(index=2, title="二", duration=3.0),
        ]
        # 第二段没有素材。
        buckets = [materials[0:2], []]

        output = section_render.render_sections(
            task_id="longform-smoke-gap",
            params=_Params(),
            sections=sections,
            section_materials=buckets,
            audio_file=audio,
            subtitle_path="",
        )
        assert output is None

    def test_completed_sections_are_reused_on_rerun(self, tmp_path):
        """分段渲染的存在理由就是可中断：重跑时不该把已完成的段重渲一遍。"""
        task_dir = str(tmp_path / "task3")
        os.makedirs(task_dir, exist_ok=True)

        materials = [_make_clip(str(tmp_path / f"r{i}.mp4"), "green", seconds=4) for i in range(4)]
        audio = _make_audio(str(tmp_path / "r.mp3"), seconds=6)

        sections = [
            Section(index=1, title="一", duration=3.0),
            Section(index=2, title="二", duration=3.0),
        ]
        buckets = [materials[0:2], materials[2:4]]

        kwargs = dict(
            task_id="longform-smoke-resume",
            params=_Params(),
            sections=sections,
            section_materials=buckets,
            audio_file=audio,
            subtitle_path="",
        )
        assert section_render.render_sections(**kwargs)

        render_dir = os.path.join(str(tmp_path / "task3"), "sections")
        first = os.path.join(render_dir, "section-001.mp4")
        stamp = os.path.getmtime(first)

        # 记录内容指纹，确认重跑后仍然是同一个文件（没有被重写）。
        before = open(first, "rb").read()

        # 故意把第二段的成片删掉：只应该重渲第二段。
        second = os.path.join(render_dir, "section-002.mp4")
        os.remove(second)

        assert section_render.render_sections(**kwargs)
        assert open(first, "rb").read() == before
        assert os.path.exists(second)

    def test_truncated_section_file_is_rerendered(self, tmp_path):
        """上一次崩在写文件中途会留下残片。只判"文件存在"就会把它当成完成，
        成片会在那一段突然黑屏。"""
        render_dir = tmp_path / "sections"
        render_dir.mkdir()
        stub = render_dir / "section-001.mp4"
        stub.write_bytes(b"\x00" * 512)

        assert section_render._is_complete(str(stub), 3.0) is False

    def test_zero_duration_section_aborts(self, tmp_path):
        """时长回填失败时不能猜一个值继续渲染。"""
        audio = _make_audio(str(tmp_path / "z.mp3"), seconds=3)
        output = section_render.render_sections(
            task_id="longform-smoke-zero",
            params=_Params(),
            sections=[Section(index=1, title="一", duration=0.0)],
            section_materials=[[]],
            audio_file=audio,
            subtitle_path="",
        )
        assert output is None


class TestSubtitleWindowing:
    def test_entries_are_rebased_into_the_section(self, tmp_path):
        entries = [
            section_render.SrtEntry(0.0, 2.0, "a"),
            section_render.SrtEntry(2.0, 4.0, "b"),
            section_render.SrtEntry(10.0, 12.0, "c"),
        ]
        window = section_render.slice_entries(entries, 10.0, 12.0)
        assert len(window) == 1
        assert window[0].start == 0.0
        assert window[0].end == 2.0
        assert window[0].text == "c"

    def test_entries_spanning_the_boundary_are_clipped(self, tmp_path):
        entries = [section_render.SrtEntry(8.0, 14.0, "straddle")]
        window = section_render.slice_entries(entries, 10.0, 12.0)
        assert window[0].start == 0.0
        assert window[0].end == 2.0

    def test_sub_second_slivers_are_dropped(self, tmp_path):
        """切在一条长字幕开头时可能只留下几十毫秒，渲染出来是一帧闪烁。"""
        entries = [section_render.SrtEntry(0.0, 10.0, "long")]
        window = section_render.slice_entries(entries, 9.99, 10.02)
        assert window == []
