import os
import unittest
from unittest.mock import patch

from environment_fixtures import APPLICATION_VARIABLES, isolated_environment


class EnvironmentFixtureTests(unittest.TestCase):
    def test_runtime_variables_survive_and_app_settings_are_restored_after_failure(self):
        runtime = {
            'SYSTEMROOT': r'C:\Windows',
            'USERPROFILE': r'C:\Users\test-user',
            'HOMEDRIVE': 'C:',
            'HOMEPATH': r'\Users\test-user',
            'PATH': r'C:\Windows\System32',
            'TEMP': r'C:\Temp',
            'PYTHONIOENCODING': 'cp1252',
        }
        app = {name: f'original-{name}' for name in APPLICATION_VARIABLES}
        with patch.dict(os.environ, runtime | app):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                with isolated_environment({'MAX_RETRIES': '2'}):
                    self.assertEqual({name: os.environ[name] for name in runtime}, runtime)
                    self.assertEqual(os.environ['MAX_RETRIES'], '2')
                    for name in APPLICATION_VARIABLES:
                        if name != 'MAX_RETRIES':
                            self.assertNotIn(name, os.environ)
                    os.environ['CLIENT_SECRET'] = 'temporary-secret'
                    raise RuntimeError('test failure')
            self.assertEqual({name: os.environ[name] for name in APPLICATION_VARIABLES}, app)
            self.assertEqual({name: os.environ[name] for name in runtime}, runtime)

    def test_nested_isolation_restores_outer_settings_and_absent_keys(self):
        with isolated_environment({'CLIENT_ID': 'outer-id', 'MAX_RETRIES': '3'}):
            with isolated_environment({'DOWNLOAD_DIR': 'inner-backup'}):
                self.assertEqual(os.environ['DOWNLOAD_DIR'], 'inner-backup')
                self.assertNotIn('CLIENT_ID', os.environ)
                self.assertNotIn('MAX_RETRIES', os.environ)
            self.assertEqual(os.environ['CLIENT_ID'], 'outer-id')
            self.assertEqual(os.environ['MAX_RETRIES'], '3')
            self.assertNotIn('CLIENT_SECRET', os.environ)
            self.assertNotIn('DOWNLOAD_DIR', os.environ)
