# -*- coding: utf-8 -*-
"""用 Markdown 接口文档定义工具。

**为什么不用 JSON Schema**:JSON Schema 是给机器看的,人写起来啰嗦、读起来更啰嗦,
而运维手上现成的东西恰恰是一份 Markdown 接口文档(URI、请求方式、参数表、返回参数表、
curl 示例、返回示例)。让人把那份文档翻译成 JSON Schema,既慢又容易译错 ——
译错的表现还特别隐蔽:模型照着错的 schema 传参,接口返回一个业务错误,
看起来像"接口有问题",其实是参数名少了个字母。

所以这里反过来:**把那份文档直接贴进来**,由代码解析成
  - 请求参数 → 给模型看的 JSON Schema(哪些必传、什么类型、什么示例)
  - 返回参数 → 字段字典(给结果压缩层用:知道哪一列是什么,才谈得上筛选和统计)
  - curl 示例 → 认证方式与 header(xpftkey 这类平台私有 header 不必再问一遍)
  - 返回示例 → 数据体在哪一层(data / data.list / rows …),以及一份可回归的样例

`from_markdown()` 是唯一入口,返回一个 `ApiDoc`;`ApiDoc.to_type_config()` 直接
就是 http 工具的 type_config。解析失败会把**哪一节没看懂**说清楚,而不是给一个空壳。
"""
from cogni_agent.tool_center_service.apidoc.parser import (  # noqa: F401
    PLATFORM_SECRET_HEADERS, ApiDoc, ApiField, ApiDocParseError, annotate_params, from_markdown,
    is_credential_header, parse_fields, placeholder_for, to_markdown,
)
