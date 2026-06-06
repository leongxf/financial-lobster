# Financial Lobster

面向四大投资建议/交易咨询人员的外部网络个人 AI 助理 MVP。

当前阶段先验证飞书机器人入口，优先使用飞书官方 SDK 长连接接收事件：

1. 用户向飞书机器人发送文件。
2. 飞书通过 SDK 长连接将消息事件推送到本地 worker。
3. 后端识别文件消息。
4. 机器人回复“已收到文件，正在分析”。

## 本地启动

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --app-dir backend --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 飞书长连接

启动长连接 worker：

```bash
.venv/bin/python -m app.workers.feishu_ws
```

飞书开放平台事件订阅方式选择：

```text
使用长连接接收事件
```

## HTTP 回调备选

如果不用长连接，也可以配置飞书事件回调地址：

```text
https://your-domain.example.com/api/feishu/events
```

## 飞书配置

需要在飞书开放平台应用中配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- 事件订阅方式：优先选择“使用长连接接收事件”
- HTTP 回调备选地址：`/api/feishu/events`
- 权限：接收消息、读取消息文件、回复消息

MVP 上传入口是飞书机器人消息。H5/Web 上传不是 MVP 入口。
