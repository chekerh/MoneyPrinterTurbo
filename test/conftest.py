"""全局测试隔离：任何用例都不许碰到开发者真实的作品集数据。

这个仓库里有 18 个模块用 ``AppTest`` 跑真实的 ``webui/Main.py``。其中 15 个既没
改写 ``utils.task_dir()`` 也没改写 ``plans.PLANS_DIR`` / ``OUTPUTS_DIR``，于是
``_auto_archive_plan_tasks`` 会照着**真实**的任务目录和**真实**的作品集根跑一遍
归档链路。后果不是报错，而是静默改数据：

* ``test_webui_media_library`` 之类只把 plans 目录指向 tmp、用桩 ffprobe 让完整性
  校验通过，用例就会把开发者真实任务目录里的成片复制进自己的 tmp 目录，并把
  ``.plan_archived`` 标记盖在**真实任务**上；
* 标记一旦盖上，``_auto_archive_plan_tasks`` 下次就跳过该任务，成片永远进不了频道
  媒体库 —— 而界面只显示"未归档"，看不出是这个原因。

这在成片位置修好之后才变得可达：``_find_final_task_video`` 开始去 ``outputs``
目录下找成片，真实任务于是第一次对测试可见。

所以这里做全局兜底，而不是逐个模块去补打补丁 —— 需要改的点太多，漏一个就复发。
需要真实作品集数据的用例请显式标记 ``@pytest.mark.real_portfolio``。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_portfolio: 用例刻意读取开发者真实的作品集根（plans/ 与 outputs/），"
        "默认被 test/conftest.py 隔离到临时目录",
    )
    config.addinivalue_line(
        "markers",
        "real_config_write: 用例刻意测试真实的配置落盘行为，默认被 "
        "test/conftest.py 挡住",
    )
    config.addinivalue_line(
        "markers",
        "protect_config_file: 强制启用 config.toml 写保护（默认只对跑起 WebUI 的用例生效）",
    )


@pytest.fixture(autouse=True)
def block_config_persistence(request):
    """
    测试绝不允许写开发者真实的 ``config.toml``。

    WebUI 里几乎每个控件都会调 ``_set_runtime_config``，而整页跑完还会调一次
    ``_save_runtime_config``。这些默认路径会真的落盘，于是"跑一次测试"就把开发者
    本机的界面设置改掉：字幕位置被某个用例从 bottom 改成 top、字号被拖到最大，
    之后每次打开界面看到的都是测试留下的值。

    这个污染很难归因——设置看起来是"自己变了"，和测试八竿子打不着。已有的
    ``isolate_api_auth_from_local_config`` 只挡住 api_key 这一个键，挡不住界面设置。

    这里挡的是**落盘那一层**（``save_config``），而不是 ``try_save_config``：
    后者本身有锁语义与返回值约定，test_config 正在测它。

    作用范围限于**真正跑起 WebUI 的用例**：只有它们才会走到"整页跑完顺手保存"那条
    路径。直接调用 ``save_config`` 的用例（test_config）测的正是写入行为本身，
    对它们打桩等于把被测对象换成了桩。

    这里刻意不写成带 ``yield`` 的 fixture：带条件提前 ``return`` 的生成器 fixture
    会让 pytest 报 "did not yield a value"，而且那种报错与真正的原因隔得很远。
    """
    from app.config import config as app_config

    if request.node.get_closest_marker("real_config_write"):
        return
    # 默认只作用于跑起 WebUI 的模块；护栏自身的用例显式要求生效。
    if "AppTest" not in vars(request.module) and not request.node.get_closest_marker(
        "protect_config_file"
    ):
        return

    previous_ui = dict(app_config.ui)
    previous_save = app_config.save_config

    def blocked_save(*_args, **_kwargs):
        return True

    def restore():
        # save_config 必须还原：它被换掉之后，同一进程里后续模块（test_config
        # 之类）测到的就是桩而不是真写入，报错还会指向毫不相干的模块。
        app_config.save_config = previous_save
        app_config.ui.clear()
        app_config.ui.update(previous_ui)

    try:
        app_config.save_config = blocked_save
    except (AttributeError, TypeError):  # pragma: no cover - 配置层已改名
        pass
    request.addfinalizer(restore)


@pytest.fixture(autouse=True)
def isolate_api_auth_from_local_config():
    """
    清掉 ``[app] api_key``，让整个测试套件不依赖开发者本机 config.toml。

    ``base.verify_token`` 在 api_key 为空时走"本地免认证"分支，为非空时要求
    ``x-api-key`` 头。开发者一旦按建议给 /tasks 设了 key，所有直接打 API 而不带
    该头的用例就会收到 401 —— 症状出现在 test_llm 之类的模块里，与被测逻辑无关，
    极难定位。这就是审计 0.7a：``test_project_layout`` 清环境但不清真实
    config.toml，于是在配了 portfolio_root 的机器上必然失败。

    需要鉴权行为的用例自己把 api_key 设上（test_asgi_static_files、
    test_api_authentication、test_controller_base 都是这么写的），它们在用例体里
    赋值，晚于本 fixture，因此覆盖这里的取值。
    """
    from app.config import config

    previous = config.app.get("api_key", "")
    config.app["api_key"] = ""
    try:
        yield
    finally:
        config.app["api_key"] = previous


@pytest.fixture(autouse=True)
def block_desktop_side_effects(monkeypatch):
    """
    测试不得在开发者机器上弹窗或调用系统浏览器。

    WebUI 有两处真正的桌面副作用：``open_task_folder`` 用 ``webbrowser.open``
    打开任务目录，``_open_task_video`` / ``_reveal_in_file_manager`` 用
    ``subprocess`` 调 ``open`` / ``xdg-open`` / ``explorer``。用例里任何一个触发
    都会在开发者桌面上真的弹出窗口；访达随后会在被打开的真实目录里写
    ``.DS_Store``，于是"跑一次测试"就改动了作品集目录的内容 —— 与归档隔离
    无关的另一条污染路径。

    这里按调用内容而不是按模块打桩：``subprocess`` 只拦截看起来像"打开文件"的
    命令，其余调用（ffmpeg、git 等）照常放行，否则会误伤大量真实依赖。
    """
    import subprocess
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: opened.append(str(a)) or True)

    real_run = subprocess.run

    def guarded_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command:
            opener = str(command[0]).rsplit("/", 1)[-1].lower()
            if opener in {"open", "xdg-open", "explorer", "explorer.exe"}:
                opened.append(str(command))
                return subprocess.CompletedProcess(args=command, returncode=0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    # 记录下来供需要断言"确实调用了打开"的用例检查，而不是静默吞掉。
    monkeypatch.setattr(
        "test.conftest.DESKTOP_OPENS", opened, raising=False
    )


@pytest.fixture(autouse=True)
def isolate_real_portfolio(request, tmp_path, monkeypatch):
    """
    把自动归档链路会碰到的三个根指向临时目录。

    **只对用 ``AppTest`` 真正跑起 WebUI 的用例生效。** 自动归档只存在于
    ``webui/Main.py`` 里，只有这些模块会执行到它；其余模块（ASGI 静态挂载、
    控制器、缩略图等）根本不经过那段代码，隔离它们只会误伤 ——
    ``app/asgi.py`` 在 import 时就把 ``StaticFiles(directory=task_dir)`` 固定死了，
    之后再改 ``utils.task_dir`` 已经来不及，挂载点与用例写入位置就对不上。

    在这三个根上动手，不碰布局解析：``MPT_PORTFOLIO_ROOT`` 是全局配置，会连带
    影响静态文件等无关功能。归档链路只依赖这三个根，再加上"任务目录里没有成片时
    ``_find_final_task_video`` 返回空"这条，真实任务就够不着了。

    用例自己再 monkeypatch 同名属性时会覆盖这里的值（autouse fixture 先于用例体
    执行），所以已经做了正确隔离的模块不受影响。
    """
    if request.node.get_closest_marker("real_portfolio"):
        return
    # AppTest 才会把 Main.py 跑起来；没有它的模块与归档链路无关。
    if "AppTest" not in vars(request.module):
        return

    from app.utils import utils

    import plans as repo_plans

    # ``import plans`` 与 ``from tools import plans`` 会加载**两个不同的模块对象**：
    # 仓库里 tools/ 在 sys.path 上，所以两种写法都能用，而 monkeypatch 只对同一个
    # 对象生效。只按 top-level 那一份打桩的话，用 ``from tools import plans`` 的
    # 用例（例如 test_plans_cross_month）依然会读到真实作品集并往里写文件。
    # 两种都打，缺一个就漏。
    plans_modules = [repo_plans]
    try:
        from tools import plans as tools_plans

        if tools_plans is not repo_plans:
            plans_modules.append(tools_plans)
    except Exception:  # noqa: BLE001 - tools 包不可导入时只用 top-level 那份
        pass

    sandbox = tmp_path / "_real_portfolio_guard"
    (sandbox / "plans").mkdir(parents=True, exist_ok=True)
    (sandbox / "outputs").mkdir(parents=True, exist_ok=True)
    tasks = sandbox / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)

    # task_dir 接受一个可选的子目录参数，签名必须保持宽容，否则调用方一传参
    # 就变成 TypeError，症状落在被测代码之外。
    monkeypatch.setattr(utils, "task_dir", lambda *_a, **_k: str(tasks))
    for module in plans_modules:
        monkeypatch.setattr(module, "PLANS_DIR", str(sandbox / "plans"))
        monkeypatch.setattr(module, "OUTPUTS_DIR", str(sandbox / "outputs"))
    # 布局解析结果被 @st.cache_resource 缓存，进程内跨用例复用。清掉，
    # 否则上一个用例的 tmp 路径会漏进下一个。
    try:
        import streamlit as st

        st.cache_resource.clear()
    except Exception:  # noqa: BLE001 - 没有 Streamlit 运行时也不该拦住用例
        pass


@pytest.mark.protect_config_file
def test_the_suite_never_writes_the_developers_config():
    """
    回归护栏：跑测试不得改动开发者真实的 config.toml。

    WebUI 里几乎每个控件都走 ``_set_runtime_config``，整页跑完再 ``_save_runtime_config``。
    放任落盘的后果是"跑一次测试就改一次本机设置"：字幕位置被拽到 top、字号被拖到 30，
    用户下次打开界面看到的是测试留下的值，而这类变化几乎不可能被归因到测试。

    这里必须**真的改一个设置**再回读。只跑一遍不操作任何控件的页面是测不出来的——
    那样控件值与磁盘一致，无论有没有护栏都看不到差异。
    """
    import tomllib

    from streamlit.testing.v1 import AppTest

    import app.config.config as app_config

    config_path = Path(app_config.config_file)
    if not config_path.is_file():
        pytest.skip("this machine has no local config.toml")

    def read_ui():
        with open(config_path, "rb") as handle:
            return tomllib.load(handle).get("ui", {})

    before = read_ui()
    webui_main = Path(__file__).parent.parent / "webui" / "Main.py"
    app = AppTest.from_file(str(webui_main), default_timeout=90)
    app.run()
    # 改两个一定会触发 _set_runtime_config 的控件
    app.session_state["font_size_slider"] = 100
    app.session_state["open_task_folder_on_completion_checkbox"] = False
    app.run()
    after = read_ui()

    assert after == before, (
        "跑完 AppTest 并改动控件之后，config.toml 的 [ui] 段变了："
        f"测试改了开发者本机设置 {set(before.items()) ^ set(after.items())}"
    )


@pytest.fixture(autouse=True)
def _pin_default_webui_view(request, monkeypatch):
    """把 AppTest 的默认屏固定成生成页。

    落地页现在是工具台——这是产品决定，不是一条可以为了测试方便撤销的默认值。
    但 AppTest 一旦跑起来就渲染了某一屏，事后改不了，于是**只关心生成表单**的
    用例必须在第一次 run 之前就声明自己在哪一屏上。

    放在 harness 里一次性说清楚，好过让上百条用例因为落地页变了而集体失败：
    那样的失败只会诱导两种坏结果——把断言改松，或者干脆把落地页改回去。
    这里改的是"测试默认停在哪一屏"，没有一条断言因此变松；需要别的屏的用例自己写
    ``app.session_state["nav_view"]``，那行赋值发生在本 fixture 之后，优先级更高。

    想测"真正的落地页是什么"的用例（例如工具台自己的用例）把模块属性
    ``WEBUI_DEFAULT_VIEW`` 设成 ``None``，表示不要替它选。
    """
    from streamlit.testing.v1 import AppTest

    view = getattr(request.module, "WEBUI_DEFAULT_VIEW", "generate")
    if view is None:
        return

    original = AppTest.from_file

    def from_file(*args, **kwargs):
        app = original(*args, **kwargs)
        try:
            app.session_state["nav_view"] = view
        except Exception:  # noqa: BLE001 - 个别桩对象没有 session_state
            pass
        return app

    monkeypatch.setattr(AppTest, "from_file", from_file)


@pytest.fixture
def switch_view():
    """把 AppTest 切到左侧导航的某一屏并重跑。

    WebUI 现在按视图分屏（生成 / 计划 / 成片库 / 活动 / 设置）。媒体库、计划面板、
    设置页的用例只关心自己那一屏，跑之前先切过去。

    做成 fixture 而不是普通函数，是因为 conftest 本身不是可导入模块——从测试里
    ``from conftest import ...`` 会直接 ModuleNotFoundError。
    """

    def _switch(app, view: str):
        app.session_state["nav_view"] = view
        app.run()
        return app

    return _switch




@pytest.fixture
def open_task_manager():
    """展开主区域里的任务表。

    任务表现在只在用户展开时才渲染（``task_manager_open``），这正是要的效果：
    关闭时那一百多个控件一个都不产生。操作任务行的用例先调用这个 helper。
    """

    def _open(app):
        app.session_state["task_manager_open"] = True
        app.run()
        return app

    return _open


@pytest.fixture(autouse=True)
def isolate_final_video_output_root(tmp_path, monkeypatch):
    """把 ``outputs/<task_id>/`` 的落盘根指向临时目录。

    ``storage_policy.final_video_dir`` 是 ``os.makedirs(..., exist_ok=True)``：
    **只要有人调用它就会建出目录**，哪怕一个字节都不写。测试用 ``sonilo-task``、
    ``clip-speed-task`` 这类 task_id 走真实路径时，作品集根下就凭空多出一排空目录。

    这就是 ``outputs/`` 顶层那七个空目录的来历。上面那个
    ``isolate_real_portfolio`` 挡不住：它只对用 ``AppTest`` 跑起 WebUI 的模块
    生效，而写入来自流水线与归档链路的任何调用方。

    所以这里单独兜底，**不加** AppTest 条件，并且挡在模块级函数上而不是去改
    ``resolve_project_layout``——后者被 ``@st.cache_resource`` 缓存，跨用例复用。
    """
    from app.utils import storage_policy

    root = tmp_path / "outputs_root"
    monkeypatch.setattr(storage_policy, "output_dir", lambda: str(root))


@pytest.fixture(autouse=True)
def isolate_niche_stats_ledger(tmp_path, monkeypatch):
    """把赛道账本指向临时目录。

    ``storage/niche_stats.json`` 是追加写的长期数据，回答的正是"这个赛道一共做了
    几条"。``task._run_pipeline`` 每成功一次就记一笔，于是任何跑到那一步的用例
    （test_task 之类，**不**限于跑起 WebUI 的）都会把测试造出来的 task_id 追加进去。

    后果不是报错而是永久失真：统计里混着从不存在的作品，而界面上只是数字偏大，
    没有任何迹象指向"跑过测试"。上面那个 ``isolate_real_portfolio`` 救不了这一手，
    它只对用 ``AppTest`` 跑起 WebUI 的模块生效，而这个写入来自流水线本身。

    所以这里单独兜底，且**不加** AppTest 条件。
    """
    from app.services import niche_stats

    monkeypatch.setattr(
        niche_stats, "ledger_path", lambda: tmp_path / "niche_stats.json"
    )


@pytest.fixture(autouse=True)
def isolate_task_state_file(tmp_path, monkeypatch):
    """把任务状态文件指向临时目录。

    任务状态新增了落盘（``app/services/task_store.py``），于是任何构造
    ``MemoryState()`` 的用例都会去读开发者真实的 ``storage/state/tasks.json``，
    并把自己的任务写进去。症状很隐蔽：用例里 ``get_all_tasks`` 先返回了一条**上次
    跑测试留下的任务**，越查越查不到自己刚建的那条。

    只替换 ``state_file_path`` 这一个入口，**不去动 ``utils.storage_dir``**。
    后者牵涉任务目录、静态挂载点等一大片：``app.asgi`` 在 import 时就把
    ``StaticFiles(directory=task_dir)`` 固定死了，替掉它会让后来的用例读到上一个
    临时目录。
    """
    from app.services import task_store

    state_file = tmp_path / "state" / "tasks.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(task_store, "state_file_path", lambda: state_file)
    yield
