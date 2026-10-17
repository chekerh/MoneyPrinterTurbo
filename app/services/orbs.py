"""思考中的动效：把 thinking-orbs 接进 WebUI。

放在 ``app/services`` 而不是 ``webui``：后者只在 ``streamlit run`` 下才在
``sys.path`` 上，AppTest 跑整页时不在，于是这个模块会直接 ImportError。

参考物是 `thinking-orbs`（`/Users/mac/Workspace/Tooling/Skills/skills/thinking-orbs`），
九个手调的点阵动画状态。上游是 React 组件，而本应用是 Streamlit，没有 React。
好在它的 engine 是**纯几何、零 React、零 DOM** 的：``resolvePreset`` 把
(状态, 尺寸) 解析成绘制参数，``MODE_DRAWS[mode]`` 每一帧返回一批已经算好、已经
z 排序的圆点。``public/orbs/player.js`` 就是照着上游组件的画法把 engine 接到普通 canvas 上的
产物（用 esbuild 从上游 ``src/engine`` 现场打包，动画与配色与上游一致），由
``public/orbs/player.html`` 按查询串选择状态。

**为什么值得接**：这些异步操作此前只有三种反馈——一个转圈的 spinner、一条永远停在
0% 的进度条、或者干脆什么都没有。spinner 不告诉你"在做什么"，0% 进度条比没有更
糟（它承诺了一个不会到来的进度）。而"正在搜索素材 / 正在写脚本 / 正在上传"是**不同
的状态**，用不同的动画表达，比同一圈光轮转九次要清楚得多。
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit.components.v1 as components

# 动效包与播放器：两者都在应用目录里，由下面的 components.html 内联进 iframe。
_BUNDLE = Path(__file__).parent.parent / "assets" / "thinking-orbs.js"

#: 业务动作 → orb 状态。按"用户此刻在等什么"来选，而不是按内部模块名。
ACTION_STATES = {
    "generate": "working",
    "search_materials": "searching",
    "write_script": "composing",
    "voice": "listening",
    "thumbnail": "shaping",
    "upload": "connecting",
    "connect": "connecting",
    "analyse": "solving",
    "idle": "breathing",
}

#: 每个尺寸有各自的点阵密度与速度，是分别调过的两套设计而不是缩放，因此只认这两个。
SIZES = (64, 20)



_SCRIPT_CACHE: str | None = None


def _bundle() -> str:
    global _SCRIPT_CACHE
    if _SCRIPT_CACHE is None:
        _SCRIPT_CACHE = _BUNDLE.read_text(encoding="utf-8")
    return _SCRIPT_CACHE


def orb(
    state: str,
    *,
    size: int = 20,
    label: str = "",
    speed: float = 1.0,
    theme: str = "auto",
    height: int | None = None,
):
    """渲染一个思考动效。

    ``state`` 传业务动作（``"upload"``、``"write_script"``…）或直接传上游状态名
    （``"working"``…）。未知动作会回落到 ``"working"``，而不是画不出东西。
    """
    resolved = ACTION_STATES.get(state, state if state in ACTION_STATES.values() else "working")
    if size not in SIZES:
        size = 20
    box = height or size + 4
    payload = json.dumps(
        {
            "state": resolved,
            "size": size,
            "label": label,
            "speed": speed,
            "theme": theme,
        },
        ensure_ascii=False,
    )
    # 为什么非得是 iframe：
    #
    # * st.markdown 与 st.html 都不执行 <script>。它们用 innerHTML 插入内容，而按
    #   规范 innerHTML 插入的 script 不会运行——st.html 即使给了
    #   unsafe_allow_javascript=True 也不行（实测：容器渲染出来了，脚本没跑）。
    # * st.iframe 只收 URL，而 Streamlit 1.59 不再把 public/ 或 static/ 挂到应用
    #   根路径上（/static/... 与 /public/... 都回落到它自己的 SPA），因此没有
    #   可指的自定义静态文件。
    #
    # 于是只剩 st.components.v1.html。它已被标记弃用，但仍然可用；这里刻意保留，
    # 并由 test_orb_asset_survives_streamlit_removing_components_html 盯着：
    # 一旦 Streamlit 真把它移除，那条用例会立刻失败，而不是让界面上静悄悄地什么都不画。
    components.html(
        f"""
        <div id="mpt-orb" class="mpt-orb" style="height:{box}px"></div>
        <script>{_bundle()}</script>
        <script>
          (function () {{
            var cfg = {payload};
            var host = document.getElementById('mpt-orb');
            if (!host || !window.ThinkingOrbs) return;
            window.ThinkingOrbs.mount(host, cfg.state, cfg.size,
              {{ label: cfg.label, speed: cfg.speed, theme: cfg.theme }});
          }})();
        </script>
        """,
        height=box,
    )
