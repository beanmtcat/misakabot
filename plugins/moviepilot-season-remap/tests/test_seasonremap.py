"""Small dependency-free checks for the plugin's rule and template rewrite logic."""

import importlib.util
import sys
import types
from pathlib import Path


def load_plugin_module():
    app = types.ModuleType("app")
    core = types.ModuleType("app.core")
    core_event = types.ModuleType("app.core.event")
    log = types.ModuleType("app.log")
    plugins = types.ModuleType("app.plugins")
    schemas = types.ModuleType("app.schemas")
    schema_types = types.ModuleType("app.schemas.types")

    class DummyEvent:
        pass

    class DummyEventManager:
        @staticmethod
        def register(_event_type):
            return lambda func: func

    class DummyPluginBase:
        def log_info(self, *_args, **_kwargs):
            pass

    class DummyLogger:
        @staticmethod
        def warning(*_args, **_kwargs):
            pass

        @staticmethod
        def info(*_args, **_kwargs):
            pass

    core_event.Event = DummyEvent
    core_event.eventmanager = DummyEventManager()
    log.logger = DummyLogger()
    plugins._PluginBase = DummyPluginBase
    schema_types.ChainEventType = types.SimpleNamespace(TransferRenameBuild="transfer.rename.build")

    sys.modules.update(
        {
            "app": app,
            "app.core": core,
            "app.core.event": core_event,
            "app.log": log,
            "app.plugins": plugins,
            "app.schemas": schemas,
            "app.schemas.types": schema_types,
        }
    )
    module_path = Path(__file__).parents[1] / "seasonremap" / "__init__.py"
    spec = importlib.util.spec_from_file_location("seasonremap", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def main():
    module = load_plugin_module()
    plugin = module.SeasonRemap()
    plugin.init_plugin({"enabled": True, "rules": "334299:S01->S02\n# comment\ninvalid"})

    assert plugin._rules == {(334299, 1): 2}
    event = types.SimpleNamespace(
        event_data=types.SimpleNamespace(
            source_path="/downloads/example.S01E03.mkv",
            rename_dict={
                "type": "电视剧",
                "tmdbid": 334299,
                "season": 1,
                "season_fmt": "S01",
                "episode": "E03",
                "season_episode": "S01E03",
            },
        )
    )
    plugin.remap_season(event)
    assert event.event_data.rename_dict["season"] == 2
    assert event.event_data.rename_dict["season_fmt"] == "S02"
    assert event.event_data.rename_dict["season_episode"] == "S02E03"

    untouched = types.SimpleNamespace(
        event_data=types.SimpleNamespace(
            source_path="/downloads/other.S01E03.mkv",
            rename_dict={"type": "电视剧", "tmdbid": 999999, "season": 1},
        )
    )
    plugin.remap_season(untouched)
    assert untouched.event_data.rename_dict["season"] == 1


if __name__ == "__main__":
    main()
