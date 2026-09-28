import ast
import os
import re
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
TASK_HISTORY_HELPERS = {
    "_find_final_task_video",
    "_is_settled_video_file",
    "_probe_video_dimensions",
    "_build_video_download_name",
    "_build_restore_upload_requirements",
    "_get_unmet_restore_upload_requirements",
}
TASK_HISTORY_CONSTANTS = {
    "_FINAL_VIDEO_PATTERN",
    "_DOWNLOAD_FILENAME_INVALID_PATTERN",
    "_MEDIA_PROBE_TIMEOUT_SECONDS",
    "_VIDEO_SETTLE_SECONDS",
    "_VIDEO_DIMENSIONS_CACHE",
    "VOICE_MODE_TTS",
    "VOICE_MODE_UPLOAD",
    "VOICE_MODE_NONE",
}


class _NullLogger:
    def debug(self, *args, **kwargs):
        pass


def _load_task_history_helpers():
    """
    从 WebUI 入口中隔离加载不依赖 Streamlit 的任务历史纯函数。

    直接导入 Main.py 会执行整套页面渲染。测试只编译目标常量和函数，既验证
    合并后的真实实现，也避免为了单元测试重新拆出一个只有少量函数的生产模块。
    """
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in TASK_HISTORY_CONSTANTS
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.AnnAssign) and (
            isinstance(node.target, ast.Name)
            and node.target.id in TASK_HISTORY_CONSTANTS
        ):
            # 带类型注解的常量（例如 ``_VIDEO_DIMENSIONS_CACHE: dict = {}``）
            # 同样是 Assign 之外的另一种节点形态，漏掉会让引用它的函数 NameError。
            selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in TASK_HISTORY_HELPERS:
            selected_nodes.append(node)

    namespace = {
        "os": os,
        "re": re,
        "time": time,
        "subprocess": subprocess,
        "Mapping": Mapping,
        "logger": _NullLogger(),
    }
    module = ast.fix_missing_locations(ast.Module(body=selected_nodes, type_ignores=[]))
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


TASK_HISTORY_NAMESPACE = _load_task_history_helpers()
find_final_task_video = TASK_HISTORY_NAMESPACE["_find_final_task_video"]
build_video_download_name = TASK_HISTORY_NAMESPACE["_build_video_download_name"]
build_restore_upload_requirements = TASK_HISTORY_NAMESPACE[
    "_build_restore_upload_requirements"
]
get_unmet_restore_upload_requirements = TASK_HISTORY_NAMESPACE[
    "_get_unmet_restore_upload_requirements"
]


def _write_settled_video(path: Path, payload: bytes = b"video-bytes"):
    """写出一个「已写完」的成片：内容占位，但 mtime 回退到一小时前。"""
    path.write_bytes(payload)
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def _stub_ffprobe(monkeypatch, stdout: str):
    """让隔离命名空间里的 ``_probe_video_dimensions`` 走固定结果。

    直接替换 ``subprocess.run`` 而不是替换 ``_probe_video_dimensions`` 本身，
    这样尺寸解析和缓存仍然跑真实实现，只有 ffprobe 这一层外部依赖被打桩。
    """
    completed = subprocess.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=stdout, stderr=""
    )
    monkeypatch.setattr(
        TASK_HISTORY_NAMESPACE["subprocess"], "run", lambda *a, **k: completed
    )
    TASK_HISTORY_NAMESPACE["_VIDEO_DIMENSIONS_CACHE"].clear()


def _readable_video(monkeypatch):
    _stub_ffprobe(monkeypatch, "1080x1920\n")


def _unreadable_video(monkeypatch):
    _stub_ffprobe(monkeypatch, "")


def test_find_final_task_video_ignores_intermediate_files(tmp_path, monkeypatch):
    """任务历史只能把 final 成片识别为完成，不能使用合成中间文件。"""
    _readable_video(monkeypatch)
    for file_name in (
        "combined-1.mp4",
        "temp-clip-1.mp4",
        "final-1TEMP_MPY_wvf_snd.mp4",
    ):
        (tmp_path / file_name).touch()

    assert find_final_task_video(str(tmp_path)) == ""


def test_find_final_task_video_returns_first_numbered_output(tmp_path, monkeypatch):
    """多成片任务与运行时结果保持一致，默认播放序号最小的最终视频。"""
    _readable_video(monkeypatch)
    _write_settled_video(tmp_path / "final-10.mp4")
    _write_settled_video(tmp_path / "final-2.mp4")
    _write_settled_video(tmp_path / "final-1.mp4")

    assert find_final_task_video(str(tmp_path)) == str(tmp_path / "final-1.mp4")


def test_find_final_task_video_ignores_unreadable_final(tmp_path, monkeypatch):
    """ffprobe 解析不出尺寸的成片不能算完成。

    归档链路过去只看文件名，于是 0 字节或被截断的 final-1.mp4 会被当成成片
    复制进媒体库；这里锁定「解析不出尺寸就当作还在写」。
    """
    _write_settled_video(tmp_path / "final-1.mp4")
    _unreadable_video(monkeypatch)

    assert find_final_task_video(str(tmp_path)) == ""


def test_find_final_task_video_ignores_just_written_final(tmp_path, monkeypatch):
    """刚落盘的成片要等一个静默窗口之后才算完成。

    任务面板每 2 秒轮询一次；ffmpeg 刚创建目标文件时若立刻被判为完成，归档就会
    复制到半截文件。mtime 仍是「刚刚」的文件必须继续被视为未完成。
    """
    _readable_video(monkeypatch)
    (tmp_path / "final-1.mp4").write_bytes(b"video-bytes")  # mtime = now

    assert find_final_task_video(str(tmp_path)) == ""


def test_build_video_download_name_uses_subject_and_output_index():
    assert (
        build_video_download_name("A day: in / Shanghai?", 2, 3)
        == "A day in Shanghai-2.mp4"
    )


def test_build_video_download_name_handles_empty_and_long_subjects():
    assert build_video_download_name("  ...  ", 1, 1) == "video.mp4"
    assert len(build_video_download_name("a" * 100, 1, 1)) == 84


def test_restore_requirements_block_missing_uploaded_files():
    params = {
        "video_source": "local",
        "custom_audio_file": "/old-task/custom-audio.wav",
        "voice_name": "zh-CN-XiaoxiaoNeural-Female",
    }
    requirements = build_restore_upload_requirements(params)

    assert get_unmet_restore_upload_requirements(
        requirements,
        video_source="local",
        voice_name=params["voice_name"],
        has_local_materials=False,
        has_custom_audio=False,
    ) == {"local_materials", "custom_audio"}


def test_restore_requirements_allow_explicit_replacements():
    requirements = build_restore_upload_requirements(
        {
            "video_source": "local",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    assert not get_unmet_restore_upload_requirements(
        requirements,
        video_source="pexels",
        voice_name="en-US-JennyNeural-Female",
        has_local_materials=False,
        has_custom_audio=False,
    )


def test_restore_requirements_require_file_in_upload_voice_mode():
    """恢复上传配音任务时，继续使用上传模式必须重新选择音频文件。"""
    requirements = build_restore_upload_requirements(
        {
            "video_source": "pexels",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    assert get_unmet_restore_upload_requirements(
        requirements,
        video_source="pexels",
        voice_name="zh-CN-XiaoxiaoNeural-Female",
        has_local_materials=False,
        has_custom_audio=False,
        voice_mode="upload",
    ) == {"custom_audio"}


def test_restore_requirements_allow_replacing_upload_with_other_voice_modes():
    """用户主动切换到自动配音或无配音时，不再强制恢复历史上传文件。"""
    requirements = build_restore_upload_requirements(
        {
            "video_source": "pexels",
            "custom_audio_file": "/old-task/custom-audio.wav",
            "voice_name": "zh-CN-XiaoxiaoNeural-Female",
        }
    )

    for voice_mode in ("tts", "none"):
        assert not get_unmet_restore_upload_requirements(
            requirements,
            video_source="pexels",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
            has_local_materials=False,
            has_custom_audio=False,
            voice_mode=voice_mode,
        )
