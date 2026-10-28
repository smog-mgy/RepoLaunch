# 小样本真实仓库验证

日期：2026年10月26日。三项均通过真实模型规划、固定提交下载、Docker构建、HTTP检查及示例内容探针；完成后停止测试容器。模型为用户配置的SiliconFlow DeepSeek-V4.1-Flash。

| 仓库 | 固定提交 | 总耗时 | 规划Token | 内容探针 |
|---|---|---:|---:|---|
| [render-examples/flask-hello-world](https://github.com/render-examples/flask-hello-world) | `facfc73c44b35d00bb1f72e8986f0ea47cab0755` | 28.02秒 | 985 | 通过 |
| [digitalocean/sample-flask](https://github.com/digitalocean/sample-flask) | `3f2269883cf430cf93352a832da80fee4094f159` | 35.3秒 | 3538 | 通过 |
| [render-examples/fastapi](https://github.com/render-examples/fastapi) | `f829276c68503f7afae195c3e3f778f085242cb0` | 148.77秒 | 1849 | 通过 |

Render Flask Hello World先在SQLite/本地检索模式验证，历史随后迁入MySQL。DigitalOcean Flask与Render FastAPI在MySQL、真实Milvus检索及Langfuse SDK配置下执行。Flask检查页面内容，FastAPI还检查/items/7?q=test的JSON字段。报告中的Token仅指对应规划请求，不是账号完整用量或账单。

这三个项目是经过选择的简单样例，不是随机抽样或任意仓库成功率。首次FastAPI安装fastapi[all]额外依赖较多，耗时包含下载和构建。没有宣称业务测试完整覆盖。

## 已观察到的支持边界

另一个已有任务因目标仓库自身的setuptools打包发现配置失败，错误为Multiple top-level packages discovered in a flat-layout。该失败不计入上述三个样例。当前受控修复只允许补充有明确缺模块证据的依赖、或有项目版本依据的Python调整；不自动修改目标仓库源码或打包声明，不将这类失败标记为成功。
