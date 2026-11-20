from __future__ import annotations

import subprocess

from nebula import output


def test_copy_result_uses_available_linux_clipboard(monkeypatch):
    calls = []
    monkeypatch.setattr(output.sys, "platform", "linux")
    monkeypatch.setattr(output.os, "name", "posix")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(output.shutil, "which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(output.subprocess, "run", fake_run)
    assert output.copy_result_to_clipboard("hasil-nebula") is True
    assert calls[0][0] == ["/usr/bin/xclip", "-selection", "clipboard"]
    assert calls[0][1]["input"] == "hasil-nebula"


def test_copy_result_can_be_disabled(monkeypatch):
    monkeypatch.setenv("NEBULA_AUTO_COPY_RESULT", "false")
    monkeypatch.setattr(output.shutil, "which", lambda _name: (_ for _ in ()).throw(AssertionError("must not probe")))
    assert output.copy_result_to_clipboard("hasil") is False


def test_copy_result_fails_safely_without_clipboard(monkeypatch):
    monkeypatch.setenv("NEBULA_AUTO_COPY_RESULT", "true")
    monkeypatch.setattr(output.sys, "platform", "linux")
    monkeypatch.setattr(output.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(output.shutil, "which", lambda _name: None)
    assert output.copy_result_to_clipboard("hasil") is False


def test_emit_result_prints_and_copies_same_text(monkeypatch, capsys):
    copied = []
    monkeypatch.setattr(output, "copy_result_to_clipboard", lambda value: copied.append(value) or True)
    output.emit_result("hasil final")
    assert capsys.readouterr().out == "hasil final\n"
    assert copied == ["hasil final"]
