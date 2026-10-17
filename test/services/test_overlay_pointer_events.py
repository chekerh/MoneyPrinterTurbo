"""浮层必须不吃掉整页点击（回归测试）。

Streamlit 的 React Aria / baseweb 会挂一个 ``data-st-baseweb-layer-host`` 容器来放
弹层。它的样式是 ``position:fixed``、铺满视口、``z-index`` 极高，而且
``pointer-events:auto`` —— 即使里面一个子元素都没有。

结果是：任何一个浮层曾经打开过，页面上就永久多出一块看不见的"玻璃"，点哪里都没反应。
这个症状完全不出现在 Python 侧：没有异常、没有日志、单元测试全绿，用户只是觉得
"这个页面坏了"，而自动化更彻底——连按钮都点不动。

streamlit-tour 的遮罩是同一个毛病的另一面：它自己是 ``pointer-events:none``，但内部
用 SVG path 挖洞，而 Chrome 对 SVG 子元素的 ``pointer-events`` 走各自的 UA 规则，
算出来是 auto，那一条 path 把点击又吃了回来。

这些约束只能靠 CSS 兜住，因此这里直接断言样式表里那几条规则还在。删掉任意一条，
本用例就会失败。
"""

import re
import unittest
from pathlib import Path

STYLES_CSS = Path(__file__).parent.parent.parent / "webui" / "styles.css"
SOURCE = STYLES_CSS.read_text(encoding="utf-8")


def _rules_for(selector_fragment: str) -> str:
    """取所有选择器里包含该片段的声明块，拼起来便于断言。"""
    blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", SOURCE)
    picked = [
        f"{selector.strip()} {{{body.strip()}}}"
        for selector, body in blocks
        if selector_fragment in selector
    ]
    return "\n".join(picked)


class TestOverlayDoesNotBlockThePage(unittest.TestCase):
    def test_streamlit_layer_host_is_click_through(self):
        """
        空的浮层层宿主不能挡住整页点击。

        这是"页面点不动"的真凶：宿主铺满视口、层级极高、内容为空，却仍声明
        pointer-events:auto。
        """
        rules = _rules_for("data-baseweb-layer-host")
        self.assertIn("data-baseweb-layer-host", rules, "没有针对 Streamlit 浮层层宿主的规则")
        self.assertRegex(
            rules,
            r"pointer-events:\s*none\s*!important",
            "空的浮层层宿主仍然会吃掉整页点击；必须有 pointer-events:none !important",
        )

    def test_layer_host_children_stay_interactive(self):
        """
        宿主透明之后，它里面的真实弹层必须仍然可点。

        否则这条修复会把 Settings 对话框、菜单、下拉全部变成点不动的"死"元素——
        那比原来的问题更难发现。
        """
        rules = _rules_for("data-baseweb-layer-host] *")
        self.assertIn("data-baseweb-layer-host] *", rules, "没有恢复浮层子元素的点击")
        self.assertRegex(
            rules,
            r"pointer-events:\s*auto\s*!important",
            "浮层里的真实内容被连带弄成不可点了",
        )

    def test_tour_overlay_and_its_svg_cutout_are_click_through(self):
        """
        引导遮罩连同内部那条 SVG path 一起对点击透明。

        只给 .driver-overlay 设 pointer-events:none 是不够的：组件把挖洞画成 SVG
        path，Chrome 算出来是 auto，那一条 path 独自把整页点击吃回来。
        """
        rules = _rules_for(".driver-overlay")
        self.assertRegex(
            rules,
            r"\.driver-overlay\s*\*[^}]*pointer-events:\s*none\s*!important",
            "引导遮罩没有覆盖到内部元素，SVG path 仍会拦截点击",
        )

    def test_tour_popover_follows_its_target_instead_of_the_viewport(self):
        """
        气泡必须跟随被指向的元素，而不是钉在视口上。

        组件打包的 CSS 把气泡写成 ``position:fixed; top:0; right:0``，而 Driver.js
        定位时算的是文档坐标。两者一混，页面一滚动气泡就停在原地、盖住不相干的内容。
        """
        self.assertRegex(
            SOURCE,
            r"\.driver-popover\s*(,[^{]*)?\{[^}]*position:\s*absolute\s*!important",
            "气泡仍然是 fixed，会脱离被指向的元素钉在视口上",
        )

    def test_tour_popover_remains_clickable(self):
        """气泡自己必须仍然可点，否则上一步/下一步/完成就没了。"""
        rules = _rules_for(".driver-popover")
        self.assertRegex(
            rules,
            r"\.driver-popover\s*\*[^}]*pointer-events:\s*auto\s*!important",
            "引导气泡被弄成不可点，导航按钮全部失效",
        )

    def test_app_is_not_frozen_by_the_tour(self):
        """
        引导开着的时候页面仍然可交互。

        Driver.js 默认是模态遮罩，会把 ``.driver-active`` 下所有元素置为不可点。
        对一个只想指路、用户随时想跳过的引导来说，这既没必要也很烦人。
        """
        rules = _rules_for(".driver-active")
        self.assertRegex(
            rules,
            r"\.driver-active\s*\*[^}]*pointer-events:\s*auto\s*!important",
            "引导期间整个页面不可交互",
        )


if __name__ == "__main__":
    unittest.main()
