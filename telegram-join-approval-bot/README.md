# Telegram Join Approval Bot

一个轻量、真实可部署的 Telegram 入群审批机器人。

它只监听 `chat_join_request`，并按以下顺序快速判定：

1. 昵称：`first_name + last_name + username`
2. `bio`
3. 最新头像 OCR 文本

命中风险词则 `decline`，否则 `approve`。

## 特性

- Python 3.11
- `python-telegram-bot 22.x`
- Webhook 模式
- 本地离线 OCR：`rapidocr-onnxruntime`
- 简繁转换：`opencc-python-reimplemented`
- 只拉取最新 1 张头像
- 只下载 1 个最大尺寸版本
- 头像 OCR 结果做 24 小时内存 TTL 缓存，缓存 key 为 `file_unique_id`
- 默认快速判定，优先检查昵称和 bio，命中后不再 OCR

## 机器人权限

机器人必须在目标群中是管理员，并且至少具备：

- `Invite Users`

否则无法审批入群请求。

如果你开启了 `DECLINE_AND_BAN=true`，则还需要：

- `Ban Users`

## 项目文件

- `app.py`：应用入口与 webhook 启动
- `moderator.py`：审批主逻辑
- `text_normalizer.py`：文本归一化
- `avatar_ocr.py`：头像 OCR 与文字头像判定
- `risk_terms.py`：默认风险词与扩展词加载
- `settings.py`：环境变量配置
- `Procfile`：Railway/Heroku 风格启动入口
- `extra_terms.txt`：可选扩展词文件

## 环境变量

- `BOT_TOKEN`：Telegram Bot Token
- `WEBHOOK_URL`：公网 HTTPS 地址，例如 `https://your-app.up.railway.app`
- `PORT`：监听端口，Railway 默认会注入
- `LOG_LEVEL`：日志级别，默认 `INFO`
- `OCR_ENABLED`：是否启用 OCR，默认 `true`
- `OCR_CACHE_TTL_SECONDS`：头像 OCR 缓存 TTL，默认 `86400`
- `EXTRA_TERMS`：额外词语，逗号分隔
- `DECLINE_AND_BAN`：是否 decline 后再 ban，默认 `false`
- `OPENCC_CONFIG`：OpenCC 配置，默认 `t2s`
- `OCR_MAX_SIDE`：OCR 前头像最长边缩放上限，默认 `512`

## 风险词扩展

默认词表只包含项目内置的种子词。

你可以通过两种方式扩展：

1. 环境变量 `EXTRA_TERMS`
2. 项目根目录新增 `extra_terms.txt`

`extra_terms.txt` 格式为每行一个词，例如：

```txt
示例词1
示例词2
```

## 文字归一化策略

归一化会做这些事：

- Unicode NFKC
- 转小写
- 去空白、制表符、换行
- 去零宽字符、控制字符
- 繁体转简体
- 仅保留汉字、英数字

因此像这些绕过方式会被尽量压成同类文本：

- `幼 女`
- `幼.女`
- `幼_女`
- `蘿 莉`
- `點我`
- `看 我`
- `頭 像`

## 头像规则

机器人只做 OCR 文本识别，不做：

- 人脸识别
- 年龄识别
- NSFW 图像分类
- 任何重模型视觉推理

头像命中拒绝的条件必须同时满足：

1. OCR 后主要是汉字
2. 至少 2 个汉字
3. 命中风险词

否则头像规则不生效。

## 本策略边界

这个项目只做：

- 昵称文本
- `bio` 文本
- 头像 OCR 文本

它不判断真人年龄，不判断图片内容是否是未成年人，不做人脸/视觉身份分析。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Webhook 设置

程序启动时会自动调用 `setWebhook`。

最终 webhook 地址格式是：

```text
{WEBHOOK_URL}/telegram/webhook
```

例如：

```text
https://your-app.up.railway.app/telegram/webhook
```

## Railway 部署

1. 新建 Railway 项目并连接仓库
2. 根目录切换到本项目目录
3. 配置环境变量
4. 部署后确认 `WEBHOOK_URL` 填的是 Railway 分配的公网 HTTPS 域名
5. 让机器人进入目标群并赋予管理员权限
6. 打开群的“加入审批”

### Railway 建议

- 使用 Docker 部署即可
- 免费层足够跑这类 join request 审批
- 这个项目不依赖数据库、Redis、队列、GPU

## 审核日志

日志只记录：

- `user_id`
- `chat_id`
- `nickname_match`
- `bio_match`
- `avatar_match`
- `final_decision`
- `reason`

不会保存头像到磁盘，也不会打印图片内容。
