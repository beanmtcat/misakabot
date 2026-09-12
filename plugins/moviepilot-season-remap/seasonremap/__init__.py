"""MoviePilot plugin: remap a TV season while MoviePilot renders transfer paths.

Rules are deliberately scoped to a TMDB ID and an already-recognised source season.
This prevents a broad ``S01 -> S02`` rule from affecting unrelated shows.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType


class SeasonRemap(_PluginBase):
    """Correct the season variables used by MoviePilot automatic transfers."""

    plugin_name = "剧集季号修正"
    plugin_desc = "按 TMDB ID 将自动整理中的指定季号改写为目标季号。"
    plugin_icon = "mdi-calendar-sync"
    plugin_color = "#5C8DFF"
    plugin_version = "0.1.0"
    plugin_author = "MisakaBot"
    author_url = ""
    plugin_config_prefix = "seasonremap_"
    plugin_order = 25
    auth_level = 1

    _enabled: bool = False
    _rules_text: str = ""
    _rules: Dict[Tuple[int, int], int] = {}

    _RULE_PATTERN = re.compile(
        r"^(?:tmdb\s*)?(?P<tmdb>\d+)\s*:\s*"
        r"S?(?P<source>\d+)\s*(?:->|→)\s*S?(?P<target>\d+)\s*$",
        re.IGNORECASE,
    )

    def init_plugin(self, config: Optional[dict] = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._rules_text = str(config.get("rules", "") or "")
        self._rules = self._parse_rules(self._rules_text)
        if self._enabled:
            logger.info("已加载 %s 条剧集季号修正规则", len(self._rules))

    def get_state(self) -> bool:
        return self._enabled

    @classmethod
    def _parse_rules(cls, rules_text: str) -> Dict[Tuple[int, int], int]:
        """Parse one rule per line: ``TMDB_ID:S01->S02``."""
        rules: Dict[Tuple[int, int], int] = {}
        for line_number, raw_line in enumerate(rules_text.splitlines(), start=1):
            line = raw_line.split("#", maxsplit=1)[0].strip()
            if not line:
                continue
            matched = cls._RULE_PATTERN.match(line)
            if not matched:
                logger.warning(
                    "剧集季号修正规则格式无效，第 %s 行已忽略：%s",
                    line_number,
                    raw_line,
                )
                continue
            tmdb_id = int(matched.group("tmdb"))
            source_season = int(matched.group("source"))
            target_season = int(matched.group("target"))
            if source_season < 0 or target_season < 0:
                logger.warning("剧集季号修正规则季号不能小于 0，第 %s 行已忽略", line_number)
                continue
            rules[(tmdb_id, source_season)] = target_season
        return rules

    @staticmethod
    def _number(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        matched = re.fullmatch(r"\s*(?:S)?0*(\d+)\s*", str(value), re.IGNORECASE)
        return int(matched.group(1)) if matched else None

    @classmethod
    def _tmdb_id(cls, rename_dict: Dict[str, Any]) -> Optional[int]:
        for key in ("tmdbid", "tmdb_id"):
            tmdb_id = cls._number(rename_dict.get(key))
            if tmdb_id is not None:
                return tmdb_id
        mediainfo = rename_dict.get("__mediainfo__")
        return cls._number(getattr(mediainfo, "tmdb_id", None))

    @staticmethod
    def _is_tv(rename_dict: Dict[str, Any]) -> bool:
        media_type = rename_dict.get("type")
        if media_type is not None:
            return str(media_type) == "电视剧"
        mediainfo = rename_dict.get("__mediainfo__")
        media_type = getattr(mediainfo, "type", None)
        return getattr(media_type, "value", media_type) == "电视剧"

    @staticmethod
    def _season_episode(season_format: str, episode: Any) -> str:
        if episode is None or not str(episode).strip():
            return season_format
        episode_text = str(episode).strip()
        if not episode_text.upper().startswith("E"):
            episode_text = f"E{episode_text.zfill(2)}"
        return f"{season_format}{episode_text}"

    @staticmethod
    def _season_year(rename_dict: Dict[str, Any], target_season: int) -> Optional[Any]:
        mediainfo = rename_dict.get("__mediainfo__")
        years = getattr(mediainfo, "season_years", None)
        if not isinstance(years, dict):
            return None
        return years.get(target_season, years.get(str(target_season)))

    @eventmanager.register(ChainEventType.TransferRenameBuild)
    def remap_season(self, event: Event):
        """Update template fields immediately before MoviePilot renders destination paths."""
        if not self.get_state() or not event or not event.event_data:
            return

        event_data = event.event_data
        rename_dict = event_data.rename_dict
        if not isinstance(rename_dict, dict) or not self._is_tv(rename_dict):
            return

        tmdb_id = self._tmdb_id(rename_dict)
        source_season = self._number(rename_dict.get("season"))
        if tmdb_id is None or source_season is None:
            return
        target_season = self._rules.get((tmdb_id, source_season))
        if target_season is None or target_season == source_season:
            return

        season_format = f"S{target_season:02d}"
        rename_dict["season"] = target_season
        rename_dict["season_fmt"] = season_format
        rename_dict["season_episode"] = self._season_episode(
            season_format, rename_dict.get("episode")
        )
        season_year = self._season_year(rename_dict, target_season)
        if season_year is not None:
            rename_dict["season_year"] = season_year
        event_data.rename_dict = rename_dict

        logger.info(
            "自动整理季号已修正：TMDB %s，S%02d -> S%02d，文件：%s",
            tmdb_id,
            source_season,
            target_season,
            getattr(event_data, "source_path", "-"),
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用剧集季号修正",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rules",
                                            "label": "季号修正规则",
                                            "rows": 8,
                                            "placeholder": "每行一条：TMDB_ID:S01->S02\n例如：334299:S01->S02",
                                            "hint": "仅在自动整理的识别 TMDB ID 和原季号同时匹配时生效；# 后的内容为注释。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "插件只改自动整理时模板使用的季号、Sxx 和 SxxExx 字段，不改源文件。转移历史仍保留原识别季号；重新整理时请关闭“复用历史识别信息”。",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {"enabled": False, "rules": ""}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        """The plugin has no background job or open resource to stop."""
        pass
