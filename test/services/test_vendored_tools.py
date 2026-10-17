"""``tools/`` 在仓库里有两份副本，WebUI 用的是 app 根下那一份。

``webui/Main.py`` 的 ``tools_dir`` 解析顺序是 ``<app_root>/tools`` 优先，只有它
不存在时才回退到上一级目录。而 app 根下的 ``tools/`` 是存在的，所以**回退永远
不会发生**——上一级那份只被 shell 流水线使用。

两份副本**不是**逐字节相同的：vendored 那一份是 app 内相对路径变体（例如
``youtube_uploader.py`` 的 ``PROFILE_ROOT`` 不带 ``MoneyPrinterTurbo/`` 那一层，
``plans.py`` 也改成了显式解析作品集根）。所以这里只守住真正的风险——某个脚本
只在一边存在，或者某一边的路径基准是错的——而不是去要求两者内容一致。

后果很具体：``publish_video.py`` 一开始只加在根目录，vendored 副本缺失，于是
``_plan_uploader_tool()`` 拿到一个不存在的路径，WebUI 的发布按钮只会报一句
"uploader produced no output"。
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT
VENDORED_TOOLS = APP_ROOT / "tools"
ROOT_TOOLS = REPO_ROOT.parent / "tools"

#: WebUI/调度流程会按名字取用的工具脚本。少任何一个都是运行期才炸。
REQUIRED_TOOLS = (
    "plans.py",
    "archive_video.py",
    "publish_video.py",
    "setup_youtube.py",
    "setup_upload.py",
)


def _load_module(path: Path):
    """按文件路径加载一个工具脚本，取出它的常量。"""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass  # 带 argparse 的脚本在 import 时会 SystemExit，常量已经定义好了
    return module


class TestVendoredTools(unittest.TestCase):
    def test_vendored_tools_dir_exists(self):
        self.assertTrue(
            VENDORED_TOOLS.is_dir(),
            f"WebUI 依赖 {VENDORED_TOOLS}，它必须随仓库一起提供",
        )

    def test_required_tools_are_vendored(self):
        missing = [
            name for name in REQUIRED_TOOLS if not (VENDORED_TOOLS / name).is_file()
        ]
        self.assertEqual(
            missing,
            [],
            f"这些脚本被 WebUI/调度按 tools_dir 取用，却没有 vendored 副本: {missing}",
        )

    def test_vendored_config_path_resolves(self):
        """vendored 的 setup_youtube.py 必须指向**存在的** config.toml。

        它曾经和仓库根那份用了完全相同的相对路径，于是从 app 目录算起会指向
        ``<app>/MoneyPrinterTurbo/config.toml``——一个不存在的嵌套目录。
        路径基准不同，代码就必须不同，所以这一条单独守住。
        """
        module = _load_module(VENDORED_TOOLS / "setup_youtube.py")
        resolved = os.path.realpath(module.CONFIG_PATH)
        self.assertTrue(
            os.path.isfile(resolved),
            f"vendored setup_youtube.py 指向了不存在的配置: {resolved}",
        )
        self.assertEqual(
            os.path.realpath(resolved),
            os.path.realpath(APP_ROOT / "config.toml"),
        )

    def test_root_config_path_resolves(self):
        """根目录那份同理，指向 app 根下的 config.toml。"""
        if not ROOT_TOOLS.is_dir():
            self.skipTest("没有上一级 tools/ 目录（干净克隆）")
        module = _load_module(ROOT_TOOLS / "setup_youtube.py")
        resolved = os.path.realpath(module.CONFIG_PATH)
        self.assertTrue(
            os.path.isfile(resolved),
            f"根目录 setup_youtube.py 指向了不存在的配置: {resolved}",
        )

    def test_no_stray_tool_only_in_one_place(self):
        """反向检查：任何只在单边出现的 .py 都要被看见。"""
        root_names = {p.name for p in ROOT_TOOLS.glob("*.py")} if ROOT_TOOLS.is_dir() else set()
        vendored_names = {p.name for p in VENDORED_TOOLS.glob("*.py")}
        only_root = sorted(root_names - vendored_names)
        only_vendored = sorted(vendored_names - root_names)
        self.assertEqual(only_root, [], f"只在仓库根 tools/ 里有: {only_root}")
        self.assertEqual(only_vendored, [], f"只在 app tools/ 里有: {only_vendored}")

    def test_publish_video_tool_is_runnable(self):
        """真正跑一次发布调用：语法错、import 错、参数拼错都会在这里暴露。

        不能只跑 ``--help``：argparse 在 ``--help`` 上先退出，所以 ``from
        app.services import publishing`` 那一行根本不会执行。路径基准写错导致的
        ``ModuleNotFoundError: No module named 'app'`` 只有真跑一次才看得见——
        上一版测试就因为只测 ``--help`` 而放过了它。
        """
        for base in (VENDORED_TOOLS, ROOT_TOOLS):
            if not base.is_dir():
                continue
            tool = base / "publish_video.py"
            with self.subTest(location=base.name if base == ROOT_TOOLS else "app"):
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(tool),
                        "--channel",
                        "test-niche",
                        "--video",
                        "/nonexistent/clip.mp4",
                        "--title",
                        "T",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                combined = proc.stdout + proc.stderr
                self.assertNotIn(
                    "ModuleNotFoundError", combined, f"app 包定位失败: {combined[-400:]}"
                )
                self.assertNotIn("Traceback", combined, combined[-400:])
                # 未连接的 niche 走的是正常失败路径：stdout 必须是可解析的 JSON，
                # shell 流水线与 WebUI 都按 `"success": true` 解析最后一行。
                lines = [x for x in proc.stdout.strip().splitlines() if x.strip()]
                self.assertTrue(lines, f"stdout 为空: {combined[-400:]}")
                try:
                    payload = json.loads(lines[-1])
                except ValueError as exc:
                    self.fail(f"stdout 最后一行不是 JSON ({exc}): {lines[-1][:300]}")
                self.assertIn("success", payload)

    def test_publish_video_help(self):
        tool = VENDORED_TOOLS / "publish_video.py"
        proc = subprocess.run(
            [sys.executable, str(tool), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        self.assertIn("--channel", proc.stdout)
        self.assertIn("--video", proc.stdout)


if __name__ == "__main__":
    unittest.main()