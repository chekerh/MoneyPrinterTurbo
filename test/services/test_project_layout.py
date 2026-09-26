"""验证 ProjectLayout 只在显式配置下解析出彼此隔离的根目录。

布局契约的目标是消除隐式路径猜测：应用根、存储根和作品集根必须来自明确的
输入，任何一项缺失、相对化、符号链接化或互相嵌套都必须失败关闭，而不是回退
到某个"看起来对"的默认值。
"""

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.services.project_layout import (
    ProjectLayout,
    ProjectLayoutError,
    resolve_project_layout,
    same_filesystem,
)


class TestResolveProjectLayout(unittest.TestCase):
    """显式配置解析出的根目录必须彼此隔离且可预测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "MoneyPrinterTurbo"
        self.app_root.mkdir()
        self.portfolio = self.base / "portfolio"
        self.portfolio.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_derives_feature_roots_from_the_two_configured_roots(self):
        """
        只有两个根需要配置：应用根与作品集根。其余根必须由它们推导，
        避免各处代码各自拼路径。
        """
        layout = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )

        self.assertEqual(layout.application_root, self.app_root)
        self.assertEqual(layout.storage_root, self.app_root / "storage")
        self.assertEqual(layout.thumbnail_root, self.app_root / "storage" / "thumbnails")
        self.assertEqual(
            layout.descriptor_root, self.app_root / "storage" / "result_descriptors"
        )
        self.assertEqual(layout.portfolio_root, self.portfolio)
        self.assertEqual(layout.plans_root, self.portfolio / "plans")
        self.assertEqual(layout.outputs_root, self.portfolio / "outputs")

    def test_resolved_layout_is_frozen_and_hashable(self):
        """
        布局对象在进程内共享，若可被就地修改则各处的隔离判断会失效。
        """
        layout = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )

        self.assertIsInstance(layout, ProjectLayout)
        with self.assertRaises(Exception):
            layout.storage_root = Path("/tmp/elsewhere")
        self.assertEqual(len({layout, layout}), 1)

    def test_resolution_is_deterministic(self):
        """同样的输入必须得到同样的布局，便于测试和缓存。"""
        first = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )
        second = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )

        self.assertEqual(first, second)

    def test_reads_portfolio_root_from_environment_when_not_passed(self):
        """
        作品集根可以由 MPT_PORTFOLIO_ROOT 提供，但只能显式提供；
        不允许从应用根推导。
        """
        with patch.dict(os.environ, {"MPT_PORTFOLIO_ROOT": str(self.portfolio)}):
            layout = resolve_project_layout(application_root=self.app_root)

        self.assertEqual(layout.portfolio_root, self.portfolio)

    def test_missing_portfolio_root_fails_closed(self):
        """
        缺少作品集根时必须报错。静默回退到应用根会让产物写进错误的树。
        """
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProjectLayoutError):
                resolve_project_layout(application_root=self.app_root)

    def test_reads_portfolio_root_from_the_app_config_file(self):
        """
        config.toml 是本项目既有的配置面，作品集根应当可以写在那里。
        环境变量优先级更高，用于单次运行的临时覆盖。
        """
        with patch.dict(os.environ, {}, clear=True):
            with patch("app.config.config.app", {"portfolio_root": str(self.portfolio)}):
                layout = resolve_project_layout(application_root=self.app_root)

        self.assertEqual(layout.portfolio_root, self.portfolio)

    def test_environment_variable_overrides_the_config_file(self):
        """单次运行的临时覆盖应当胜过持久化配置。"""
        elsewhere = self.base / "elsewhere-root"
        elsewhere.mkdir()

        with patch.dict(
            os.environ, {"MPT_PORTFOLIO_ROOT": str(elsewhere)}
        ):
            with patch("app.config.config.app", {"portfolio_root": str(self.portfolio)}):
                layout = resolve_project_layout(application_root=self.app_root)

        self.assertEqual(layout.portfolio_root, elsewhere)

    def test_blank_config_value_falls_through_to_a_clear_failure(self):
        """
        配置里留空字符串等同于"未配置"。不能因此静默回退到应用根。
        """
        with patch.dict(os.environ, {}, clear=True):
            with patch("app.config.config.app", {"portfolio_root": ""}):
                with self.assertRaises(ProjectLayoutError):
                    resolve_project_layout(application_root=self.app_root)

    def test_relative_roots_are_rejected(self):
        """
        相对路径依赖进程工作目录，部署方式一变就会指向别处。
        """
        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=Path("MoneyPrinterTurbo"),
                portfolio_root=self.portfolio,
            )

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=Path("portfolio"),
            )

    def test_blank_portfolio_root_is_rejected(self):
        """空白字符串不是有效配置，不能被当作"未设置"以外的含义。"""
        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root="   ",
            )

    def test_existing_application_root_is_required(self):
        """
        应用根必须真实存在，否则后续写入会落到一个凭空创建的空目录。
        """
        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.base / "does-not-exist",
                portfolio_root=self.portfolio,
            )

    def test_roots_may_not_be_the_same_directory(self):
        """两个根相同会让隔离判断失去意义。"""
        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=self.app_root,
            )

    def test_portfolio_root_nested_inside_application_root_is_rejected(self):
        """
        作品集落在应用树内会与静态挂载和清理逻辑相互干扰。
        """
        nested = self.app_root / "portfolio"
        nested.mkdir()

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=nested,
            )

    def test_application_root_nested_inside_portfolio_root_is_rejected(self):
        """反向嵌套同样会造成清理范围重叠。"""
        inner = self.portfolio / "app"
        inner.mkdir()

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=inner,
                portfolio_root=self.portfolio,
            )

    def test_symlinked_root_is_rejected(self):
        """
        符号链接根会让越界检查跟随链接到预期之外的目录。
        """
        linked = self.base / "linked-portfolio"
        linked.symlink_to(self.portfolio, target_is_directory=True)

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=linked,
            )

    def test_symlinked_ancestor_is_rejected(self):
        """祖先目录被替换成链接时，根路径本身看起来是正常的。"""
        real = self.base / "real"
        (real / "portfolio").mkdir(parents=True)
        link_parent = self.base / "link-parent"
        link_parent.symlink_to(real, target_is_directory=True)

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=link_parent / "portfolio",
            )

    def test_missing_feature_roots_do_not_block_resolution(self):
        """
        storage/plans 等目录可以尚未创建；解析只做校验，不做创建。
        写入方按需创建，避免解析阶段产生副作用。
        """
        layout = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )

        self.assertFalse(layout.thumbnail_root.exists())
        self.assertFalse(layout.plans_root.exists())
        self.assertTrue(layout.application_root.exists())

    def test_error_message_names_the_offending_field(self):
        """报错必须指出是哪个配置项，否则排障要靠猜。"""
        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=Path("relative"),
            )

        self.assertIn("portfolio_root", str(ctx.exception))

    def test_dotdot_in_configured_root_cannot_defeat_containment(self):
        """
        未归一化的 ".." 会让 Path.relative_to 的前缀比较失效：
        plans_root 实际落在 application_root 之内，隔离判断却通过。
        这正是本模块唯一要守住的约束，必须显式拒绝。
        """
        sneaky = self.base / "sneaky" / ".." / "app" / "sub"
        sneaky.parent.mkdir(parents=True)
        sneaky.mkdir()

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=sneaky,
            )

    def test_dotdot_in_application_root_cannot_defeat_containment(self):
        """
        反向同理：应用根带 ".." 时它实际落在作品集树内，
        但按成分比较的前缀判断会漏掉这层包含关系。
        """
        (self.base / "hop").mkdir()
        inner_app = self.portfolio / "app"
        inner_app.mkdir()
        sneaky = self.base / "hop" / ".." / "portfolio" / "app"

        with self.assertRaises(ProjectLayoutError):
            resolve_project_layout(
                application_root=sneaky,
                portfolio_root=self.portfolio,
            )

    def test_single_dot_segment_is_normalized_by_pathlib(self):
        """
        pathlib 自己就会折叠单点成分，所以 "." 不构成绕过。
        这里把它钉成一条认知：真正需要拒绝的是 ".."。
        """
        self.assertEqual(Path("/a/./b").parts, ("/", "a", "b"))

        layout = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.base / "portfolio",
        )
        self.assertEqual(layout.portfolio_root, self.portfolio)

    def test_sibling_prefix_directory_is_not_treated_as_nested(self):
        """
        "/base/portfolio" 与 "/base/portfolio-old" 只是字符串前缀相近，
        并不是包含关系。经典的字符串前缀 bug 会在这里误判。
        """
        sibling = self.base / "portfolio-old"
        sibling.mkdir()

        layout = resolve_project_layout(
            application_root=sibling,
            portfolio_root=self.portfolio,
        )

        self.assertEqual(layout.plans_root, self.portfolio / "plans")
        self.assertNotEqual(
            self.portfolio, layout.plans_root.parent.parent
        )

    def test_symlinked_storage_root_is_rejected(self):
        """
        storage 是推导出来的根。如果它本身是指向作品集树的软链，
        新产物就会写进 plans 旁边。这一层不能因为"是推导出来的"就免检。
        """
        (self.portfolio / "outputs").mkdir()
        (self.app_root / "storage").symlink_to(
            self.portfolio / "outputs", target_is_directory=True
        )

        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=self.portfolio,
            )

        self.assertIn("storage_root", str(ctx.exception))

    def test_symlinked_plans_root_is_rejected(self):
        """plans 是推导出来的根，被软链到树外同样必须失败关闭。"""
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (self.portfolio / "plans").symlink_to(elsewhere, target_is_directory=True)

        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=self.portfolio,
            )

        self.assertIn("plans_root", str(ctx.exception))

    def test_symlinked_thumbnail_root_is_rejected(self):
        """thumbnails 目录被软链出去会让新产物暴露在公开挂载下。"""
        elsewhere = self.base / "elsewhere2"
        elsewhere.mkdir()
        (self.app_root / "storage").mkdir()
        (self.app_root / "storage" / "thumbnails").symlink_to(
            elsewhere, target_is_directory=True
        )

        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=self.portfolio,
            )

        self.assertIn("thumbnail_root", str(ctx.exception))

    def test_non_symlinked_derived_roots_are_accepted(self):
        """真实存在的普通目录不应被上面的检查误伤。"""
        (self.app_root / "storage" / "thumbnails").mkdir(parents=True)
        (self.app_root / "storage" / "result_descriptors").mkdir()
        (self.portfolio / "plans").mkdir()
        (self.portfolio / "outputs").mkdir()

        layout = resolve_project_layout(
            application_root=self.app_root,
            portfolio_root=self.portfolio,
        )

        self.assertTrue(layout.thumbnail_root.is_dir())
        self.assertTrue(layout.plans_root.is_dir())

    def test_tilde_paths_are_rejected(self):
        """
        "~" 会被展开成当前用户的家目录，属于隐式猜测，必须显式写全。
        """
        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root="~/portfolio",
            )

        self.assertIn("portfolio_root", str(ctx.exception))

    def test_blank_application_root_is_rejected(self):
        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root="  ",
                portfolio_root=self.portfolio,
            )

        self.assertIn("application_root", str(ctx.exception))

    def test_existing_file_is_not_a_valid_portfolio_root(self):
        """指向一个普通文件时必须失败，不能把它当成目录根。"""
        not_a_dir = self.base / "plain-file.txt"
        not_a_dir.write_text("x")

        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=self.app_root,
                portfolio_root=not_a_dir,
            )

        self.assertIn("portfolio_root", str(ctx.exception))

    def test_symlinked_application_root_is_rejected(self):
        linked_app = self.base / "linked-app"
        linked_app.symlink_to(self.app_root, target_is_directory=True)

        with self.assertRaises(ProjectLayoutError) as ctx:
            resolve_project_layout(
                application_root=linked_app,
                portfolio_root=self.portfolio,
            )

        self.assertIn("application_root", str(ctx.exception))

    def test_default_application_root_falls_back_to_repo_root(self):
        """
        不传应用根时使用既有的 root_dir()，这是全仓库唯一的应用根定义。
        """
        with patch.dict(os.environ, {"MPT_PORTFOLIO_ROOT": str(self.portfolio)}):
            layout = resolve_project_layout()

        from app.utils.utils import root_dir

        self.assertEqual(layout.application_root, Path(root_dir()))


class TestSameFilesystemEdgeCases(unittest.TestCase):
    """同盘判定在异常输入下必须返回 False 而不是抛错。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.existing = self.base / "existing"
        self.existing.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_inputs_are_not_comparable(self):
        """None 会被转成字符串 "None"，属于无效输入。"""
        self.assertFalse(same_filesystem(None, self.existing))
        self.assertFalse(same_filesystem(self.existing, None))

    def test_unreadable_ancestor_reports_no_shared_device(self):
        """
        权限不足会沿 Path.exists() 抛出 OSError，判定必须降级为 False 而不是
        把异常抛给调用方——发布路径上崩溃比"不同盘"更难排查。

        该行为依赖权限位对当前进程生效：以 root 运行时 chmod 不起作用，
        Windows 上 os.chmod 也只切换只读位。两种环境都跳过。
        """
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("权限位对当前进程无效，跳过")

        blocked = self.base / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        try:
            self.assertFalse(same_filesystem(blocked / "child", self.existing))
        finally:
            os.chmod(blocked, 0o755)

    def test_path_with_no_existing_ancestor_reports_no_shared_device(self):
        """
        整条祖先链都不可 stat 时判定为不可比，而不是抛异常。
        真正走到这个分支的还有上面的权限用例；这里补的是"链路完全不存在"。
        """
        with patch.object(Path, "exists", return_value=False):
            self.assertFalse(same_filesystem(self.base / "ghost", self.existing))


class TestSameFilesystem(unittest.TestCase):
    """原子改名要求源与目标在同一文件系统上。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.first = self.base / "first"
        self.second = self.base / "second"
        self.first.mkdir()
        self.second.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_dirs_in_one_temp_tree_share_a_device(self):
        """同一个临时目录下的两个子目录必然同盘。"""
        self.assertTrue(same_filesystem(self.first, self.second))

    def test_missing_leaf_falls_back_to_nearest_existing_ancestor(self):
        """
        待写入的临时文件通常还不存在。判定必须回退到最近的已存在祖先，
        否则每次发布前都要先创建文件才能检查同盘。
        """
        self.assertTrue(
            same_filesystem(self.first / "not-created-yet.jpg", self.second)
        )

    def test_existing_file_compares_by_itself(self):
        """已存在的文件用自身判定，而不是它的父目录。"""
        target = self.first / "artifact.jpg"
        target.write_bytes(b"jpeg")

        self.assertTrue(same_filesystem(self.second, target))

    def test_relative_paths_are_not_comparable(self):
        """
        相对路径的落点依赖进程工作目录，无法给出有意义的同盘判断，
        必须返回 False 而不是猜一个设备号。
        """
        self.assertFalse(same_filesystem(Path("first"), self.second))
        self.assertFalse(same_filesystem(self.first, Path("second")))

    def test_blank_paths_are_not_comparable(self):
        """空路径不是有效输入。"""
        self.assertFalse(same_filesystem("", self.second))
        self.assertFalse(same_filesystem(self.first, "   "))


if __name__ == "__main__":
    unittest.main()
