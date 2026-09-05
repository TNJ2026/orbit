import unittest
from pathlib import Path
from unittest.mock import Mock

from orbit.hub import WorkspaceRuntimeManager, create_hub_app
from orbit.platform.runtime_ownership import DiscoveredRuntime
from tests.test_web_composition import AsgiHarness


class HubUiTests(unittest.TestCase):
    def runtime(self, path, url):
        return DiscoveredRuntime(Path('/unused'), {'project_root': path, 'base_url': url})

    def test_lists_healthy_runtimes_without_launching_or_registering(self):
        registry = Mock()
        launcher = Mock()
        manager = WorkspaceRuntimeManager(
            registry=registry, launcher=launcher,
            runtime_discovery=lambda: [
                self.runtime('/work/a', 'http://127.0.0.1:41001'),
                self.runtime('/work/<b>&', 'http://127.0.0.1:41002'),
                self.runtime('/work/offline', 'http://127.0.0.1:41003'),
                self.runtime('/work/unsafe', 'javascript:alert(1)'),
            ], health_check=lambda url: not url.endswith('41003'),
        )
        with AsgiHarness(create_hub_app(manager)) as client:
            for path in ('/ui', '/ui/'):
                response = client.get(path)
                self.assertEqual(200, response.status_code)
                body = response.content.decode()
                self.assertIn('2 个运行中的 Runtime', body)
                self.assertIn('/work/a', body)
                self.assertIn('/work/&lt;b&gt;&amp;', body)
                self.assertIn('href="http://127.0.0.1:41002/ui/"', body)
                self.assertIn('<code>41001</code>', body)
                self.assertNotIn('/work/offline', body)
                self.assertNotIn('javascript:', body)
                self.assertEqual('no-store', response.headers['cache-control'])
        launcher.assert_not_called()
        self.assertEqual([], registry.mock_calls)

    def test_empty_list_does_not_start_default_runtime(self):
        launcher = Mock()
        manager = WorkspaceRuntimeManager(runtime_discovery=lambda: [], launcher=launcher)
        with AsgiHarness(create_hub_app(manager)) as client:
            response = client.get('/ui')
        self.assertEqual(200, response.status_code)
        self.assertIn('暂无运行中的 Runtime', response.content.decode())
        launcher.assert_not_called()
