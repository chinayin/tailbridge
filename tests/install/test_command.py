"""Exercise the installer test command without touching host networking."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.state = self.base / 'state.json'
        self.initial = {'ExitNodeID': '', 'ExitNodeIP': '', 'AutoExitNode': '',
                        'ExitNodeAllowLANAccess': True, 'CorpDNS': True, 'RouteAll': False,
                        'AdvertiseRoutes': ['192.0.2.0/24'], 'Persist': {'PrivateNodeKey': 'do-not-print'}}
        self.state.write_text(json.dumps({'prefs': self.initial, 'sets': []}))
        self.env = {**os.environ, 'PATH': str(self.bin), 'TEST_STATE_FILE': str(self.state),
                    'TMPDIR': str(self.base), 'SERVER_URL': '', 'NODE_AUTHKEY': ''}
        (self.bin / 'python3').symlink_to(sys.executable)
        (self.bin / 'cat').symlink_to('/bin/cat')
        self.stub('tailscale', '''
p=Path(os.environ['TEST_STATE_FILE']); data=json.loads(p.read_text()); prefs=data['prefs']
scenario=os.environ.get('TEST_SCENARIO','')
peers={'target':{'ID':'1','HostName':'exit-a','DNSName':'exit-a.tail.test.',
                 'TailscaleIPs':['100.77.0.1'],'Online':scenario!='offline','ExitNodeOption':True},
       'old':{'ID':'2','HostName':'exit-b','DNSName':'exit-b.tail.test.',
              'TailscaleIPs':['100.77.0.2'],'Online':True,'ExitNodeOption':True}}
if sys.argv[1:]==['status','--json']:
    print(json.dumps({'BackendState':'NeedsLogin' if scenario=='logged_out' else 'Running','TUN':scenario!='userspace',
                      'Self':{'ID':'3','HostName':'client','TailscaleIPs':['100.77.0.3']},'Peer':peers}))
elif sys.argv[1:]==['debug','prefs']:
    print('invalid JSON' if scenario=='bad_prefs' else json.dumps(prefs))
elif sys.argv[1:]==['netcheck']: print('UDP: true; DERP: available')
elif sys.argv[1]=='set':
    args=sys.argv[2:]
    assert len(args)==2 and args[0].startswith('--exit-node=') and args[1].startswith('--exit-node-allow-lan-access=')
    target=args[0].split('=',1)[1]
    data['sets'].append(target)
    data.setdefault('lan_settings', []).append(args[1].endswith('=true'))
    if scenario=='restore_failure' and len(data['sets'])==3:
        p.write_text(json.dumps(data)); raise SystemExit(1)
    prefs['ExitNodeID']={'':'','100.77.0.1':'1','100.77.0.2':'2','auto:any':'2'}[target]
    prefs['ExitNodeIP']=''
    prefs['AutoExitNode']='any' if target=='auto:any' else ''
    prefs['ExitNodeAllowLANAccess']=args[1].endswith('=true')
    p.write_text(json.dumps(data))
    if scenario=='selection_failure' and target=='100.77.0.1': raise SystemExit(1)
else: raise SystemExit('Unexpected tailscale mutation: '+repr(sys.argv[1:]))
''')
        self.stub('curl', '''
import signal
args=sys.argv[1:]
assert args[0]=='-q' and args[args.index('--noproxy')+1]=='*'
assert '-4' in args and '--max-time' in args and '--connect-timeout' in args
assert '-k' not in args and '--insecure' not in args
data=json.loads(Path(os.environ['TEST_STATE_FILE']).read_text()); prefs=data['prefs']
selected=prefs['ExitNodeID']=='1'; scenario=os.environ.get('TEST_SCENARIO','')
if scenario=='signal' and selected and 'checkip.amazonaws.com' in args[-1]:
    os.kill(os.getppid(),signal.SIGTERM)
failed=scenario=='all_fail' or (scenario=='baseline_failure' and not selected) or (scenario=='exit_failure' and selected)
failed=failed or (scenario=='restored_http_failure' and len(data['sets'])>=3)
ip='203.0.113.10' if selected else ('203.0.113.20' if prefs['ExitNodeID']=='2' else '198.51.100.10')
body='current IP: '+ip if 'ipip' in args[-1] else ip
Path(args[args.index('--output')+1]).write_text(body)
print(json.dumps({'http_code':0 if failed else 200,'time_total':0.1,'remote_ip':'203.0.113.80'}))
raise SystemExit(35 if failed else 0)
''')

    def stub(self, name, source):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n'+source)
        path.chmod(0o755)

    def run_test(self, *args, scenario='', expected=0):
        result = subprocess.run(['/bin/bash', str(ROOT/'install.sh'), 'test', *args],
                                env={**self.env, 'TEST_SCENARIO': scenario},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout+result.stderr)
        self.assertNotIn('do-not-print', result.stdout+result.stderr)
        return result

    def saved(self):
        return json.loads(self.state.read_text())

    def test_missing_tailscale_skips_without_installing_anything(self):
        (self.bin/'tailscale').unlink()
        result = self.run_test()
        self.assertIn('[SKIP]', result.stderr)
        self.assertIn('tailscale', result.stderr)
        self.assertEqual(self.saved()['sets'], [])

    def test_missing_other_dependencies_skip_without_installation(self):
        for dependency in ('curl', 'python3'):
            with self.subTest(dependency=dependency):
                path=self.bin/dependency; hidden=self.bin/(dependency+'.hidden')
                path.rename(hidden)
                try:
                    self.assertIn('[SKIP]', self.run_test().stderr)
                finally:
                    hidden.rename(path)

    def test_local_check_needs_no_role_root_linux_or_configuration(self):
        result=self.run_test()
        self.assertIn('198.51.100.10', result.stdout)
        self.assertEqual(self.saved(), {'prefs':self.initial,'sets':[]})

    def test_exit_comparison_restores_selection_lan_and_other_preferences(self):
        result=self.run_test('--exit-node','exit-a')
        self.assertIn('198.51.100.10', result.stdout)
        self.assertIn('203.0.113.10', result.stdout)
        self.assertEqual(self.saved()['prefs'], self.initial)
        self.assertEqual(self.saved()['sets'], ['', '100.77.0.1', ''])
        self.assertEqual(self.saved()['lan_settings'], [True, True, True])

    def test_exit_enables_lan_then_restores_disabled_preference(self):
        self.initial['ExitNodeAllowLANAccess']=False
        for scenario, expected in [('', 0), ('exit_failure', 1), ('selection_failure', 1), ('signal', 130)]:
            with self.subTest(scenario=scenario):
                self.state.write_text(json.dumps({'prefs':self.initial,'sets':[]}))
                self.run_test('--exit-node','exit-a',scenario=scenario,expected=expected)
                self.assertEqual(self.saved()['prefs'], self.initial)
                self.assertEqual(self.saved()['lan_settings'], [False, True, False])

    def test_existing_exit_selection_is_restored(self):
        self.initial['ExitNodeID']='2'
        self.state.write_text(json.dumps({'prefs':self.initial,'sets':[]}))
        self.run_test('--exit-node=100.77.0.1')
        self.assertEqual(self.saved()['prefs'], self.initial)
        self.assertEqual(self.saved()['sets'][-1], '100.77.0.2')

    def test_automatic_exit_selection_is_restored(self):
        self.initial.update(ExitNodeID='2', AutoExitNode='any')
        self.state.write_text(json.dumps({'prefs':self.initial,'sets':[]}))
        self.run_test('--exit-node','exit-a.tail.test')
        self.assertEqual(self.saved()['prefs'], self.initial)

    def test_failed_baseline_does_not_prevent_exit_comparison(self):
        result=self.run_test('--exit-node','exit-a',scenario='baseline_failure')
        self.assertIn('203.0.113.10', result.stdout)
        self.assertEqual(self.saved()['prefs'], self.initial)

    def test_failed_exit_http_restores_preferences_and_reports_failure(self):
        self.run_test('--exit-node','exit-a',scenario='exit_failure',expected=1)
        self.assertEqual(self.saved()['prefs'], self.initial)

    def test_failure_after_selection_mutation_still_restores(self):
        self.run_test('--exit-node','exit-a',scenario='selection_failure',expected=1)
        self.assertEqual(self.saved()['prefs'], self.initial)

    def test_interrupt_during_exit_requests_restores_preferences(self):
        self.run_test('--exit-node','exit-a',scenario='signal',expected=130)
        self.assertEqual(self.saved()['prefs'], self.initial)

    def test_restore_failure_is_not_reported_as_success(self):
        result=self.run_test('--exit-node','exit-a',scenario='restore_failure',expected=1)
        self.assertIn('restore', result.stderr.lower())
        self.assertEqual(self.saved()['prefs']['ExitNodeID'],'1')

    def test_userspace_exit_is_rejected_before_changing_routes(self):
        self.run_test('--exit-node','exit-a',scenario='userspace',expected=1)
        self.assertEqual(self.saved()['sets'],[])

    def test_previously_working_connectivity_must_recover_after_restoration(self):
        self.run_test('--exit-node','exit-a',scenario='restored_http_failure',expected=1)
        self.assertEqual(self.saved()['prefs'],self.initial)

    def test_unavailable_exit_and_invalid_preferences_do_not_change_settings(self):
        for scenario in ('offline','bad_prefs','logged_out'):
            with self.subTest(scenario=scenario):
                self.run_test('--exit-node','exit-a',scenario=scenario,expected=1)
                self.assertEqual(self.saved()['sets'],[])

    def test_local_connectivity_failure_returns_nonzero(self):
        self.run_test(scenario='all_fail',expected=1)

    def test_invalid_arguments_are_rejected_before_any_mutation(self):
        for args in [('--exit-node',),('--exit-node=',),('--force',),('--role','node'),('unexpected',)]:
            with self.subTest(args=args):
                self.run_test(*args,expected=1)
                self.assertEqual(self.saved()['sets'],[])

    def test_test_help_needs_no_tailscale(self):
        (self.bin/'tailscale').unlink()
        result=self.run_test('--help')
        self.assertIn('--exit-node',result.stdout)


if __name__=='__main__':
    unittest.main()
