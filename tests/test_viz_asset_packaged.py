import importlib.resources as resources


def test_vis_network_asset_is_resolvable():
    asset = resources.files("sidegraph.viz").joinpath("assets", "vis-network.min.js")
    assert asset.is_file()
    text = asset.read_text(encoding="utf-8")
    assert len(text) > 400_000  # the real minified library, not a stub
    assert "vis" in text
