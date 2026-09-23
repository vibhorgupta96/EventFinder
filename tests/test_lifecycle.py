from __future__ import annotations

import plistlib

import pytest
from eventfinder import lifecycle
from eventfinder.runtime import single_instance_lock


def test_launchd_template_uses_unique_label_and_caffeinate():
    template = plistlib.loads(open("launchd/com.eventfinder.app.plist", "rb").read())
    assert template["Label"] == "com.eventfinder.app"
    assert template["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]


def test_rendered_plist_has_project_local_logs_and_venv():
    payload = lifecycle._plist()
    assert payload["Label"] == lifecycle.LABEL
    assert "/.venv/bin/python" in payload["ProgramArguments"][2]
    assert "EventFinder" in payload["StandardOutPath"]


def test_runtime_lock_refuses_a_second_process(tmp_path):
    path = tmp_path / "eventfinder.lock"
    with single_instance_lock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance_lock(path):
                pass
