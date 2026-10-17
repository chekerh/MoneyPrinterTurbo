"""``/tasks`` 静态挂载的暴露面（安全回归）。

任务目录里放着 ``script.json``（完整脚本、搜索词、全部生成参数）、
``plan_meta.json`` 与成片本身。``app/asgi.py`` 把这个目录挂在 ``/tasks`` 上供
界面和自动化取文件，因此在 api_key 为空（出厂默认、也是本机此前的取值）时它是
**免认证**的。两个配置把"免认证"从"需要知道 task_id"放大成"能枚举出全部"：

1. ``StaticFiles(..., html=True)`` 会把目录渲染成可点的列表，于是"猜到 task_id"
   这道门槛消失；
2. ``CORSMiddleware(allow_origins=["*"], allow_credentials=True)`` 在 Starlette
   1.2 里会把请求方 Origin 原样回显进 ``Access-Control-Allow-Origin`` 并附上
   ``Access-Control-Allow-Credentials: true``。于是用户只要在应用运行时打开任意
   一个网页，那个网页就能用 ``fetch()`` 跨域读取本机 127.0.0.1 的任务产物。

鉴权走的是 ``x-api-key`` **请求头**而不是 Cookie，所以凭据支持本来就没有用途。
下面按"配置事实"而不是"源码文本"来断言：读回真实的中间件配置，避免源码里再换个
写法就绕过检查。
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ASGI_SOURCE = REPO_ROOT / "app" / "asgi.py"


def _asgi_module():
    """导入 app.asgi，拿到真实配置好的中间件栈。"""
    import app.asgi as asgi_module

    return asgi_module


class TestTaskMountIsNotListable(unittest.TestCase):
    """目录列表是"枚举全部任务"的入口，必须关掉。"""

    def test_task_mount_does_not_render_directory_listings(self):
        asgi = _asgi_module()
        tasks_mount = next(
            route
            for route in asgi.app.routes
            if getattr(route, "name", None) == ""
            and getattr(route, "path", None) == "/tasks"
        )
        self.assertFalse(
            tasks_mount.app.html,
            "/tasks 挂载启用了 html=True：任何人都能列出全部历史任务及其 "
            "script.json，无需知道 task_id",
        )

    def test_public_root_still_serves_html(self):
        """
        根挂载是公开站点，必须保留 html=True。

        上一条只针对 /tasks。把两者一起关掉会破坏公开页面，属于过度修正。
        注意根挂载的 path 是空串而不是 "/"——Mount 按前缀匹配，挂在根上时如此。
        """
        from starlette.staticfiles import StaticFiles

        asgi = _asgi_module()
        root_mount = next(
            route
            for route in asgi.app.routes
            if getattr(route, "name", None) == ""
            and getattr(route, "path", None) == ""
        )
        self.assertIsInstance(root_mount.app, StaticFiles)
        self.assertTrue(
            root_mount.app.html, "公开站点挂载被误关，首页会变成 404"
        )


class TestCorsDoesNotCombineWildcardWithCredentials(unittest.TestCase):
    """通配来源 + 凭据 = 任意网页都能跨域读本机。"""

    def _cors_middleware(self):
        """
        取真实配置好的 CORSMiddleware。

        Starlette 把 ``app.add_middleware`` 的参数存在 ``Middleware.kwargs`` 里，
        外层对象并没有 ``allow_credentials`` 属性——直接取属性会在测试里抛
        AttributeError，症状与被测问题无关。
        """
        asgi = _asgi_module()
        from starlette.middleware.cors import CORSMiddleware

        for middleware in asgi.app.user_middleware:
            if middleware.cls is CORSMiddleware:
                return middleware.kwargs
        self.fail("没有找到 CORSMiddleware，无法验证其配置")

    def test_wildcard_origins_do_not_send_credentials(self):
        kwargs = self._cors_middleware()
        self.assertFalse(
            kwargs["allow_credentials"],
            "allow_origins=['*'] 时仍然 allow_credentials=True：Starlette 会把"
            "请求方 Origin 回显进响应并附上 Allow-Credentials，任意网页因此都能"
            "跨域读取本机任务产物。鉴权用的是 x-api-key 请求头，凭据本就无用。",
        )

    def test_explicit_origins_may_still_send_credentials(self):
        """
        显式配置来源时保留凭据支持。

        真正的多源部署需要它；一刀切关掉会破坏反向代理场景。判定要基于实际的
        CORS_ALLOWED_ORIGINS，而不是源码里的常量。
        """
        configured = os.getenv("CORS_ALLOWED_ORIGINS", "")
        if not configured:
            self.skipTest("未显式配置来源，credentials 本就不应开启")
        self.assertTrue(self._cors_middleware()["allow_credentials"])


class TestUnauthenticatedTaskMountIsDisclosed(unittest.TestCase):
    """免认证必须被说出口，而不是静默。"""

    def test_warning_names_the_setting_and_the_risk(self):
        source = ASGI_SOURCE.read_text(encoding="utf-8")
        self.assertIn("api_key", source)
        self.assertIn(
            "无需认证",
            source,
            "api_key 为空时 /tasks 是免认证的，源码必须有一条 startup 警告"
            "说明这一点，否则用户不会知道自己把脚本与成片暴露给了谁",
        )


if __name__ == "__main__":
    unittest.main()
