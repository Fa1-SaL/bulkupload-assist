"""Launch the real UI against the event support provided by older Windows Tk."""
from pathlib import Path
import subprocess
import sys
import unittest

from test_extractor import _tk_available


@unittest.skipUnless(_tk_available(), 'Tk is not available')
class TestWindowsStartup(unittest.TestCase):
    def test_app_and_settings_open_when_tk_has_no_touchpad_event(self):
        code = '''
import tkinter as tk
from unittest.mock import patch
from test_ui_flow import TestSimpleWorkflow
original_bind = tk.Misc.bind
def older_tk_bind(self, sequence=None, func=None, add=None):
    if sequence and 'TouchpadScroll' in sequence:
        raise tk.TclError('bad event type or keysym "TouchpadScroll"')
    return original_bind(self, sequence, func, add)
case = TestSimpleWorkflow()
try:
    with patch.object(tk.Misc, 'bind', older_tk_bind):
        case.setUp()
        case.app.deiconify()
        case.app.update()
        assert case.app.winfo_viewable()
        case.app.toggle_settings()
        case.app.update()
        assert case.app._settings_window.winfo_viewable()
finally:
    case.doCleanups()
'''
        result = subprocess.run([sys.executable, '-c', code],
            cwd=Path(__file__).resolve().parent, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
