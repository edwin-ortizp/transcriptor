import json
import tempfile
import unittest
from pathlib import Path

import diagnostics


class FakeVault:
    def __init__(self, fail=None):
        self.fail, self.values, self.deleted = fail, {}, False

    def set_password(self, service, user, value):
        if self.fail:
            raise self.fail
        self.values[(service, user)] = value

    def get_password(self, service, user):
        return self.values.get((service, user))

    def delete_password(self, service, user):
        self.deleted = True
        self.values.pop((service, user), None)


def importer_missing(*names):
    def load(module):
        if module in names:
            raise ModuleNotFoundError(f'No module named {module}', name=module)
    return load


class DiagnosticsTest(unittest.TestCase):
    def run_checks(self, importer=lambda _: None, load=lambda: None, vault=None):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        report = diagnostics.run(load, lambda: root.name, importer, vault or FakeVault)
        self.root = root.name
        return report, {item['id']:item for item in report['checks']}

    def test_all_good(self):
        report, _ = self.run_checks()
        self.assertTrue(report['ok'])
        self.assertEqual(list(Path(self.root).iterdir()), [])

    def test_missing_dependency_names_feature_and_fix(self):
        report, checks = self.run_checks(importer_missing('spylls'))
        item = checks['dependency:spylls']
        self.assertFalse(report['ok'])
        self.assertEqual((item['state'], item['affects']), ('missing', 'Palabras sospechosas'))
        self.assertIn('pip install', item['action'])
        self.assertTrue(checks['dependency:keyring']['ok'])

    def test_installed_but_blocked_is_not_reported_as_missing(self):
        def blocked(module):
            if module == 'onnxruntime':
                raise ImportError('DLL load failed')
        _, checks = self.run_checks(blocked)
        self.assertEqual(checks['dependency:onnxruntime']['state'], 'blocked')

    def test_vault_failure_is_blocked_and_probe_is_removed(self):
        error = OSError('C:\\Users\\persona\\secreto')
        error.winerror = 1312
        vault = FakeVault(error)
        _, checks = self.run_checks(vault=lambda: vault)
        self.assertEqual(checks['vault']['state'], 'blocked')
        self.assertIn('1312', checks['vault']['detail'])
        self.assertTrue(vault.deleted)

    def test_vault_probe_leaves_nothing_behind(self):
        vault = FakeVault()
        _, checks = self.run_checks(vault=lambda: vault)
        self.assertTrue(checks['vault']['ok'])
        self.assertEqual(vault.values, {})

    def test_dictionary_and_folder_failures(self):
        def bad():
            raise ValueError('frase privada de una reunión')
        _, checks = self.run_checks(load=bad)
        self.assertEqual(checks['dictionaries']['state'], 'error')
        checked = diagnostics.check_writable(lambda: str(Path(self.root) / 'no-existe'))
        self.assertEqual(checked['state'], 'blocked')

    def test_report_does_not_expose_exception_messages(self):
        def bad():
            raise ValueError('frase privada de una reunión')
        def vault():
            raise OSError('sk-clave-secreta')
        report, _ = self.run_checks(load=bad, vault=vault)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('frase privada', text)
        self.assertNotIn('sk-clave', text)


if __name__ == '__main__':
    unittest.main()
