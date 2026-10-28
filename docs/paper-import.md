# 论文精读文章维护

这批内容来自本机已经完成的单篇精读报告，不重新调用 API，不读取 `~/.env`，也不包含整份 PDF、模型响应缓存或账户信息。图片从原 PDF 选页渲染，保留物理页码和版权归属，不生成替代原实验的示意数据。

## 文件位置

- `content/posts/2026-10-22-paper-*/index.md`：每篇独立文章和相邻导航。
- 各文章目录的 `source-page-*.png`：原页截图，点击可看原尺寸。
- `content/posts/2026-10-22-database-papers-source-reading/index.md`：按主题组织的总索引。
- `data/paper-reading-import.json`：相对来源、PDF 和报告指纹、截图页码、生成文件指纹。没有本机绝对目录或密钥。
- `scripts/import_paper_reports.py`：可重复执行的本地转换器。

正文顺序为研究问题、机制、正确性、实验、取舍与适用边界，再保留全部原文精读与证据索引。沿用原报告的不确定性；截图的增加不代表自动复核了其中全部图表和公式。

英文原文用经过 HTML 转义的 `blockquote/pre/code` 保持符号与换行，样式限定于 `paper_reading` 文章，显示为可换行正文。这可避免 Hugo 的数学透传和 MathJax 将原文括号、下划线误当公式；不要将此处引文改成未经转义的 HTML。

原 PDF 提取文本中的不可见控制字符用 `[U+0000]` 一类明确标记显示，不能补猜成变量或公式。原始报告保持不变；验收时仅将这一明确的显示转义计入比较，不模糊匹配其他符号。原文换行编码为 HTML 数字实体，避免空行使 Markdown 解析器提前结束原文块。

## 再次导入

在 WSL 的博客仓库中运行。依赖 Python 3.10+ 与 PyMuPDF；当前机器可使用已经安装的研究程序虚拟环境，不必另装一套模型框架。

```bash
PYTHON=/home/lism/.local/share/db-paper-research/.venv/bin/python
PAPERS=/mnt/c/Users/JoJo/Documents/Documents/Documents/Papers
REF=4ff1331069c7cf52ac97dd769e5f484c1c35c200

# 默认仅内存渲染和检查，不写博客；加 --write 才导入。
"$PYTHON" scripts/import_paper_reports.py \
  --source "$PAPERS/db-papers/papers" \
  --reports "$PAPERS/db-papers-deep-research" \
  --source-index "$PAPERS/db-papers/README.md" \
  --source-url "https://github.com/lonng/db-papers/blob/$REF/papers" \
  --date 2026-10-22 --expected 67
```

新批次使用新日期与输出路径，先确认来源索引的公开仓库和固定提交。公开 URL 来自论文集 README 的原路径，不根据 Windows 改名后的文件名猜测。缺失映射会在命令输出中明确列出，不会伪造链接。

导入前核对 PDF 指纹和物理页数；拒绝来源、报告与博客目录重叠。若文件已被人工编辑，转换器拒绝覆盖；不会删除上一批文章。图片页数或文件集合发生变化时需要单独审查迁移，不自动清理旧文件。生成前完成冲突检查，但不是跨文件事务；运行中断可在未手工编辑的情况下重跑。

对同一批次再次运行可加 `--reuse-images`：只有 PDF、报告及图片指纹都匹配才复用，避免重复渲染；正文仍重新生成并检查人工改动。

## 验证与预览

```bash
"$PYTHON" -m unittest discover -s scripts -p 'test_*.py'
/home/lism/go/bin/hugo --buildDrafts --destination /tmp/lishuming-paper-preview
/home/lism/go/bin/hugo server --buildDrafts --bind 127.0.0.1 --port 1315
```

构建后可运行自动验收，检查本地页面、图片、锚点以及原文在 HTML 中是否保留。若另有不带草稿的生产构建，传入 `--production-build` 验证草稿未进入生产结果：

```bash
"$PYTHON" scripts/validate_paper_posts.py \
  --build /tmp/lishuming-paper-preview \
  --reports "$PAPERS/db-papers-deep-research"
```

`--buildDrafts` 会包含仓库原有草稿。此次文章全部 `draft: true`，普通生产构建不会发布它们。本地入口为 `/2026/10/22/database-papers-source-reading/`。预览端口按实际可用端口调整。

构建只验证站点模板与链接解析，不证明论文实验可复现。原页截图的自动选页依据图注、正文引用页和绘图对象数量；对没有图的理论文章会保留必要文字页。这是页面定位策略，不是视觉模型识别。正式发布仍需人工检查引用比例、论文图片使用权和相关数值。

## 发布前

1. 逐篇检查书目信息、推断和实验前提，核对图片是否确有说明价值。
2. 检查论文短引与截图的使用权和引用比例。本文保留详细精读供本地使用，不自动认定可全部公开转载。
3. 决定保留哪些精读内容作为正文，哪些改为少量摘引。不要直接把原始 PDF 或整本论文上传到博客。
4. 将准备好的文章及其总索引单独改为 `draft: false`，并重新构建、检查导航。此类手工编辑后，导入器会保护文章，不自动覆盖。
5. 自行审查提交内容再推送；脚本没有提交、推送或部署操作。

## 本批验收记录：2026-10-22

- 67 篇精读和 1 篇分类索引，19 个主题分组，全部为草稿。
- 131 张原页截图，合计约 49 MiB 的文章与图片；没有复制完整 PDF。
- 1,252 组原文精读及 3,244 条证据在 Hugo 生成的 HTML 中逐项匹配通过；不可见控制字符按上文规则显式转义。
- 67 个论文来源链接完成映射；检查了站内链接、图片及锚点，没有检查所有外部地址的实时可用性。
- 23 项离线单元测试通过，Hugo 0.160.1 草稿与生产构建成功；生产结果不包含这 68 个草稿页。CI 配置仍使用仓库原有 0.164.0，本次未修改或运行远端 CI。
- 131 张截图完成文件指纹、尺寸和非空白检查；浏览器抽查索引与 Cascades 精读排版。不将抽查称为对全部图表数值的视觉复核。
- 没有调用新的模型、读取密钥、复现实验、写入 Notion、提交 Git、推送或部署。原 PDF 和已完成报告保持不变。
