"""任务状态落盘：重启之后必须还在。

任务状态此前只存在内存里，于是进程一重启，**整张任务历史连同每条错误信息全部
消失**。用户看到的不是"失败"，而是一条状态含糊的旧任务——``failed_stage`` 与
``error`` 是排障时唯一能说明"到底卡在哪一步、为什么"的东西；异步 API 调用方
拿到的 ``task_id`` 则彻底查不到状态，既不能确认成功也不能确认失败。

落盘本身也可能变成新的故障源：原地覆写被 kill 会留下半截 JSON，比没有文件更糟
（下次启动再也加载不出来）。所以这几条用例盯的正是"写得稳、读得回、坏得起"。
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import const
from app.services import task_store
from app.services.state import MemoryState

# app.services.state.state 是 import 时创建的全局单例，早于用例的隔离 fixture
# 就绪；这里一律用局部实例，避免碰到开发者真实的状态文件。


class _Store:
    """把 task_store 的路径换到临时目录。"""

    def __init__(self, tmp: Path):
        self.path = tmp / "state" / "tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._patcher = patch.object(task_store, "state_file_path", lambda: self.path)

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, *_args):
        self._patcher.stop()
        return False


class TestTaskStatePersistence(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_survives_a_restart(self):
        """重启后任务历史必须还在。"""
        with _Store(self.tmp):
            first = MemoryState(persist=True)
            first.update_task("t1", state=const.TASK_STATE_COMPLETE, progress=100)

            # 模拟重启：全新的实例从磁盘读回
            second = MemoryState(persist=True)
            restored = second.get_task("t1")

        self.assertIsNotNone(restored, "重启之后任务历史全丢了")
        self.assertEqual(restored["state"], const.TASK_STATE_COMPLETE)
        self.assertEqual(restored["progress"], 100)

    def test_failure_reason_survives_a_restart(self):
        """
        失败原因必须落盘。

        ``failed_stage`` 与 ``error`` 是排障时唯一能说明"卡在哪一步、为什么"的
        信息，它们此前只活在内存里，重启即消失。
        """
        with _Store(self.tmp):
            first = MemoryState(persist=True)
            first.update_task(
                "t2",
                state=const.TASK_STATE_FAILED,
                progress=40,
                failed_stage="tts",
                error="ElevenLabs rejected the key",
            )
            second = MemoryState(persist=True)
            restored = second.get_task("t2")

        self.assertEqual(restored["failed_stage"], "tts")
        self.assertEqual(restored["error"], "ElevenLabs rejected the key")
        self.assertEqual(restored["progress"], 40, "失败前进度不该被清零")

    def test_patch_and_delete_are_also_persisted(self):
        with _Store(self.tmp):
            first = MemoryState(persist=True)
            first.update_task("t3", state=const.TASK_STATE_COMPLETE, progress=100)
            first.patch_task("t3", published=True)

            second = MemoryState(persist=True)
            self.assertTrue(second.get_task("t3")["published"])

            second.delete_task("t3")
            third = MemoryState(persist=True)
            self.assertIsNone(third.get_task("t3"), "删除没有落盘，重启后又回来了")

    def test_unserialisable_values_do_not_break_the_write(self):
        """
        任务记录里可能混进不可序列化的对象。

        状态是辅助信息，为它让生成流程崩掉是本末倒置。
        """
        with _Store(self.tmp):
            state = MemoryState(persist=True)
            state.update_task("t4", state=const.TASK_STATE_FAILED, error=ValueError("boom"))
            reloaded = MemoryState(persist=True).get_task("t4")

        self.assertIsNotNone(reloaded)
        self.assertIn("boom", str(reloaded["error"]))

    def test_corrupt_file_degrades_instead_of_crashing(self):
        """
        坏文件必须降级。

        为了状态文件让整个应用起不来是本末倒置；而且坏文件要改名留档，否则它会
        留在原地、每次启动都失败一次。
        """
        store_path = self.tmp / "state" / "tasks.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text("{not json at all", encoding="utf-8")

        with _Store(self.tmp):
            self.assertEqual(task_store.load_tasks(), {})
            self.assertTrue(store_path.with_suffix(".corrupt").is_file())

        state = MemoryState(persist=True)
        self.assertEqual(state.get_all_tasks(page=1, page_size=10), ([], 0))

    def test_write_is_atomic(self):
        """
        写入必须先落临时文件再改名。

        原地覆写在进程被 kill 的瞬间会留下半截 JSON，而那比没有文件更糟：下次
        启动读到坏文件，整个状态就再也加载不出来了。
        """
        with _Store(self.tmp):
            state = MemoryState(persist=True)
            state.update_task("t5", state=const.TASK_STATE_COMPLETE, progress=100)
            self.assertEqual(
                [p.name for p in (self.tmp / "state").iterdir()],
                ["tasks.json"],
                "写完之后仍留下了临时文件，说明不是先写临时文件再改名",
            )
            payload = json.loads((self.tmp / "state" / "tasks.json").read_text())
        self.assertIn("t5", payload["tasks"])

    def test_persist_false_never_touches_disk(self):
        with _Store(self.tmp):
            MemoryState(persist=False).update_task("t6", progress=10)
            self.assertFalse((self.tmp / "state" / "tasks.json").exists())

    def test_history_is_capped(self):
        """无节制增长会让状态文件无限变大；只保留最近的即可。"""
        with _Store(self.tmp):
            state = MemoryState(persist=True)
            for index in range(task_store._MAX_RECORDS + 25):
                state.update_task(f"t{index}", progress=1)
            reloaded = MemoryState(persist=True)
            total = reloaded.get_all_tasks(page=1, page_size=10_000)[1]
        self.assertLessEqual(total, task_store._MAX_RECORDS)

    def test_a_long_running_task_is_not_the_one_evicted(self):
        """
        容量上限淘汰的必须是"最久没动"的，而不是"最早创建"的。

        ``dict`` 对已有键重新赋值不会改变插入位置，所以一条跑了几分钟、进度一直在
        变的任务，在只按创建顺序截断的写法下会随着新任务涌入而被优先淘汰——它恰恰
        是最不该丢的那条。上限本身有测试看着（``test_history_is_capped`` 只断言总数
        ≤ 上限），但"淘汰谁"没有任何测试看得见。

        这里的时序是刻意的：先让长跑任务成为**最近更新**的那条，再制造溢出。若在
        制造溢出的过程中反复 patch 它，落盘层的合并会把被淘汰的条目重新追加到末尾，
        断言就变成恒真——那条自愈行为掩盖的正是"某一次落盘当场写丢了它"。
        """
        with _Store(self.tmp):
            state = MemoryState(persist=True)
            long_running = "long-running"
            state.update_task(long_running, progress=1)
            for index in range(task_store._MAX_RECORDS - 1):
                state.update_task(f"t{index}", progress=1)

            # 它刚刚还在更新，因此此刻它是最"新"的一条
            state.patch_task(long_running, progress=42)
            for index in range(task_store._MAX_RECORDS, task_store._MAX_RECORDS + 25):
                state.update_task(f"t{index}", progress=1)

            payload = json.loads((self.tmp / "state" / "tasks.json").read_text())
        self.assertIn(
            long_running,
            payload["tasks"],
            "刚更新过的长跑任务被上限淘汰了；截断按创建顺序而不是最近更新",
        )

    def test_two_writers_do_not_erase_each_others_history(self):
        """
        两个进程各写各的时，谁都不能把对方的任务从磁盘上抹掉。

        本应用同时跑 WebUI 与 API 两个进程，各自持有一份 ``MemoryState``，而状态
        文件只有一份。若落盘时直接拿本进程的内存快照整份覆写，API 进程的一次进度
        更新就会把文件重写成它启动时读到的旧内容，WebUI 那边的历史与每条 ``error``
        全部消失——正是本模块存在的理由所要防的故障。
        """
        with _Store(self.tmp):
            # 两个进程几乎同时启动，各自只在启动时读一次盘，之后内存就分叉了。
            # 顺序很关键：必须先都构造完再各自写。若在 WebUI 写完之后才构造 API，
            # API 会把 WebUI 的记录一起读进自己的内存，于是这条用例在整份覆写的
            # 旧实现下也能通过——它测的是一个现实中不存在的启动顺序。
            webui = MemoryState(persist=True)
            api = MemoryState(persist=True)

            webui.update_task("webui-1", progress=50, error="webui 的错误")
            webui.update_task("webui-2", progress=100)
            api.update_task("api-1", progress=10)

            payload = json.loads((self.tmp / "state" / "tasks.json").read_text())
            self.assertIn("webui-1", payload["tasks"], "API 写入抹掉了 WebUI 的任务")
            self.assertIn("webui-2", payload["tasks"], "API 写入抹掉了 WebUI 的任务")
            self.assertIn("api-1", payload["tasks"])

            # 重启之后两边的历史都还在，错误信息也在
            reloaded = MemoryState(persist=True)
            self.assertEqual(reloaded.get_task("webui-1")["error"], "webui 的错误")
            self.assertIsNotNone(reloaded.get_task("webui-2"))
            self.assertIsNotNone(reloaded.get_task("api-1"))

    def test_deletion_does_not_depend_on_having_never_seen_the_task(self):
        """
        删除必须靠显式集合传达，不能靠"本进程内存里没有"来推断。

        合并落盘意味着磁盘上会存在本进程没见过的任务（属于另一个进程）。若删除
        只把自己那份内存快照写回去，那些任务会因为"不在快照里"而被静默删除。
        """
        with _Store(self.tmp):
            other = MemoryState(persist=True)
            other.update_task("other-1", progress=10)

            mine = MemoryState(persist=True)
            mine.update_task("mine-1", progress=10)
            mine.delete_task("mine-1")

            payload = json.loads((self.tmp / "state" / "tasks.json").read_text())
            self.assertNotIn("mine-1", payload["tasks"], "删除没有真正生效")
            self.assertIn("other-1", payload["tasks"], "删除误伤了别的进程的任务")

    def test_a_corrupt_file_does_not_take_down_the_new_state(self):
        """坏文件降级为留档，同时新进程仍能写出一份可读的状态。"""
        path = self.tmp / "state" / "tasks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        with _Store(self.tmp):
            state = MemoryState(persist=True)
            self.assertEqual(state.get_all_tasks(page=1, page_size=10), ([], 0))
            state.update_task("t-new", progress=1)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("t-new", payload["tasks"])


if __name__ == "__main__":
    unittest.main()
