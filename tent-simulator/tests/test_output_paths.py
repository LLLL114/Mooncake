"""Output root regression tests."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OutputPathsTest(unittest.TestCase):
    def invoke(self, output, code):
        env = dict(os.environ, MOONCAKE_TENT_OUTPUT=str(output),
                   PYTHONPATH=str(ROOT/'scripts'), PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable,'-B','-c',code],env=env,
                              text=True,capture_output=True)

    def test_external_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            code = '''import experiment_paths as p
for root,name in [(p.SOURCE_ROOT,'simulator/driver.cpp'),
                  (p.OUTPUT_ROOT,'build/release/observed'),
                  (p.OUTPUT_ROOT,'acceptance/build-manifest.json')]:
    assert p.artifact_path(p.artifact_key(root/name))==root/name
assert p.artifact_path(p.LEGACY_ROOT/'runs/test.json')==p.OUTPUT_ROOT/'runs/test.json'
assert p.artifact_path('acceptance/acceptance-plan.md')==p.SOURCE_ROOT/'acceptance/acceptance-plan.md'
'''
            result=self.invoke(temp,code)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_repository_is_rejected(self):
        result=self.invoke(ROOT/'must-not-exist','import experiment_paths')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('outside the Mooncake repository',result.stderr)
        self.assertFalse((ROOT/'must-not-exist').exists())

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            alias=Path(temp)/'alias'
            alias.symlink_to(ROOT,target_is_directory=True)
            self.assertNotEqual(self.invoke(alias,'import experiment_paths').returncode,0)

    def test_cli_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            env=dict(os.environ,MOONCAKE_TENT_OUTPUT=temp,PYTHONDONTWRITEBYTECODE='1')
            target=ROOT/'must-not-exist'
            cases=[['analyze.py','missing-input.json',str(target)],
                   ['capture_environment.py','--output-dir',str(target)]]
            for name,*args in cases:
                result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts'/name),*args],env=env,text=True,capture_output=True)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('outside the Mooncake repository',result.stderr)
            self.assertFalse(target.exists())


if __name__=='__main__':
    unittest.main()
