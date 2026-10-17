"""成片下载必须流式进行，且不能变成一个开放的文件下载口。

界面上的「下载」过去是把整条成片 ``read()`` 进内存再交给 Streamlit。一条 50 秒
1080p 大约 54 MB，结果页一屏 3 条——用户只是**看一眼**结果页，进程就先多吃进
160 MB，而这 160 MB 在按钮被点之前一次都没被用到。

现在走 ``/download``，由 FileResponse 边读边发。``path`` 是浏览器传来的参数，
属于不可信输入，因此根目录约束必须真的挡住 ``../`` 与任意绝对路径。
"""

import os
import unittest
from urllib.parse import unquote

from fastapi.testclient import TestClient

from app import asgi
from app.utils import utils


class TestStreamingDownloadEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(asgi.app)
        cls.roots = asgi._download_allowed_roots()
        assert cls.roots, "下载根目录为空，用例无法验证任何东西"

    def _sample_inside_a_root(self) -> str:
        import tempfile

        root = self.roots[0]
        os.makedirs(root, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(dir=root, suffix=".mp4", delete=False)
        handle.write(b"video-content")
        handle.close()
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        return handle.name

    def test_serves_a_real_file(self):
        path = self._sample_inside_a_root()
        response = self.client.get("/download", params={"path": path})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"video-content")
        self.assertTrue(response.headers["content-type"].startswith("video/"))

    def test_honours_the_suggested_file_name(self):
        """
        界面算好的建议文件名必须真的到达浏览器。

        ``st.link_button`` 没有 ``file_name`` 参数，传了会直接 TypeError，因此这个名字
        只能经 URL 传给端点。端点若忽略它，用户存下来的就是 ``final-1.mp4`` 这种
        毫无信息量的名字，而按主题命名的逻辑白算了。
        """
        path = self._sample_inside_a_root()
        response = self.client.get(
            "/download", params={"path": path, "file_name": "Mystery of the Sea-1.mp4"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        # Starlette 对非 ASCII 的建议名走 RFC 5987 的 filename*，百分号编码
        disposition = unquote(response.headers["content-disposition"])
        self.assertIn("Mystery of the Sea-1.mp4", disposition)

    def test_a_suggested_name_cannot_escape_into_a_path_or_a_header(self):
        """
        建议文件名是不可信输入：它只该出现在 Content-Disposition 里。

        带路径分隔符的名字会变成 Content-Disposition 里的路径；带 CR/LF 的名字能
        直接切断 HTTP 响应头。两样都必须被压成纯 basename。
        """
        path = self._sample_inside_a_root()
        for hostile in (
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\config\\sam",
            "evil\r\nX-Injected: yes",
        ):
            with self.subTest(file_name=hostile):
                response = self.client.get(
                    "/download", params={"path": path, "file_name": hostile}
                )
                self.assertEqual(response.status_code, 200, response.text)
                disposition = unquote(response.headers["content-disposition"])
                self.assertNotIn("/", disposition.replace('filename="', ""))
                self.assertNotIn("..", disposition)
                self.assertNotIn("X-Injected", response.headers)
                # 无论建议名多脏，实际送出的字节必须是目标文件本身
                self.assertEqual(response.content, b"video-content")

    def test_a_missing_file_name_falls_back_to_the_real_basename(self):
        path = self._sample_inside_a_root()
        response = self.client.get("/download", params={"path": path})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(
            os.path.basename(path), unquote(response.headers["content-disposition"])
        )

    def test_rejects_paths_outside_the_allowed_roots(self):
        response = self.client.get("/download", params={"path": "/etc/passwd"})
        self.assertIn(response.status_code, (403, 404))

    def test_rejects_traversal_out_of_the_allowed_roots(self):
        """
        ``..`` 必须在服务端被挡住。

        ``path`` 来自浏览器；只做前缀字符串比较的实现会被
        ``/allowed/../../etc/passwd`` 直接绕过。这里用 realpath 解析后再判断，
        所以符号链接与 ``..`` 一并失效。
        """
        import os

        root = self.roots[0]
        traversal = os.path.join(root, "..", "..", "..", "etc", "passwd")
        response = self.client.get("/download", params={"path": traversal})
        self.assertIn(response.status_code, (403, 404))

    def test_missing_file_is_not_a_server_error(self):
        import os

        response = self.client.get(
            "/download", params={"path": os.path.join(self.roots[0], "no-such-file.mp4")}
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertLess(response.status_code, 500)

    def test_uses_a_file_response_rather_than_reading_the_file(self):
        """
        端点必须返回 FileResponse，也就是由文件句柄边读边发。

        这是它存在的全部理由：用 ``open(...).read()`` 自己拼响应，几百 MB 的成片
        会把内存吃光，而用户可能永远不点下载。直接调端点函数看返回类型，比断言
        响应头更接近"它到底怎么发的"。
        """
        import asyncio

        from fastapi.responses import FileResponse

        class _URL:
            path = "/download"

        class _Request:
            # verify_token 读 headers.get("x-api-key")；未配置 api_key 时直接放行。
            headers: dict = {}

            def __init__(self, query_params):
                self.query_params = query_params
                self.method = "GET"
                self.url = _URL()

        path = self._sample_inside_a_root()
        result = asyncio.run(
            asgi.download_generated_file(
                _Request({"path": path}), path=path
            )
        )
        self.assertIsInstance(result, FileResponse)
        # 它的长度与文件名在真正发送时才 stat 出来（不预先读内容），
        # 因此这里只确认响应已经指向磁盘上的真实文件。
        self.assertEqual(result.path, path)


class TestDownloadRootsAreConstrained(unittest.TestCase):
    def test_roots_are_absolute_and_real(self):
        import os

        for root in asgi._download_allowed_roots():
            self.assertTrue(os.path.isabs(root), f"下载根不是绝对路径：{root}")
            self.assertEqual(root, os.path.realpath(root))

    def test_task_dir_is_one_of_the_roots(self):
        """任务目录必须在内：未归档的成片只存在于那里。"""
        import os

        task_dir = os.path.realpath(utils.task_dir())
        self.assertIn(task_dir, asgi._download_allowed_roots())


if __name__ == "__main__":
    unittest.main()
