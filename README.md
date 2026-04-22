# claude-multi-provider-launcher

单文件的 **Claude Code** 多模型启动器 + 本地反向代理。

一个脚本搞定:统一管理多个 OpenAI / Anthropic 兼容的模型中转,通过交互式菜单选一个模型,自动拉起本地反代并以正确的环境变量启动 `claude`。

## 特性

- 🎯 **单文件 / 零依赖** — 只需 Python 3.10+,标准库搞定,不需要 pip install
- 🎨 **交互式菜单** — 圆角边框 + 分组配色,清晰易读
- 🧩 **多提供商管理** — 在菜单里直接新增 / 编辑 / 删除 provider 和 model
- 🔍 **自动拉取模型** — 填好 URL + key 之后点 `F`,自动请求 `/v1/models` 拉出可用模型,多选勾选
- 🔐 **零配置入库** — 本仓库代码里不含任何 URL 或 api_key,所有配置存在本地 `~/.claude-launcher/providers.json`
- 🪶 **轻量反代** — 本地 `127.0.0.1:8888`,按 `model` 字段路由到对应上游

## 快速开始

```bash
# 1. 下载脚本
curl -LO https://raw.githubusercontent.com/PhoenixHairpin/claude-multi-provider-launcher/master/claude-launch.py
chmod +x claude-launch.py

# 2. 启动 (首次会引导你添加第一个 provider)
python3 claude-launch.py
```

第一次运行时菜单里是空的,输入 `M` 进入管理,再按 `N` 添加第一个提供商。

## 命令

| 命令 | 作用 |
|------|------|
| `python3 claude-launch.py` | 主菜单:选一个模型 → 启动 Claude Code |
| `python3 claude-launch.py --manage` | 直接进入提供商管理 |
| `python3 claude-launch.py --list` | 纯文本打印所有已配置的 providers / models |
| `python3 claude-launch.py --proxy` | 仅以反代模式运行(内部用,也可手动调试) |

## 界面示例

**主菜单:**

```
╭────────────────────────────────────────────────────────────────────╮
│   Claude Code  ·  模型选择启动器                                    │
│   选择编号启动 Claude Code,  输入 M 进入管理菜单                    │
├────────────────────────────────────────────────────────────────────┤
│   ▎ 国产模型  ·  aihubmix                                           │
│      1)  MiniMax-M2.5               http://...                      │
│      2)  glm-5                      http://...                      │
│      3)  kimi-k2.5                  http://...                      │
│                                                                     │
│   ▎ Claude Opus 4.7  ·  xxx                                         │
│      4)  claude-opus-4-7-low        https://...                     │
│      5)  claude-opus-4-7-max        https://...                     │
├────────────────────────────────────────────────────────────────────┤
│     M)  管理提供商 / 模型                                           │
│     0)  退出                                                        │
╰────────────────────────────────────────────────────────────────────╯
```

**提供商编辑页:** `A` 手动加模型 / `F` 从 API 自动拉 / `R <n>` 删 / `M` 改元数据 / `X` 删整个 provider。

## 添加一个新提供商

1. 主菜单按 `M` → `N`
2. 依次填写:
   - **显示名** — 例如 `MyProv  ·  某中转`
   - **颜色** — 从调色板选,或直接填 ANSI code(如 `35`、`1;36`)
   - **base_url** — 不带末尾 `/`,例如 `https://api.example.com`(如果供应商把 Anthropic 接口放在 `/anthropic` 下,就填到 `https://api.example.com/anthropic`)
   - **api_key** — 供应商给的 `x-api-key` 或 Bearer token
3. 问你「是否自动拉取模型列表」时选 `Y`
   - 成功 → 多选勾选想加的(支持 `1,3,5-8` / `a` 全选)
   - 失败 → 会打印每个试过的端点和状态码,你再手动填 model id

### 自动拉取规则

脚本会依次尝试以下端点,同时发送 `Authorization: Bearer` 和 `x-api-key` 两种鉴权:

1. `{base_url}/v1/models`
2. `{base_url}/models`
3. 如果 `base_url` 以 `/anthropic` 结尾,还会去掉后缀再试 `.../v1/models` 和 `.../models`

## 工作原理

```
┌──────────────┐    /v1/messages     ┌──────────────────┐    x-api-key     ┌─────────────┐
│ Claude Code  │ ──────────────────▶│  claude-launch   │ ───────────────▶│  上游供应商  │
│  (claude)    │  model: xxxx       │  反代 :8888      │                  └─────────────┘
└──────────────┘                    │                  │
                                    │  model → (url,   │
                                    │     api_key)     │
                                    │     路由表        │
                                    └──────────────────┘
```

1. 启动器把所有 provider 的 `(model_id → base_url, api_key)` 装进内存路由表
2. 通过 `--settings` 注入 `ANTHROPIC_BASE_URL=http://127.0.0.1:8888` 启动 `claude`
3. Claude Code 的请求里自带模型名,反代据此路由到对应上游

## 配置文件

- 路径: `~/.claude-launcher/providers.json`(首次运行自动创建为空数组)
- 格式:

```json
[
  {
    "label": "MyProv  ·  xxx",
    "color": "36",
    "base_url": "https://api.example.com",
    "api_key": "sk-...",
    "models": ["model-a", "model-b"]
  }
]
```

⚠️ 该文件包含明文 api_key,请注意权限和同步策略,**不要**把它提交到任何 Git 仓库。

## 依赖

- Python 3.10+(标准库即可)
- `claude` CLI 在 `$PATH` 中

## 许可

[MIT](./LICENSE)
