# -*- coding: utf-8 -*-
"""cogni_agent 顶层包。

在包导入的最早阶段强制 LiteLLM 使用随包自带的本地计费表,从根上消除对
GitHub(raw.githubusercontent.com)的实时拉取——litellm 默认在进程启动时会尝试
下载 model_prices_and_context_window.json,网络抖动时会造成秒级挂起甚至请求
超时(conv_7ef087ef... 事故根因)。本包内任何模块 import litellm 之前都会先执行
本文件,因此这里设置环境变量一定先于 litellm 生效。

模型单价不依赖 litellm 计费表:统一由「系统设置 · 模型单价」页面(表
gov_llm_model_prices)配置,升级 litellm 即同步更新本地备份表,无需任何外部网络。
"""
import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

__all__: list[str] = []
