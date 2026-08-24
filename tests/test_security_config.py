from lunatvsource_test import LunaTVSource
import lunatvsource_test as plugin_module


def test_probe_allowlist_does_not_inherit_image_proxy_settings(monkeypatch):
    monkeypatch.setattr(
        plugin_module,
        "_get_runtime_settings",
        lambda: {"IMAGE_PROXY_ALLOWED_PRIVATE_RANGES": ["10.0.0.0/8"]},
        raising=False,
    )

    plugin = LunaTVSource()
    plugin.init_plugin({"enabled": True})

    assert plugin._probe_allowed_private_ranges() == ()
