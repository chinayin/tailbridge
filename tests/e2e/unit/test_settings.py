"""验证配置边界与共享角色解析；不接触 Docker。"""

import copy
from pathlib import Path
import tempfile
import unittest

from settings import Settings, load_config, ROOT
from config import read_yaml

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings
        self.data = {
            "project": "tailbridge", "base_domain": "e2e.tb.test",
            "server_url": "https://hs.private.test", "webui_port": 3000,
            "derp_map": read_yaml(ROOT / "environment.example.yaml")["derp_map"],
            "nodes": {"client-macbook": "client-macbook", "client-macmini": "client-macmini",
                      "client-server": "client-server"},
            "aliases": {"client-a": "client-macbook", "client-b": "client-macmini",
                        "target": "client-server", "exit": "client-server"},
            "exit_probe": {"ipv4_subnet": "198.18.0.0/24", "ipv4_address": "198.18.0.10",
                           "ipv6_subnet": "2001:db8:77::/64", "ipv6_address": "2001:db8:77::10",
                           "link_ipv4_subnet": "198.19.0.0/24", "link_ipv4_address": "198.19.0.10",
                           "link_ipv6_subnet": "2001:db8:78::/64", "link_ipv6_address": "2001:db8:78::10"},
        }

    def test_domain_and_target_follow_configuration(self):
        self.data["base_domain"] = "e2e.tb.example.org"
        self.data["nodes"]["client-server"] = "client-nas"
        config = self.settings(self.data)
        self.assertEqual(config.fqdn("target"), "client-nas.e2e.tb.example.org")
        self.assertEqual(config.env["TB_CLIENT_SERVER_HOSTNAME"], "client-nas")
        self.assertNotIn("registration_user", config.deployment)
        self.assertEqual(config.relays, ["derp-main"])
        self.assertEqual(config.env["SERVER_URL"], "https://hs.private.test")

    def test_production_domain_is_used_verbatim(self):
        self.data["base_domain"] = "tb.test"
        self.assertEqual(self.settings(self.data).fqdn("target"), "client-server.tb.test")

    def test_derp_region_and_node_reach_the_control_environment(self):
        region = next(iter(self.data['derp_map']['regions'].values()))
        region.update(regionid=912, regioncode='cn-hongkong', regionname='Hong Kong')
        region['nodes'][0].update(name='tb-derp-hk-02', regionid=912)
        self.data['derp_map']['regions'] = {912: region}
        try:
            config = self.settings(self.data)
        except ValueError as error:
            self.fail(f'Valid custom region was rejected: {error}')
        self.assertEqual(config.env['DERP_REGION_ID'], '912')
        self.assertEqual(config.env['DERP_REGION_CODE'], 'cn-hongkong')
        self.assertEqual(config.env['DERP_REGION_NAME'], 'Hong Kong')
        self.assertEqual(config.env['DERP_NODE_NAME'], 'tb-derp-hk-02')
        self.assertEqual(config.relays, ['derp-main'])
        self.assertEqual(config.relay_nodes[0]['name'], 'tb-derp-hk-02')

    def test_unsafe_derp_region_names_are_rejected_before_startup(self):
        for value in ('Hong Kong & Macau', "St. John's", 'bad"name', 'bad|name',
                      'bad\\name', 'bad\nname', 'bad\x7fname', '${SERVER_URL}'):
            data = copy.deepcopy(self.data)
            next(iter(data['derp_map']['regions'].values()))['regionname'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'regionname'):
                self.settings(data)

    def test_exit_probe_uses_explicit_inputs_and_is_not_a_production_component(self):
        self.data["exit_probe"].update(ipv4_subnet="198.19.8.0/24", ipv4_address="198.19.8.20")
        config = self.settings(self.data)
        self.assertEqual(config.service("exit"), "client-server")
        self.assertEqual(config.env["TB_EXIT_PROBE_IPV4_SUBNET"], "198.19.8.0/24")
        self.assertEqual(config.env["TB_EXIT_PROBE_IPV4_ADDRESS"], "198.19.8.20")
        self.assertNotIn("exit_probe", config.deployment)

    def test_exit_probe_rejects_missing_mismatched_or_nonisolated_addresses(self):
        mutations = (
            lambda d: d.pop("exit_probe"),
            lambda d: d["aliases"].pop("exit"),
            lambda d: d["aliases"].update(exit="client-macbook"),
            lambda d: d["exit_probe"].pop("ipv6_address"),
            lambda d: d["exit_probe"].update(ipv4_address="198.19.0.10"),
            lambda d: d["exit_probe"].update(ipv4_address="198.18.0.0"),
            lambda d: d["exit_probe"].update(ipv4_address="198.18.0.1"),
            lambda d: d["exit_probe"].update(ipv4_address="198.18.0.255"),
            lambda d: d["exit_probe"].update(ipv4_subnet="192.168.0.0/24", ipv4_address="192.168.0.10"),
            lambda d: d["exit_probe"].update(ipv6_subnet="fd00::/64", ipv6_address="fd00::10"),
            lambda d: d["exit_probe"].update(ipv6_address="invalid"),
            lambda d: d["exit_probe"].update(link_ipv6_subnet="2001:db8:77::/64", link_ipv6_address="2001:db8:77::20"),
            lambda d: d["exit_probe"].update(link_ipv4_address="198.19.0.1"),
            lambda d: d["exit_probe"].update(link_ipv4_address=123),
        )
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.settings(data)

    def test_swapping_clients_changes_role_resolution_without_state_remapping(self):
        self.data["aliases"]["client-a"], self.data["aliases"]["client-b"] = (
            self.data["aliases"]["client-b"], self.data["aliases"]["client-a"])
        config = self.settings(self.data)
        self.assertEqual(config.service("client-a"), "client-macmini")
        self.assertEqual(config.credentials[config.service("client-a")], "macmini")

    def test_exit_link_rejects_insufficient_addresses_before_startup(self):
        self.data["exit_probe"].update(link_ipv4_subnet="198.19.0.0/30", link_ipv4_address="198.19.0.2")
        with self.assertRaisesRegex(ValueError, "three hosts"):
            self.settings(self.data)

    def test_invalid_configs_are_rejected(self):
        mutations = (
            lambda d: d.update(base_domain="tb.xxx"),
            lambda d: d.update(base_domain="bad_name.test"),
            lambda d: d.update(server_url="https://control.e2e.tb.test"),
            lambda d: d.update(server_url="https://e2e.tb.test"),
            lambda d: d.update(server_url="https://user:password@hs.private.test"),
            lambda d: d["aliases"].update(target="missing"),
            lambda d: d["aliases"].pop("target"),
            lambda d: d["aliases"].update(target="client-macbook"),
            lambda d: d["nodes"].update({"client-server": "client-macbook"}),
            lambda d: d.update(webui_port=0),
            lambda d: d.update(registration_user="owner"),
            lambda d: d.update(domain_prefix="e2e"),
        )
        for mutate in mutations:
            data = copy.deepcopy(self.data)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.settings(data)

    def test_default_yaml_contains_the_complete_domain(self):
        config = load_config(ROOT / "environment.example.yaml")
        self.assertEqual(config.data["base_domain"], "e2e.tb.test")
        self.assertEqual(config.fqdn("target"), "client-server.e2e.tb.test")

    def test_explicit_yaml_supports_comments_and_nested_mappings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "environment.yaml"
            path.write_text((ROOT / "environment.example.yaml").read_text().replace(
                "base_domain: e2e.tb.test", "base_domain: tb.example.org # deployment domain"))
            config = load_config(path)
            self.assertEqual(config.fqdn("target"), "client-server.tb.example.org")
            self.assertEqual(config.service("client-a"), "client-macbook")

    def test_invalid_yaml_is_reported_as_configuration_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.yaml"
            for source in ("project: [", "!!python/tuple [1, 2]"):
                path.write_text(source)
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Invalid YAML"):
                    load_config(path)

    def test_duplicate_and_invalid_yaml_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'environment.yaml'
            for source in ('project: first\nproject: second\n', 'true: value\n', '1.5: value\n'):
                path.write_text(source)
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'unique strings or integers'):
                    read_yaml(path)

    def test_invalid_service_inputs_are_rejected_before_startup(self):
        cases = [('project', '../../outside'), ('base_domain', 'tb'),
                 ('server_url', 'http://hs.private.test'), ('server_url', 'https://hs.private.test:8443'),
                 ('server_url', 'https://hs.private.test/path'), ('server_url', 'https://127.0.0.1'),
                 ('webui_port', True), ('webui_port', 443), ('webui_port', 65536)]
        for key, value in cases:
            data = copy.deepcopy(self.data)
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.settings(data)

    def test_invalid_derp_topologies_are_rejected_before_startup(self):
        identifier = next(iter(self.data['derp_map']['regions']))
        changes = [lambda d: d.update(derp_map={'regions': {}}),
                   lambda d: d['derp_map']['regions'][identifier].update(regionid=identifier+1),
                   lambda d: d['derp_map']['regions'][identifier]['nodes'].append(
                       copy.deepcopy(d['derp_map']['regions'][identifier]['nodes'][0]))]
        for key, value in [('ipv4', '203.0.113.10'), ('ipv4', '192.168.1.1'), ('ipv4', '224.0.0.1'),
                           ('ipv4', 'bad'), ('name', '../../other'), ('hostname', 'derp.e2e.tb.test'),
                           ('hostname', '8.8.8.8'), ('derpport', True), ('derpport', 0),
                           ('stunport', 65536), ('canport80', True), ('regionid', identifier+1)]:
            changes.append(lambda d, k=key, v=value: d['derp_map']['regions'][identifier]['nodes'][0].update({k: v}))
        for change in changes:
            data = copy.deepcopy(self.data)
            change(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.settings(data)

        data = copy.deepcopy(self.data)
        second = copy.deepcopy(data['derp_map']['regions'][identifier])
        second.update(regionid=identifier+1, regioncode='ap-northeast-1', regionname='Tokyo')
        second['nodes'][0].update(regionid=identifier+1, name='tb-derp-jp-01', hostname='derp-jp.private.test')
        data['derp_map']['regions'][identifier+1] = second
        with self.assertRaisesRegex(ValueError, 'one DERP region'):
            self.settings(data)

    def test_runtime_and_external_volumes_are_scoped_to_the_instance(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder).resolve()
            volumes = runtime / "volumes.tailbridge.yaml"
            volumes.write_text("volumes: {}\n")
            existing = self.settings(self.data, runtime)
            self.assertIn(str(volumes), existing.compose)
            self.assertEqual(existing.generated, runtime.resolve() / "generated")
            self.assertIn(str(ROOT / "compose.yaml"), existing.compose)
            self.data["project"] = "separate-test"
            isolated = self.settings(self.data, runtime)
            self.assertNotIn(str(volumes), isolated.compose)
            self.assertEqual(isolated.project, "separate-test")


if __name__ == "__main__":
    unittest.main()
