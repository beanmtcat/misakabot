# MoviePilot 剧集季号修正插件

为少数季号被资源文件错误标记的剧集提供精确的自动整理路径修正。

## 规则格式

在插件设置中每行填写一条：

```text
TMDB_ID:S原季号->S目标季号
334299:S01->S02
```

支持 `->` 或 `→`；`#` 后可写注释。规则必须同时匹配 TMDB ID 与原季号，避免全局替换误伤其他剧集。

## 生效范围

插件监听 MoviePilot 的 `TransferRenameBuild` 事件，在自动整理生成目标目录及文件名之前改写模板变量：`season`、`season_fmt`、`season_episode`，并在可用时更新 `season_year`。

它不移动源文件，也不修改 MoviePilot 转移历史的原始识别结果。因此，从历史记录重新整理时，必须关闭“复用历史识别信息”，否则历史中的旧季号会覆盖手工选择。

## 安装

将 `seasonremap` 目录作为 MoviePilot 插件仓库中的一个插件目录安装。插件目录名必须为 `seasonremap`，入口文件为 `seasonremap/__init__.py`。

安装后在 MoviePilot 的插件页启用“剧集季号修正”，填写规则并保存；先用“预览”确认目标路径为预期的 `S02`，再放行自动整理。
