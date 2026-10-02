"""
工具中心 · Quick BI 智能小Q 集成模块(独立维护)。

把 Quick BI 已集成的「智能小Q」能力(问数 / 报表 / 解读 / 报告)封装成平台内的一个
独立模块,供三类入口复用同一套实现:

  1. 管理后台 / 操练台     /api/admin/quickbi/*      (JWT)
  2. 开放平台 API          /v1/quickbi/*             (API Key: sk-zhilv-*)
  3. 执行引擎(Agent/Skill) 工具类型 type="quickbi"    (tools_adapter → invoke_tool)

业务场景:
  - 固定问题:运营预置「问题 × 能力 × 目标数据」,一键/批量执行,结果可追溯;
  - 对话场景:会话粘住目标(数据集/上传文件/仪表板),多轮追问,自动按意图路由能力;
  - 报告生成:异步创建小Q报告,轮询进度,产出可回放的报告链接。

数据来源:
  - 已有仪表板(小Q报表):pageId / 仪表板 URL / 数据门户 URL;
  - 上传的数据:xls/xlsx/csv(≤5MB),问数走 copilot/parse,报告走 uploadReferenceFile,
    解读在本地转成 Markdown 表格后提交;
  - 数据集:cubeId,或不指定时走小Q智能选表。

协议对齐阿里云官方开源 skill:alibabacloud-quickbi-smartq v1.3.0
(github.com/aliyun/alibabacloud-aiops-skills,MIT/Apache-2.0)。模块内文件分层:

  signing.py      网关签名(X-Gw-*)                      纯函数,无外部依赖
  client.py       OpenAPI 客户端(httpx,含 SSE 流)       只依赖 httpx
  events.py       SSE 事件 → 平台统一事件                纯函数
  dashboard.py    仪表板 / 数据门户解析                   纯函数
  filesupport.py  Excel/CSV ↔ Markdown/CSV 转换           openpyxl 可选
  capabilities.py 能力编排(问数/解读/报告/报表)          只依赖 client
  models.py       连接 / 资产 / 文件 / 固定问题 / 会话 / 运行记录
  service.py      带持久化的业务编排(运行记录、会话、固定问题、报告刷新)
  tool_type.py    工具中心 quickbi 工具类型(模板 / Schema / 执行)
  routes_admin.py 管理端路由     routes_open.py 开放平台路由
"""
