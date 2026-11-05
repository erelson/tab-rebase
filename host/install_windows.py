"""Kept so `py install_windows.py` still works; see install.py."""
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "install.py"), run_name="__main__")
