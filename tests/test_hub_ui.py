import re
import unittest
from pathlib import Path
from unittest.mock import Mock

from orbit.hub import WorkspaceRuntimeManager, create_hub_app
from orbit.platform.runtime_ownership import DiscoveredRuntime
from orbit.web.hub_ui import render_hub_ui
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

    def test_every_surface_takes_its_colour_from_the_palette(self):
        """A page with two colour schemes has to be painted in both.

        The table was given a literal dark panel while `--text` in the light
        scheme is near-black, so the two columns carrying the answer — which
        workspace, which port — were black on black for anybody not in dark
        mode. Everything else on the page already read from the palette, which
        is defined for both schemes; that one surface did not.

        Checked as the rule rather than as the colour: a literal is allowed
        only where the same selector is given a value in the dark block too,
        which is how `.status` says green twice.
        """

        css = render_hub_ui([]).split("<style>")[1].split("</style>")[0]
        dark = "".join(re.findall(r"@media\(prefers-color-scheme:dark\)\{(.*)\}", css))
        # What is left once both palettes are removed: the rules that paint.
        painting = re.sub(r":root[^{]*\{[^}]*\}", "", css)
        painting = re.sub(r"@media[^{]*\{.*?\}\s*\}", "", painting, flags=re.S)

        literals = [
            declaration
            for declaration in re.findall(
                r"(?:background|border[a-z-]*|color)\s*:[^;}]*", painting,
            )
            if re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", declaration)
        ]
        # `.status` is the one that may: its dark value is right there.
        self.assertEqual(["color:#258353"], literals, painting)
        self.assertIn(".status{color:", dark)

    def test_empty_list_does_not_start_default_runtime(self):
        launcher = Mock()
        manager = WorkspaceRuntimeManager(runtime_discovery=lambda: [], launcher=launcher)
        with AsgiHarness(create_hub_app(manager)) as client:
            response = client.get('/ui')
        self.assertEqual(200, response.status_code)
        self.assertIn('暂无运行中的 Runtime', response.content.decode())
        launcher.assert_not_called()
