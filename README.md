# claude-multi-provider-launcher

一个单文件的 **Claude Code** 多模型启动器 + 反向代理。

- 把多家 OpenAI / Anthropic 兼容的模型中转站统一到一个交互式菜单里
- 启动时选一个模型,脚本会拉起本地反代 `127.0.0.1:8888` 并以正确的 `ANTHROPIC_BASE_URL` 启动 `claude`
- 所有提供商 / 模型可以在菜单里交互式增删改
- 支持从 provider 接口自动拉取模型列表 (`/v1/models` → `/models`,兼容 Bearer / x-api-key 两种鉴权)

## 文件

- `claude-launch.py` — 唯一脚本。代理、菜单、管理页、模型发现都在里面。
- 配置文件: `~/.claude-launcher/providers.json`,首次运行自动创建为空列表。

## 依赖

- Python 3.10+(标准库即可,`urllib` + `http.server` + `socketserver`)
- 一个 `claude` CLI 可执行文件在 `$PATH` 中

## 用法

```bash
python3 claude-launch.py            # 主菜单: 选模型 → 启动 Claude Code
python3 claude-launch.py --manage   # 直接进入提供商管理
python3 claude-launch.py --list     # 纯文本打印已配置的 providers/models
python3 claude-launch.py --proxy    # 仅以反代模式运行 (内部用,也可手动调试)
```

## 交互式管理

主菜单输入 `M` 进入提供商管理:

- `N` 新增提供商 — 引导输入 label / 颜色 / base_url / api_key,随后询问是否从接口自动拉取模型
- `E <n>` 或直接输入 `<n>` — 进入该提供商编辑页
- 编辑页:
  - `A` 手动新增模型 (逗号/空格分隔,一次多个)
  - `F` 从 API 自动拉取并追加模型,失败时会把每个端点的尝试结果打出来
  - `R <n>` 删除指定编号模型
  - `M` 修改 label / 颜色 / base_url / api_key
  - `X` 删除整个提供商

多选格式: `1,3,5-8` / `a`(全选) / `0`(取消)

## 工作原理

1. 启动器把所有 provider 的 `(model_id → base_url, api_key)` 索引加载到本地反代
2. 通过 `--settings` 注入 `ANTHROPIC_BASE_URL=http://127.0.0.1:8888`,`ANTHROPIC_MODEL=<选中的 model>` 启动 `claude`
3. Claude Code 发来的请求里带着模型名,反代据此路由到对应上游并转发响应

## 隐私

仓库里不包含任何 provider URL 或 api_key。`providers.json` 在本地磁盘,建议按你平时习惯处理(别提交进其它项目)。

## 许可

MIT
