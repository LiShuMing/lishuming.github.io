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
hugo server -D
```

## 论文精读

2026-10-22 的数据库论文精读已整理为独立文章草稿、原页截图和分类索引。可用 `hugo server -D` 预览；普通构建不发布草稿。

转换器、离线检查、后续批次与发布前审查见 [论文文章维护](docs/paper-import.md)。
