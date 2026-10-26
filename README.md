# kindle-panel

把 Kindle Oasis 变成自动刷新的墨水屏信息面板。

**公网轻量版**：只放公共数据（天气 / 指数 / 头条 / 每日一句），不含日程与待办，可安全公开托管。

## 工作方式

```
GitHub Actions (每 30 分钟)
      |  generate_panel_public.py
      v
docs/panel.png  -->  GitHub Pages
      ^                    |
      |                    | curl (每 60 秒)
   KOReader 插件  <---------+
```

## 部署

1. 本仓库 `Settings → Pages → Source` 选 `gh-pages` 分支
2. `Actions` 里手动跑一次 `panel` 工作流
3. 面板地址：`https://davidchenliang.github.io/kindle-panel/panel.png`

## Kindle 端

把 `kindle_panel.koplugin` 放进 `koreader/plugins/`，并把 `init.lua` 里的 `PULL_URL` 填成上面的地址，插件会每 60 秒主动拉取。

## 本地运行

```bash
pip install Pillow
python generate_panel_public.py
# 输出 docs/panel.png (1264x1680, 灰度)
```

## 数据源

- 天气：Open-Meteo（免费无 key）
- 指数：腾讯行情 qt.gtimg.cn（免费无 key）
- 头条：RSS（少数派 / IT之家 / BBC 兜底）
