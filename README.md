# PokerHub 初级 Demo

双人抽牌比点。不登录就能和电脑打。注册后可以改牌组、匹配朋友、留下历史。

胜负只由服务器计算。网页只负责按钮和展示。

## 依赖

Python 3.11 及以上。

```bash
pip install -r requirements.txt
```

| 包 | 用途 |
| --- | --- |
| fastapi | 网页和接口 |
| uvicorn | 启动服务 |
| websockets | 浏览器对战连接，不能省略 |
| sqlalchemy | 用户、牌组和历史 |
| pydantic | 检查请求数据 |
| httpx | 只在运行自检时用到 |

## 启动

```bash
python -m backend.main
```

浏览器打开 http://127.0.0.1:8000 ，点「开始游戏」。

自检（可选）：

```bash
python -m backend.demo
python -m backend.smoke
```

## 免登录人机接口

不需要账号，也不写入数据库。`POST /api/test/solo` 开一局，响应里有 `play_id` 和你的手牌。

```bash
POST /api/test/solo
GET  /api/test/solo/{play_id}
POST /api/test/solo/{play_id}/draw
POST /api/test/solo/{play_id}/play
POST /api/test/solo/{play_id}/surrender
```

出牌的请求体：

```json
{"card": "SA"}
```

`SA` 是黑桃 A，`H10` 是红心 10。牌不在手里时，接口返回 400。

## 规则

每人 10 张牌。服务器开局各发 3 张。回合内可以抓 1 张，手牌最多 5 张，然后打出 1 张。双方都出牌后比点数，A 最大，2 最小，花色不计。先到 3 分获胜。轮到你时手里和牌组都空了，判负。投降立刻判负。

## 代码从哪读

| 内容 | 文件 |
| --- | --- |
| 发牌、抓牌、合法性、胜负 | `backend/engine.py` |
| 免登录人机接口 | `backend/main.py` 里的 `/api/test/solo` |
| 注册、匹配、历史 | `backend/main.py` |
| 八个按钮 | `frontend/app.js` |
