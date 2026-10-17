"""tools/plans.py 必须显式解析作品集根，不能靠 __file__ 的位置去猜。

回归背景：tools/ 被 vendor 进应用仓库（commit 51ac3b4）之后，plans.py 里
``dirname(dirname(__file__))`` 从"作品集根"悄悄变成了"应用根"。应用根没有
plans/ 目录，于是 ``list_channels()`` 返回空列表，WebUI 的内容计划面板与成片库
面板各自渲染成一个只有标题的空折叠块，全程没有任何报错。

这类 bug 能溜过去，是因为既有用例全都 monkeypatch 掉 ``PLANS_DIR`` /
``OUTPUTS_DIR``，真实的根推导一次都没被执行过。这里补上对推导本身的覆盖。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
while REPO_ROOT != os.path.dirname(REPO_ROOT) and not os.path.isfile(
    os.path.join(REPO_ROOT, "pyproject.toml")
):
    REPO_ROOT = os.path.dirname(REPO_ROOT)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import plans  # noqa: E402


def _seed_portfolio(root: str, channel: str = "chan") -> str:
    """在 root 下造出一个"真的像作品集根"的目录树，返回它。"""
    plans_dir = os.path.join(root, "plans", channel)
    os.makedirs(plans_dir)
    with open(
        os.path.join(plans_dir, "topics_30day.txt"), "w", encoding="utf-8"
    ) as handle:
        handle.write("Topic one\nTopic two\n")
    return root


def _resolve_in_subprocess(setup: str) -> str:
    """在子进程里导入 plans 并打印解析出的根。

    必须开子进程：``BASE_DIR`` / ``PLANS_DIR`` / ``OUTPUTS_DIR`` 是导入时算出的
    模块级常量，而每个用例都要用不同的目录布局重跑一遍。在同一进程里改完只能
    reload，等于把被测的那段逻辑换掉。
    """
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {TOOLS_DIR!r})
        {setup}
        import plans
        print(plans.BASE_DIR)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.mark.real_portfolio
class TestPortfolioRootResolution(unittest.TestCase):
    """解析顺序：MPT_PORTFOLIO_ROOT > config.toml > 真的有 plans/ 的目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_env_var_overrides_everything_else(self):
        """环境变量是"只跑这一次"的显式覆盖，必须压过 config.toml。"""
        from_env = os.path.join(self.base, "from-env")
        os.makedirs(from_env)

        resolved = _resolve_in_subprocess(
            f'os.environ["MPT_PORTFOLIO_ROOT"] = {from_env!r}\n'
        )

        self.assertEqual(resolved, from_env)

    def test_configured_root_is_read_from_config_toml(self):
        """[app] portfolio_root 是声明式的配置，必须被读到。"""
        app_root = os.path.join(self.base, "app")
        configured = _seed_portfolio(os.path.join(self.base, "configured"))
        os.makedirs(app_root)
        with open(
            os.path.join(app_root, "config.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                '[app]\nportfolio_root = "%s"\n' % configured.replace("'", "\\'")
            )

        self.assertEqual(plans._portfolio_root_from_config(app_root), configured)

    def test_missing_or_unparseable_config_yields_no_root(self):
        """
        读不到配置等于"没配"，而不是"配了个空串"。

        区别在于：空串会让 resolve_portfolio_root 以为用户显式配了空值，从而
        跳过向后查找，直接用猜出来的目录——正好复现原来的 bug。
        """
        app_root = os.path.join(self.base, "app")
        os.makedirs(app_root)

        self.assertEqual(plans._portfolio_root_from_config(app_root), "")

        with open(
            os.path.join(app_root, "config.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write("[app]\nportfolio_root = ''\n")
        self.assertEqual(plans._portfolio_root_from_config(app_root), "")

        with open(
            os.path.join(app_root, "config.toml"), "w", encoding="utf-8"
        ) as handle:
            handle.write("this is not = valid = toml [[[\n")
        self.assertEqual(plans._portfolio_root_from_config(app_root), "")

    def test_configured_root_wins_even_without_a_plans_directory(self):
        """
        显式配置就应当生效，哪怕那个目录下还没有 plans/。

        悄悄改用"看起来更对"的另一个目录，会让用户以为自己配的路径被采纳了；
        正确做法是照配置解析，再由 list_channels() 返回空列表如实反映。
        """
        configured = os.path.join(self.base, "configured-but-empty")
        os.makedirs(configured)

        with mock.patch.object(
            plans, "_portfolio_root_from_config", return_value=configured
        ):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MPT_PORTFOLIO_ROOT", None)
                self.assertEqual(plans.resolve_portfolio_root(), configured)

    def test_falls_back_to_the_directory_that_actually_holds_plans(self):
        """
        没有任何配置时，向外找第一个真的有 plans/ 的目录。

        这就是原 bug 的回归点：把 plans.py 搬到另一棵树里之后，它仍然必须找到
        放着真实数据的那棵。按"有没有内容"锚定才做得到，数 dirname() 层数则
        会跟着文件的搬家静默漂移。
        """
        content_root = _seed_portfolio(os.path.join(self.base, "with-content"))
        relocated_tools = os.path.join(content_root, "tools")
        os.makedirs(relocated_tools)
        shutil.copyfile(
            plans.__file__, os.path.join(relocated_tools, "plans.py")
        )

        script = textwrap.dedent(
            f"""
            import os, sys
            sys.path.insert(0, {relocated_tools!r})
            os.environ.pop("MPT_PORTFOLIO_ROOT", None)
            import plans
            print(plans.resolve_portfolio_root())
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(completed.stdout.strip(), content_root)

    def test_plans_and_outputs_hang_off_the_same_root(self):
        """
        plans/ 与 outputs/ 必须挂在同一个根下。

        这两个目录一旦被推导到不同的树上，媒体库就会在 A 树里找归档、而新成片
        写到 B 树里，两边永远对不上，而且没有任何报错。
        """
        self.assertEqual(
            os.path.dirname(plans.PLANS_DIR), os.path.dirname(plans.OUTPUTS_DIR)
        )
        self.assertEqual(os.path.dirname(plans.PLANS_DIR), plans.BASE_DIR)


@pytest.mark.real_portfolio
class TestPlansAgreesWithLayoutResolver(unittest.TestCase):
    """plans.py 与 app.services.project_layout 必须解析出同一个作品集根。"""

    def test_both_resolvers_pick_the_same_portfolio_root(self):
        from app.services.project_layout import resolve_project_layout

        # 布局解析返回 Path，plans.py 全程用 str（os.path.join 的老接口），
        # 所以按字符串比路径，不在这里强行统一两边的类型。
        self.assertEqual(
            str(resolve_project_layout().portfolio_root), plans.BASE_DIR
        )

    def test_list_channels_sees_the_configured_channels(self):
        """
        端到端守住最初的症状：根解析对了，频道列表就必须非空。

        面板空白的直接原因就是 list_channels() 返回 []，所以这里直接断言它能
        看见至少一个频道，而不是去断言面板渲染——后者还要搭整个 Streamlit 环境，
        且失败时定位不到根解析这一步。
        """
        self.assertTrue(plans.list_channels())


if __name__ == "__main__":
    unittest.main()
