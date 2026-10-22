import subprocess
import sys

import pytest

from process_hud.util import tool


def test_oversize_input_does_not_start_helper(monkeypatch):
    def unexpected_spawn(*args, **kwargs):
        pytest.fail('Invalid input must be rejected before spawning a helper')
    monkeypatch.setattr(subprocess, 'Popen', unexpected_spawn)
    with pytest.raises(ValueError, match='64 KB'):
        tool(['unused'], input_text='я' * 32001)


def test_timeout_after_helper_closes_output_is_normalized():
    with pytest.raises(TimeoutError, match='Helper timed out'):
        tool([sys.executable, '-c',
              'import os,time;os.close(1);os.close(2);time.sleep(2)'], timeout=.2)


def test_helper_exchanges_bounded_input_and_both_output_streams():
    code, out, err = tool([sys.executable, '-c',
                          'import sys;print(sys.stdin.read());print("diagnostic",file=sys.stderr)'],
                         input_text='payload')
    assert code == 0 and out == 'payload\n' and err == 'diagnostic\n'
