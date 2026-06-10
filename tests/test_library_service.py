from types import SimpleNamespace

from jellyfin_kodi import library as library_module


def _library_instance(is_playing):
    library = library_module.Library.__new__(library_module.Library)
    library.player = SimpleNamespace(isPlayingVideo=lambda: is_playing)
    library.pending_refresh = False
    library.deferred_library_update = False
    library.download_threads = []
    library.writer_threads = {"updated": [], "userdata": [], "removed": []}
    library.total_updates = 0
    library.progress_display = 50
    library.progress_updates = None
    library.screensaver = None
    library.stop_thread = False
    library.worker_downloads = lambda: None
    library.worker_sort = lambda: None
    library.worker_updates = lambda: None
    library.worker_userdata = lambda: None
    library.worker_remove = lambda: None
    library.worker_notify = lambda: None
    library.save_last_sync = lambda: None
    return library


def test_service_dispatches_deferred_library_update_when_video_not_playing(monkeypatch):
    library = _library_instance(is_playing=False)
    library.deferred_library_update = True
    builtins = []

    monkeypatch.setattr(library_module, "settings", lambda _: False)
    monkeypatch.setattr(library_module, "window", lambda *args, **kwargs: None)
    monkeypatch.setattr(library_module, "set_screensaver", lambda **kwargs: None)
    monkeypatch.setattr(library_module, "get_screensaver", lambda: None)
    monkeypatch.setattr(
        library_module.xbmc,
        "getCondVisibility",
        lambda condition: condition == "Window.IsMedia",
    )
    monkeypatch.setattr(
        library_module.xbmc, "executebuiltin", lambda command: builtins.append(command)
    )

    library_module.Library.service.__wrapped__(library)

    assert builtins == ["UpdateLibrary(video)", "Container.Refresh"]
    assert library.deferred_library_update is False


def test_service_defers_pending_refresh_update_during_playback(monkeypatch):
    library = _library_instance(is_playing=True)
    library.pending_refresh = True
    builtins = []
    save_last_sync_calls = []

    monkeypatch.setattr(library_module, "settings", lambda _: False)
    monkeypatch.setattr(library_module, "window", lambda *args, **kwargs: None)
    monkeypatch.setattr(library_module, "set_screensaver", lambda **kwargs: None)
    monkeypatch.setattr(library_module, "get_screensaver", lambda: None)
    monkeypatch.setattr(
        library_module.xbmc, "getCondVisibility", lambda condition: False
    )
    monkeypatch.setattr(
        library_module.xbmc, "executebuiltin", lambda command: builtins.append(command)
    )
    library.save_last_sync = lambda: save_last_sync_calls.append(True)

    library_module.Library.service.__wrapped__(library)

    assert save_last_sync_calls == [True]
    assert "UpdateLibrary(video)" not in builtins
    assert library.deferred_library_update is True


def test_stop_client_clears_deferred_library_update():
    library = _library_instance(is_playing=False)
    library.deferred_library_update = True

    library.stop_client()

    assert library.stop_thread is True
    assert library.deferred_library_update is False
