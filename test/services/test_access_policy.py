"""验证服务默认只监听回环地址，并且非回环绑定必须显式开启。

这台机器同时有一个局域网地址。``api_key`` 默认为空，也就是没有鉴权；
在这种组合下把监听地址设成 ``0.0.0.0`` 等于把整台机器的任务、脚本和
生成产物暴露给同一个 WiFi 里的任何人。

因此：

* 默认监听地址改为回环；
* 非回环绑定必须在配置里显式开启，否则启动即失败——而不是"先起来再说"；
* 判定逻辑是纯函数，可以直接测，不需要真的起服务。
"""

import pathlib
import unittest

from app.utils.access_policy import (
    AccessPolicyError,
    is_loopback_host,
    resolve_bind_host,
)


class TestIsLoopbackHost(unittest.TestCase):
    def test_accepts_the_loopback_literals(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_accepts_the_whole_127_range(self):
        """整个 127/8 都是回环。"""
        for host in ("127.0.0.1", "127.0.0.53", "127.1.2.3"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_rejects_wildcard_and_routable_addresses(self):
        for host in ("0.0.0.0", "::", "192.168.1.230", "10.0.0.5", "8.8.8.8"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_rejects_blank_and_odd_input(self):
        """主机名解析不了就当不安全，而不是猜它是本机。"""
        for host in ("", "   ", None, "example.com", "127.0.0.1.evil.com"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


class TestResolveBindHost(unittest.TestCase):
    def test_loopback_passes_through(self):
        self.assertEqual(resolve_bind_host("127.0.0.1", allow_non_loopback=False), "127.0.0.1")
        self.assertEqual(resolve_bind_host("::1", allow_non_loopback=False), "::1")

    def test_wildcard_is_rejected_by_default(self):
        """默认配置就是 0.0.0.0，因此这条拒绝路径必须真的生效。"""
        with self.assertRaises(AccessPolicyError) as ctx:
            resolve_bind_host("0.0.0.0", allow_non_loopback=False)

        self.assertIn("0.0.0.0", str(ctx.exception))

    def test_routable_address_is_rejected_by_default(self):
        with self.assertRaises(AccessPolicyError):
            resolve_bind_host("192.168.1.230", allow_non_loopback=False)

    def test_explicit_opt_in_allows_non_loopback(self):
        """确实需要局域网访问的人可以显式开启，但要自己负责。"""
        self.assertEqual(
            resolve_bind_host("0.0.0.0", allow_non_loopback=True), "0.0.0.0"
        )
        self.assertEqual(
            resolve_bind_host("192.168.1.230", allow_non_loopback=True),
            "192.168.1.230",
        )

    def test_blank_host_is_rejected_even_with_opt_in(self):
        """显式开启非回环不等于允许空地址。"""
        for allow in (True, False):
            with self.subTest(allow=allow):
                with self.assertRaises(AccessPolicyError):
                    resolve_bind_host("", allow_non_loopback=allow)

    def test_error_message_names_the_escape_hatch(self):
        """
        报错必须告诉用户怎么改，否则只能去翻文档或者直接放弃。
        """
        with self.assertRaises(AccessPolicyError) as ctx:
            resolve_bind_host("0.0.0.0", allow_non_loopback=False)

        message = str(ctx.exception)
        self.assertIn("allow_non_loopback_bind", message)


if __name__ == "__main__":
    unittest.main()


class TestCodeDefaultIsSafe(unittest.TestCase):
    """
    config.toml 会被仍在运行的旧进程用陈旧快照整体覆盖（save_config 直接序列化
    导入时读到的 _cfg）。因此安全不能只依赖配置文件——代码里的兜底默认值
    也必须是回环，否则一次覆盖就会重新打开公网监听。
    """

    def test_config_fallback_default_is_loopback(self):
        import app.config.config as config_module

        source = pathlib.Path(config_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'listen_host = _cfg.get("listen_host", "127.0.0.1")',
            source,
            "app/config/config.py 的兜底监听地址必须回环",
        )

    def test_non_loopback_opt_in_defaults_to_disabled(self):
        import app.config.config as config_module

        source = pathlib.Path(config_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'allow_non_loopback_bind = _cfg.get("allow_non_loopback_bind", False)',
            source,
            "非回环绑定默认必须关闭",
        )

    def test_module_level_bind_hosts_actually_resolve(self):
        """用真实模块值走一遍策略，确认默认配置能通过启动检查。"""
        from app.config import config as live

        host = resolve_bind_host(
            getattr(live, "listen_host", "127.0.0.1"),
            allow_non_loopback=bool(getattr(live, "allow_non_loopback_bind", False)),
        )
        self.assertTrue(
            is_loopback_host(host) or getattr(live, "allow_non_loopback_bind", False),
            "默认配置不应要求非回环权限",
        )
