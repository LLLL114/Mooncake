#!/usr/bin/env python3
"""SSH-only regression: never export worker buffers before teardown."""
import importlib.util
import os
from pathlib import Path
import sys
import types


class Function:
    def __init__(self, callback): self.callback=callback
    def __call__(self, *args): return self.callback(*args)


def check(fail):
    state=dict(joined=False,dumps=0)
    def dump(*args):
        state['dumps']+=1
        assert state['joined'],'unsafe dump before worker teardown'
        return 0
    lib=types.SimpleNamespace(tent_pd_configure=Function(lambda:0),tent_osc_configure=Function(lambda *a:0),
        tent_pd_dump=Function(dump),tent_osc_dump=Function(dump))
    fake=types.SimpleNamespace(bindings=lambda lib:None)
    def run(args):
        if fail: raise RuntimeError('before_join')
        state['joined']=True
    fake.run=run
    def main():
        fake.bindings(lib)
        fake.run(types.SimpleNamespace(callers=1,size=1048576,config_override=None,alpha=.01))
    fake.main=main;sys.modules['native_sender_extended']=fake
    os.environ['MC_TENT_CONF']='/root/mooncake-tent-multirdma-output/cross-node-multirail/poll-diagnostic-20260930/validation/test-config.json'
    path=Path(__file__).with_name('poll_diagnostic_sender.py')
    spec=importlib.util.spec_from_file_location('pd_sender_control_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sys.argv=[str(path),'--poll-diagnostic']
    if fail:
        try: module.main()
        except RuntimeError as error: assert str(error)=='before_join'
        else: raise AssertionError('original failure must propagate')
        assert state['dumps']==0
    else:
        module.main();assert state['dumps']==2


check(True);check(False)
print('PD_SENDER_FAILURE_PATH_TEST_OK')
