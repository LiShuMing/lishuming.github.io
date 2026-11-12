# Vipassana

[lishuming.github.io](https://lishuming.github.io/) — Hugo 个人站。作者署名 **LiShuMing**。

原站点基于 Jekyll，现已迁移至 Hugo。站名 Vipassana；页脚与 About 不再使用 Mr.Think / 柠檬树 / alemontree 作为对外署名。

## 目录结构

```
.
├── hugo.toml              # 站点配置
├── content/               # 内容
│   ├── posts/             # 博客文章
│   ├── about.md           # 关于页面
│   └── links.md           # 友链页面
├── layouts/               # 站点级模板覆盖（标题、首页摘要等）
├── static/                # 静态资源（CSS/JS/图片）
└── themes/aphasia/        # 主题
```

## 本地预览

```bash
hugo server --disableFastRender
```

## 数据库经典论文精读

67 篇数据库经典文献已按演化脉络整合为 1 篇总纲索引（`content/posts/2026-10-22-database-papers-source-reading.md`）与 10 篇专题精读长文（`content/posts/2026-10-22-db-papers-*.md`），配套矢量架构与状态流转图位于 `static/images/diagrams/paper-*.svg`。
