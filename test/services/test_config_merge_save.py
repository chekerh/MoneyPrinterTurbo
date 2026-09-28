"""保存配置时必须保留别人对 config.toml 的修改。

真实事故：config.toml 会被仍在运行的旧进程用陈旧快照整体覆盖。起因是
``save_config()`` 直接序列化导入时读到的 ``_cfg``——一个长期驻留内存的快照。
任何在该进程启动之后对文件的手工修改（改 API key、加新键、切换视频源），
都会在下一次界面保存时无声消失，文件里还会连带着少掉那些旧进程根本
不认识的键。

这里建立的不变量：

* **只写自己真正改动过的键。** 保存时重新读盘，把内存相对基线的差异叠加到
  磁盘内容上，其余部分原样保留。
* **认得的新键不丢。** 旧进程没听说过的键必须原封不动留下。
* **自己删掉的键要真的删掉。** 合并不能退化成"只增不减"。
* **冲突时以内存为准并留下痕迹。** 界面里点下的修改优先，但必须记录，
  否则用户会眼睁睁看着自己刚设的值被换掉而毫无线索。
* **文件损坏时不阻塞服务。** 读不出来就退回原来的整份写入。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import toml

from app.config import config as config_module


class _ConfigSandbox:
    """把 config 模块的落盘路径与基线都指向临时文件，避免污染真实配置。"""

    def __init__(self, initial: str):
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.path = root / "config.toml"
        self.path.write_text(initial, encoding="utf-8")
        self._patches = [
            patch.object(config_module, "root_dir", str(root)),
            patch.object(config_module, "config_file", str(self.path)),
            patch.object(config_module, "_cfg", {}),
        ]
        self._originals = None

    def __enter__(self):
        self._originals = (
            dict(config_module._cfg),
            {name: dict(getattr(config_module, name))
             for name in _SECTION_NAMES},
        )
        for p in self._patches:
            p.start()
        config_module._cfg.clear()
        for name in _SECTION_NAMES:
            getattr(config_module, name).clear()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        config_module._cfg.clear()
        config_module._cfg.update(self._originals[0])
        for name, values in self._originals[1].items():
            section = getattr(config_module, name)
            section.clear()
            section.update(values)
        self._dir.cleanup()
        return False

    def read(self) -> dict:
        return toml.loads(self.path.read_text(encoding="utf-8"))

    def rewrite(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def set_baseline(self) -> None:
        """
        模拟"进程启动时读到这份文件"：内容进内存，同时记为保存基线。
        只记基线而不载入内存是不真实的——真实进程两者都有。
        """
        loaded = self.read()
        config_module._cfg.clear()
        config_module._cfg.update(loaded)
        for name in _SECTION_NAMES:
            section = getattr(config_module, name)
            section.clear()
            section.update(loaded.get(name, {}) or {})
        config_module._remember_saved_payload(loaded)


_SECTION_NAMES = (
    "app", "azure", "siliconflow", "minimax_tts", "elevenlabs",
    "chatterbox", "fish_audio", "ui",
)


class TestSavePreservesExternalEdits(unittest.TestCase):
    def test_operator_added_key_is_not_dropped(self):
        """旧进程不认识的键必须留下——这是这次事故最直接的损失。"""
        with _ConfigSandbox('log_level = "DEBUG"\n') as box:
            box.set_baseline()
            # 模拟别的进程/操作者在文件里加了一个键
            box.rewrite('log_level = "DEBUG"\nbrand_new_key = "keep me"\n')

            config_module.save_config()

            self.assertEqual(box.read().get("brand_new_key"), "keep me")

    def test_operator_changed_value_survives_an_unrelated_save(self):
        """我们没有改过的键，操作者改过的值不能被快照覆盖回去。"""
        with _ConfigSandbox('log_level = "DEBUG"\nui_mode = "old"\n') as box:
            box.set_baseline()
            box.rewrite('log_level = "DEBUG"\nui_mode = "operator choice"\n')

            # 这次保存与 ui_mode 毫无关系
            config_module._cfg["unrelated"] = "x"
            config_module.save_config()

            self.assertEqual(box.read()["ui_mode"], "operator choice")

    def test_our_own_change_is_still_applied(self):
        """合并不能以"保留别人改动"为代价把自己的改动丢掉。"""
        with _ConfigSandbox('log_level = "DEBUG"\n') as box:
            box.set_baseline()

            config_module._cfg["listen_port"] = 9999
            config_module.save_config()

            self.assertEqual(box.read()["listen_port"], 9999)

    def test_our_own_deletion_is_honoured(self):
        """合并不能退化成只增不减：我们删掉的键要真的消失。"""
        with _ConfigSandbox('log_level = "DEBUG"\nstale_key = "v"\n') as box:
            box.set_baseline()

            config_module._cfg.pop("stale_key")
            config_module.save_config()

            self.assertNotIn("stale_key", box.read())

    def test_section_key_edits_merge_without_dropping_siblings(self):
        """改一个 section 里的键，不能把这个 section 的其他键弄丢。"""
        with _ConfigSandbox('[app]\nvideo_source = "pexels"\napi_key = ""\n') as box:
            box.set_baseline()
            box.rewrite('[app]\nvideo_source = "pixabay"\napi_key = "operator"\n')

            config_module.app["match_materials_to_script"] = True
            config_module.save_config()

            saved = box.read()["app"]
            self.assertEqual(saved["video_source"], "pixabay")
            self.assertEqual(saved["api_key"], "operator")
            self.assertIs(saved["match_materials_to_script"], True)

    def test_conflict_prefers_memory_and_records_it(self):
        """
        同一个键两边都改了：界面里点下的算，但必须留下痕迹，
        否则用户只会看到自己刚设的值莫名消失。
        """
        with _ConfigSandbox('ui_mode = "baseline"\n') as box:
            box.set_baseline()
            box.rewrite('ui_mode = "from disk"\n')

            config_module._cfg["ui_mode"] = "from ui"
            with patch.object(config_module, "logger") as fake_logger:
                config_module.save_config()

            self.assertEqual(box.read()["ui_mode"], "from ui")
            warnings = [
                " ".join(str(part) for part in call.args)
                for call in fake_logger.warning.call_args_list
            ]
            self.assertTrue(
                any("ui_mode" in message for message in warnings),
                f"冲突必须点名到具体的键，实际日志: {warnings}",
            )

    def test_repeated_saves_do_not_accumulate_garbage(self):
        """连续保存不应把文件写坏或无限膨胀。"""
        with _ConfigSandbox('log_level = "DEBUG"\n') as box:
            box.set_baseline()
            for index in range(5):
                config_module._cfg[f"round_{index}"] = index
                config_module.save_config()

            saved = box.read()
            for index in range(5):
                self.assertEqual(saved[f"round_{index}"], index)

    def test_unparseable_file_falls_back_to_full_write(self):
        """文件损坏时不能卡死服务：退回整份写入，让用户能重新配置。"""
        with _ConfigSandbox('log_level = "DEBUG"\n') as box:
            box.set_baseline()
            box.rewrite("this is [not valid toml\n")

            config_module._cfg["listen_port"] = 1234
            config_module.save_config()

            self.assertEqual(box.read()["listen_port"], 1234)

    def test_in_memory_state_absorbs_external_edits(self):
        """
        保存后内存要吸收磁盘上的新值。

        否则下一次保存的基线仍然是旧的，又会把操作者的修改当成"我们自己
        改的"写回去，问题只是被推迟了一次。
        """
        with _ConfigSandbox('log_level = "DEBUG"\n') as box:
            box.set_baseline()
            box.rewrite('log_level = "DEBUG"\nexternal = "v1"\n')

            config_module._cfg["mine"] = "m"
            config_module.save_config()

            self.assertEqual(config_module._cfg.get("external"), "v1")

            # 第二次保存不应把 external 退回或删除
            box.rewrite('log_level = "DEBUG"\nexternal = "v2"\n')
            config_module._cfg["mine"] = "m2"
            config_module.save_config()

            self.assertEqual(box.read()["external"], "v2")
            self.assertEqual(box.read()["mine"], "m2")


if __name__ == "__main__":
    unittest.main()
