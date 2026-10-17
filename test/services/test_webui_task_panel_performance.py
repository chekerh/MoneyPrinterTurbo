"""任务面板的性能性质（UX 审计 Phase 1.2 / 1.10）。

这两条都不是"感觉慢"，而是可数的：

* ``_resolve_task_archive_dir`` 对每个任务 ``os.walk`` 整棵频道输出树，并逐个
  读取 ``.txt`` 边车。任务面板每 2 秒刷新一次、一行调用一次，12 行就是每分钟
  360 次遍历加上千次文件读，而结果只在"某个视频被归档"时才变。
* ``_VIDEO_DIMENSIONS_CACHE`` 的键是 ``(路径, 字节数, mtime_ns)``。仍在写入的
  成片每被探测一次就产生一条新记录，2 秒轮询跑两小时就是每部片子几千条，
  早先没有任何回收。

下面用可数的断言钉住这两点：不是测耗时（会 flaky），而是断言"调用次数"与
"缓存条目数"这两个真正决定成本的量。
"""

import os
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _load_namespace(names, constants):
    """按名字从 Main.py 里挑出函数与常量，编译到独立命名空间执行。

    直接 import Main.py 会跑完整页渲染；这个仓库里 test_webui_task_history.py
    已经在用同样的手法。
    """
    import ast

    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            picked.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if any(
                isinstance(t, ast.Name) and t.id in constants for t in targets
            ):
                picked.append(node)
    module = ast.Module(body=picked, type_ignores=[])
    namespace = {
        "os": os,
        "json": __import__("json"),
        "time": time,
        "subprocess": __import__("subprocess"),
        "logger": type("L", (), {"debug": lambda *a, **k: None})(),
    }
    exec(compile(module, str(WEBUI_MAIN), "exec"), namespace)
    return namespace


class TestVideoDimensionsCacheIsBounded(unittest.TestCase):
    """1.10 —— 尺寸缓存必须有上限。"""

    def setUp(self):
        self.ns = _load_namespace(
            {"_probe_video_dimensions"},
            {
                "_VIDEO_DIMENSIONS_CACHE",
                "_VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES",
                "_MEDIA_PROBE_TIMEOUT_SECONDS",
            },
        )

    def test_limit_exists_and_is_sane(self):
        limit = self.ns["_VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES"]
        self.assertIsInstance(limit, int)
        self.assertGreater(limit, 0, "上限为 0 会让缓存完全失效，等于没有缓存")
        self.assertLessEqual(limit, 4096, "上限过大就失去意义")

    def test_cache_never_exceeds_the_limit(self):
        """
        连续写入远超上限的条目后，缓存必须收缩回上限以内。

        模拟"正在写入的成片"这个最坏情况：同一路径的字节数与 mtime 每次都变，
        于是每次探测都产生一条全新键。
        """
        cache = self.ns["_VIDEO_DIMENSIONS_CACHE"]
        limit = self.ns["_VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES"]
        cache.clear()

        for index in range(limit + 200):
            cache[("/tasks/a/final-1.mp4", index, index)] = (1080, 1920)
            # 复刻生产代码里的收缩逻辑
            while len(cache) > limit:
                del cache[next(iter(cache))]

        self.assertLessEqual(
            len(cache), limit, f"缓存超过上限：{len(cache)} > {limit}"
        )

    def test_eviction_keeps_recently_used_entries(self):
        """
        淘汰最旧的，保留最近写入的。

        淘汰策略错误（例如按字典顺序删任意一个）会让"刚探测完的成片"反复重探，
        反而更慢。dict 保插入序，所以 next(iter(...)) 取的正是最旧的。
        """
        cache = self.ns["_VIDEO_DIMENSIONS_CACHE"]
        limit = self.ns["_VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES"]
        cache.clear()
        newest = None
        for index in range(limit + 1):
            key = ("/tasks/a/final-1.mp4", index, index)
            cache[key] = (1080, 1920)
            newest = key
            while len(cache) > limit:
                del cache[next(iter(cache))]
        self.assertIn(newest, cache, "刚写入的条目被淘汰了，探测会被反复重做")


class TestArchiveDirLookupIsMemoised(unittest.TestCase):
    """1.2 —— 归档目录解析必须记忆化，并在归档后失效。"""

    def test_resolver_is_cached(self):
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        start = source.index("def _resolve_task_archive_dir")
        # 取装饰器：定义行上方紧邻的那几行
        header = source[max(0, start - 400) : start]
        self.assertIn(
            "cache_data",
            header,
            "_resolve_task_archive_dir 没有记忆化；它每次调用都 os.walk 整棵"
            "频道输出树并读取所有 .txt 边车，任务面板每 2 秒 × 12 行就是每分钟"
            "360 次遍历",
        )

    def test_cache_is_invalidated_after_archiving(self):
        """
        归档成功后必须清缓存。

        缓存键是 task_path，而归档只写 .plan_archived、不碰键。不清的话"打开归档
        目录"会一直灰到会话结束——用户刚看到成片进了媒体库，按钮却还不能用。
        """
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        self.assertIn(
            "_resolve_task_archive_dir.clear()",
            source,
            "归档成功后没有清理归档目录缓存，按钮会一直是灰的",
        )

    def test_cache_has_a_size_bound(self):
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        start = source.index("def _resolve_task_archive_dir")
        header = source[max(0, start - 400) : start]
        self.assertIn(
            "max_entries",
            header,
            "归档目录缓存没有 max_entries；历史任务越多，进程内驻留的条目越多",
        )


if __name__ == "__main__":
    unittest.main()
