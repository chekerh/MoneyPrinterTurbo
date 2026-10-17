"""``st.rerun(scope="fragment")`` 只能出现在 ``@st.fragment`` 函数里。

这不是风格问题，是**运行时崩溃**：Streamlit 在整页 rerun 走到
``scope="fragment"`` 时抛 ``StreamlitAPIException: scope="fragment" can only be
specified from @st.fragment-decorated functions``，页面上直接冒出红色 traceback。

而且它极难被发现：所有视图都能正常渲染、不点按钮就永远看不到。本仓库里就有四处
中招——计划页与媒体库的"发布"、缓存管理的"刷新统计/清理"、任务表的"取消"。点一下
必炸，全部是既有缺陷。

用 AST 静态检查而不是点按钮：按钮是否可见取决于有没有任务、缓存条目或凭据，
CI 里大多点不到，而语法错误可以稳定拦住。
"""

import ast
import unittest
from pathlib import Path

WEBUI_MAIN = Path(__file__).resolve().parent.parent.parent / "webui" / "Main.py"


def _fragment_functions(tree: ast.AST) -> set[str]:
    """返回被 ``@st.fragment`` 装饰的函数名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            text = ast.unparse(dec)
            if "fragment" in text:
                names.add(node.name)
    return names


def _rerun_calls(tree: ast.AST) -> list[tuple[str, int, str]]:
    """返回 ``(所在函数名, 行号, scope 值)``，仅限带 scope 的 ``st.rerun``。

    只有 ``scope="fragment"`` 受上下文限制：``scope="app"`` 在任何位置都合法，
    所以这里两种都收集，由调用方区分。

    scope 取的是 AST 常量的字面值（``kw.value.value``），**不是**
    ``ast.unparse()``——后者返回带引号的 ``"'fragment'"``，拿去和 ``"fragment"``
    比较会永远不相等，这个检查就会变成永远通过的空跑。
    """
    found: list[tuple[str, int, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "rerun"
            ):
                for kw in node.keywords:
                    if kw.arg != "scope":
                        continue
                    literal = getattr(kw.value, "value", None)
                    found.append(
                        (
                            self.stack[-1] if self.stack else "<module>",
                            node.lineno,
                            literal if isinstance(literal, str) else ast.unparse(kw.value),
                        )
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


class TestFragmentRerunScope(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        self.fragments = _fragment_functions(self.tree)
        self.calls = _rerun_calls(self.tree)

    def test_the_file_parsed_and_we_found_the_helpers(self):
        # 前提不成立的话下面的断言会变成空跑
        self.assertTrue(self.fragments, "没有解析到任何 @st.fragment 函数，检查器失效了")

    def test_no_scoped_rerun_outside_a_fragment(self):
        offenders = [
            f"{name}:{line}"
            for name, line, scope in self.calls
            # scope="app" 在整页 rerun 里任何位置都合法，只有 fragment 受限
            if scope == "fragment" and name not in self.fragments
        ]
        self.assertEqual(
            offenders,
            [],
            "这些函数不是 @st.fragment，用 st.rerun(scope=\"fragment\") 一点就抛 "
            "StreamlitAPIException：\n  "
            + "\n  ".join(offenders),
        )

    def test_every_scope_value_is_known(self):
        """scope 只接受 'app' / 'fragment'；拼错同样会炸。"""
        allowed = {"app", "fragment"}
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rerun"
            ):
                for kw in node.keywords:
                    if kw.arg != "scope":
                        continue
                    value = getattr(kw.value, "value", None)
                    if isinstance(value, str):
                        self.assertIn(
                            value,
                            allowed,
                            f"line {node.lineno}: unknown rerun scope {value!r}",
                        )