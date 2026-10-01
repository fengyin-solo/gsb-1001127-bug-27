# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 告警对账台 | `reconciliation` | 排水设施主档案/风险面/告警 | 设施编号、水位阶段、可通行性、根因 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 告警对账台口径

排水设施的风险面、处置通知、防汛事件原先各自给出水位阶段，泵站离线后风险面
还会停留在“可通行”。对账台（前端“告警对账台”，后端 `/api/reconciliation`）把口径统一为：

- **离线即封路**：存在待处置的“泵站离线”告警时，风险面一律 `超警戒·不可通行`。
- **人工复测优先**：同一泵站多次告警，以最近一次“人工复测”读数为准；即使之后又到了
  更晚的自动采集，也不推翻人工结论。
- **历史水位冻结**：水位阶段（低/中/高/超警戒）在采集时刻计算并冻结，后续重算只引用、不回改。
- **处置即对账回写**：现场处置在同一事务里核销处置通知、结束防汛事件、重算风险面，
  并回写排水台账（`drainage`）、防汛清单（`flood`）和路段风险看板（`road_risk`，按路段空间聚合）。
- **并发处置**：按设施加锁，同一告警只保留一个有效结论，后到的处置被拒绝。
- **失败不归零**：重建任务按“设施 + 版本”幂等；重算任一步失败整体回滚，保留上一版活动风险面。
- **空号迁移**：空设施号按“设施类型 + 所属路段 + 桩号位置”归并到既有主档案，
  同批多条只升一次版本，匹配不上才新建主档案。
- **根因一处算、多处看**：根因只在对账服务里计算，排水设施列表和详情透出同一份结果。

后端验证：

```bash
cd backend
python3 -m unittest app.tests.test_reconciliation   # 12 条业务规则
python3 -m app.tests.smoke_reconciliation           # 27 项 HTTP 端到端校验
```
