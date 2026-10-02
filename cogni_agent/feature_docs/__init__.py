"""管理端「系统功能说明」文档源(markdown,随镜像发布)。

目录约定:
- 每个功能一篇 `*.md`,文件名即 doc_id(URL 用);
- frontmatter(YAML 子集,`key: value` 行)必填:title / category / summary,
  可选:related(关联代码文件,逗号分隔——代码改动时按此索引同步更新本文档);
- 正文为标准 markdown(前端用 ReactMarkdown 渲染,支持表格/代码块)。

同步铁律:related 列出的代码文件有行为级改动时,同 PR 更新对应文档
(页面顶部展示 related 文件清单,评审时可见对照)。
"""
