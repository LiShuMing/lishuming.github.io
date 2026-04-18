# Aphasia

lishuming's website, powered by [Hugo](https://gohugo.io/).

原站点基于 Jekyll，现已迁移至 Hugo。

## 目录结构

```
.
├── hugo.toml              # 站点配置
├── content/               # 内容
│   ├── posts/             # 博客文章
│   ├── about.md           # 关于页面
│   └── links.md           # 友链页面
├── static/                # 静态资源（CSS/JS/图片）
│   ├── assets/css/        # 样式文件
│   ├── assets/images/     # 图片
│   ├── media/js/          # JavaScript
│   └── lsm.ico            # 站点图标
├── themes/aphasia/        # 自定义主题
│   └── layouts/           # HTML 模板
└── .github/workflows/     # GitHub Actions 自动部署
```

## 本地构建

### 安装 Hugo

```bash
# macOS
brew install hugo

# Ubuntu/Debian
sudo apt-get install hugo

# 或使用 Go 安装
go install github.com/gohugoio/hugo@latest
```

### 预览

```bash
hugo server -D
# 访问 http://localhost:1313
```

### 构建

```bash
hugo
# 构建产物输出到 public/ 目录
```

## 发布新文章

```bash
hugo new content posts/文章标题.md
```

Front Matter 示例：

```yaml
---
title: 文章标题
date: 2024-01-01T00:00:00+08:00
categories:
  - 分类名称
tags:
  - 标签名称
---
```

## 部署

本仓库配置了 GitHub Actions，推送代码到 `master` 分支后会自动构建并部署到 GitHub Pages。

## 迁移记录

- **原框架**: Jekyll + GitHub Pages
- **新框架**: Hugo + GitHub Actions
- **迁移日期**: 2025-04-18
- **保留内容**: 全部 13 篇博客文章、About 页面、Links 页面、CSS 样式、图片资源
- **保留功能**: 分类/标签、分页、Disqus 评论、键盘翻页导航
