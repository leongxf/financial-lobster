# Financial Lobster

面向四大投资建议/交易咨询人员的外部网络个人 AI 助理 MVP。

当前阶段先验证飞书机器人入口，优先使用飞书官方 SDK 长连接接收事件：

1. 用户向飞书机器人发送文件。
2. 飞书通过 SDK 长连接将消息事件推送到本地 worker。
3. 后端识别文件消息。
4. 机器人回复“已收到文件，正在分析”。

## 本地开发

本地直接运行，不需要 Docker。

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e .
PYTHONPATH=backend .venv/bin/python -m app.workers.feishu_ws
```

飞书开放平台事件订阅方式选择：**使用长连接接收事件**。

## 远端部署（Docker）

服务器上 clone 项目并配置 `.env` 后，用脚本一键部署（默认会先 `git pull` 再构建）：

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

跳过代码更新、仅重建容器：

```bash
./scripts/start.sh --no-pull
```

也可手动执行：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f feishu-worker
```

数据目录 `./storage` 会挂载到容器内，用于上传文件、任务 JSON 与分片缓存。

停止服务：

```bash
docker compose down
```

## 飞书配置

需要在飞书开放平台应用中配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- 事件订阅方式：**使用长连接接收事件**
- 权限：接收消息、读取消息中的文件、回复消息、**更新消息（im:message:update，进度卡需要）**
- 自定义菜单：可添加 `event_key=financial_summary`、文案「文件摘要」，点击后发上传引导卡

MVP 上传入口是飞书机器人消息。H5/Web 上传不是 MVP 入口。
