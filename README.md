# 餐桌安全食品追溯服务

这是一个面向农产品监管部门、检测实验室和蔬菜配送企业的模块化后端，集中管理供应商、蔬菜批次、抽样检测、农残限值、运输链、风险处置、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 农产品档案：登记供应商、种植地、蔬菜批次和追溯标识。
- 检测业务：登记抽样、实验室结果、农残限值和风险判定。
- 运输追踪：记录装车、转运、到货、温度与异常处置。
- 风险协同：支持批次隔离、召回、监管公告和跨部门办理。
- 供应商风险画像：合并历史超标、温控异常、整改逾期与投诉四类因子，按规则版本做时间衰减评分；高风险走人工复核后发布等级与抽检比例，评分版本记录输入事件、规则版本与逐因子解释；供应商仅可合并或停用，历史不丢失。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 供应商风险画像

风险画像由 `app/risk/` 提供，评分引擎是纯函数（`app/risk/rules.py`），数据与流程在 `app/risk/service.py`。

- 因子与权重：历史超标、温控异常、整改逾期、投诉四类因子，各自带基础权重、严重度分段系数；越旧的事件按 180 天半衰期指数衰减（规则可配置，默认仅统计近 720 天）。
- 触发时机：检测结果（合格或超标）、温控异常到达时，在同一事务内登记事件并重算评分；检测超标会自动开立整改单，到期未闭环自动计入“整改逾期”，闭环后该逾期事件作废并立即重算。
- 唯一确定版本：每次评分把规则版本、供应商合并链、输入事件快照计算成输入指纹；指纹相同不产生新版本。每个版本记录逐事件贡献（`risk_score_events`）与完整解释 JSON。新草稿产生时旧草稿自动置为 `superseded`，因此同一供应商同时只有一个待复核版本。
- 发布流程：低/中风险自动发布；高/极高风险为草稿，进入 `/api/risk/reviews/pending`，复核可直接发布、调整等级与抽检比例后发布（`publish_override`）或驳回（`reject`）；抽检建议只取自已发布的当前版本，未发布前沿用基线 5%。
- 历史保留：供应商不能删除，只能停用或合并；合并支持多级（A→B→C），评分沿合并链汇总全部来源事件，停用后画像与事件仍可查询。

主要接口（前缀 `/api/risk`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/suppliers` | 登记供应商（建批次时也会按名称自动登记/回填） |
| POST | `/suppliers/{id}/merge` `/disable` `/enable` | 合并、停用、重新启用 |
| GET | `/suppliers/{id}/profile` | 当前画像：等级、抽检比例建议与逐因子解释 |
| GET | `/suppliers/{id}/events` | 输入事件（含已合并来源、作废事件） |
| POST | `/suppliers/{id}/complaints` | 登记投诉并触发重算 |
| POST/GET | `/suppliers/{id}/rectifications`、`POST /rectifications/{id}/resolve` | 整改单与闭环 |
| POST | `/suppliers/{id}/scores?force=true` | 立即重算（默认同输入幂等） |
| GET | `/suppliers/{id}/scores`、`/scores/{version}` | 版本列表与单版本解释（含输入事件） |
| GET | `/reviews/pending`、`POST /scores/{id}/review` | 待复核列表与复核决定 |
| GET/POST | `/rules` | 规则版本列表与发布（配置带版本号，发布前校验） |

食品批次的 `recommended_sampling_ratio` 字段会随已发布画像自动写回；批次详情接口可一并查看。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、农产品批次、抽样检测、运输追踪、风险任务和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  food/             农产品、检测、运输和风险处置服务
  risk/             供应商风险画像：规则引擎、评分版本、复核发布
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
