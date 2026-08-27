# AI Project Bot

Telegram 机器人：自动采集 GitHub / HuggingFace / 新闻 / LLM 排行榜数据 → LLM 摘要 → 人工审查（仅新闻）→ 推送到频道 / 私聊订阅。

---

## 架构总览

```
                    ┌────────────────────────────────────────────────┐
                    │  Scheduler (APScheduler, cron 定时触发)         │
                    │  8:00 daily_run · 13:00 news_collect           │
                    │  9:00 push · 10:00 review · 14/20:00 snapshot  │
                    │  Mon 7:30 llm_leaderboard                      │
                    └───────────────┬────────────────────────────────┘
                                    ▼
   ┌────────────────────┐    ┌────────────────────┐    ┌────────────────────┐
   │ Collector          │───▶│ ContentItem        │───▶│ Analyzer           │
   │ 采集原始数据        │    │ (source+external_id │    │ build_context(prompt)│
   │ + MetricSnapshot   │    │  upsert)           │    │ README/commits/…   │
   └────────────────────┘    └────────────────────┘    └─────────┬──────────┘
                                                                  ▼
   ┌────────────────────┐                                ┌────────────────────┐
   │ Publisher          │                                │ Summarizer (LLM)   │
   │ 频道/Bot 推送       │◀───  审查通过(approved)  ───────│ 结构化 JSON → Summary│
   │ (ADR-0003 双通道)   │                                └─────────┬──────────┘
   └────────────────────┘                                          ▼
                                                            ┌────────────────────┐
                                                            │ Review Gate (Q4)   │
                                                            │ 仅新闻：DM 管理员    │
                                                            │ 白名单 ✅/❌ 人工审查 │
                                                            └────────────────────┘
```

**核心原则（ADR）**
- ADR-0001：人工审查闸门 = 仅新闻进审查；GitHub/HF 已被 star/trending 信号预过滤，自动推送。
- ADR-0002：`ContentItem` 统一存储范式（entity 一行 + MetricSnapshot 每日快照）。
- ADR-0003：双通道——频道只承载已审新闻；GitHub/HF 走 bot 私聊/订阅推送，不进频道。

---

## 模块结构

| 目录 | 职责 |
|------|------|
| `app/bot/` | Telegram 入口：aiogram `Router`（各 handler）、middleware（限流/用户注册）、renderers（卡片渲染） |
| `app/collectors/` | 采集器 `BaseCollector`（fetch→save）与分析器 `BaseAnalyzer`（build_context→prompt）；含 `news/` 子目录 |
| `app/ai/` | LLM Provider 适配器注册表（openrouter/ollama/groq/openai/opencode/agent，带失败回退）+ `BatchSummary` 批量摘要 |
| `app/services/` | 业务服务层：`GithubService`/`HuggingFaceService`/`NewsService`（DB 查询）、`ReviewService`（审查）、`DailyPublisher`（推送） |
| `app/api/` | 外部 API 客户端：github / huggingface / rss / hackernews |
| `app/database/` | SQLAlchemy engine + session + `ContentItem`/`Summary`/`User`/`Subscription` 等模型 |
| `app/scheduler/` | APScheduler 定时任务编排 |
| `app/config.py` | pydantic-settings 配置（env 注入，`.env`/`.env.local`） |
| `app/logger.py` | 日志（控制台 + 每日轮转文件，JSON 可选） |
| `app/health.py` | 管道健康指标 + `PipelineRun` 记录；`app/api_server.py` 暴露 `/health` |

---

## 核心流程

### 1. 采集（Collector）
`Collector.fetch(config)` 发现候选 → 增强（star 增长/commit/release）→ `save()`：
按 `source + external_id` upsert `ContentItem`，并写 `MetricSnapshot`（每日快照，ADR-0002）。
`processed=0` 表示待分析。

### 2. 分析与摘要（Analyzer → Summarizer）
`BatchSummary.process_batch()` 捞 `processed=0` 且无 Summary 的项目 →
`Analyzer.build_context(project.url)` 抓 README/commits/issues/依赖/贡献者/语言，拼装 prompt →
`Summarizer` 调 LLM 返回严格 JSON（summary/pros/cons/score/tags/install_step）→ 存 `Summary`，`processed=1`。
Provider 按 `provider_fallback_order` 依次尝试，失败自动回退（`QuotaExceededError`/`ProviderUnavailableError`）。

### 3. 人工审查（Q4，仅新闻）
`ReviewService.send_for_review()`：把待审新闻卡片**私聊 DM** 给 `REVIEW_ADMIN_IDS` 白名单管理员（带 ✅/❌ 按钮）。
管理员点击 → `review_handler` 双重校验：白名单身份 + `review_status == 'pending'` 幂等守卫 → `approve`（推公开频道）/ `reject`。
状态流转：`NULL → pending → approved / rejected`。

### 4. 推送（Publisher）
- 频道：`publish_to_channel()` 只推 `approved` 的新闻；跨源去重（NewsCluster 非 canonical 成员跳过）。
- Bot 私聊：`publish_to_user()` 按订阅维度过滤推送 GitHub/HF 趋势与最热。

---

## 定时任务（`app/scheduler/jobs.py`）

| 时间 | 任务 | 说明 |
|------|------|------|
| 08:00 | `daily_run` | 采集（`daily_collect`，按 source/config 隔离失败）+ 处理流程 |
| 09:00 | `daily_push` | 主动推送订阅内容 |
| 10:00 | `daily_review` | 送审（DM 管理员） |
| 13:00 | `news_collect` | 午后新闻二次采集（时效性） |
| 14:00/20:00 | `lightweight_snapshot` | 轻量指标快照（不触发 LLM） |
| Mon 07:30 | `weekly_leaderboard` | LLM 排行榜采集 |

---

## 配置（`.env`）

关键项：`TELEGRAM_BOT_TOKEN`、`REVIEW_ADMIN_IDS=[id1,id2]`、`PUBLIC_CHANNEL_ID`、
`DATABASE_URL`、`GITHUB_TOKEN`、各 LLM Provider key（`OPENROUTER_API_KEY`/`OPENAI_API_KEY`…）、`OLLAMA_URL`。

---

## 安全与已知问题

> 现状速览：`tests/*`、`*.md`、`.env*` 均被 `.gitignore` 忽略，不入库。

**安全风险**
1. 【高】`docker-compose.yml` 弱口令/无鉴权：Postgres `dev:dev123`、pgAdmin `admin@admin.com/admin123`、Redis 无密码，且 `5432/6379/8090` 端口映射到宿主机。若宿主暴露公网 → 数据库/缓存可直接被连。
2. 【中】`_render_review_card` 的 `<a href="{url}">` 中 `url` 未做 HTML 转义（title 已 escape）。采集自不可信来源的仓库 URL 可注入属性。
3. 【中】LLM Prompt 注入：README/描述等采集内容直接进 prompt，无输出侧强校验，恶意仓库可能诱导摘要输出误导性内容进频道。
4. 【低】`/health` 接口无鉴权，暴露管道统计信息。
5. 【低】限流（Throttling/ToolRateLimit）与 FSM 均为内存态（`MemoryStorage`）——仅单实例有效，重启即失。

**工作流问题**
1. 【中】GitHub 仓库改名/转移后 API 返回 301：analyzer 各接口失败并静默降级（数据缺失），且库中 `external_id/url` 不会同步到新名（方案已定：`follow_redirects=True` + `full_name` 规范化，待实施）。
2. 【中】`send_for_review` 只要任一管理员收到即标 `pending`：个别管理员投递失败后不再重试（其余管理员仍可处理）。
3. 【中】LLM 摘要失败即标 `processed=2`，无自动重试/退避，需人工干预。
4. 【低】错误处理不一致：`_get_commits_activity`/`_get_issues_summary` 抛异常，其余 `_get_*` 吞异常返回默认值。
5. 【低】`_build_context_sections` 直接下标访问 `repo_info.metrics[...]`，缺 key 时整条分析失败。

---

### alembic命令

**初始化 alembic**
* alembic init migrations

**生成迁移文件**
  * alembic revision --autogenerate -m "init"
**执行迁移**
* alembic upgrade head

### docker命令
# 4️⃣ 部署
docker-compose up -d
# 5️⃣ 查看日志
docker-compose logs -f bot
# 停止旧容器
docker-compose down
# 重新构建
docker-compose build --no-cache
# 迁移
docker-compose exec -T bot alembic upgrade head
# 重启
docker compose up -d --build

### 迁移数据
# 1. 初始化（只需要做一次，且既然你已经有 migrations 文件夹了，这步跳过）
docker compose exec bot alembic init migrations
# 2. 每次改完模型后，生成迁移文件
docker compose exec bot alembic revision --autogenerate -m "init"
# 3. 应用迁移到数据库
docker compose exec bot alembic upgrade head


### 数据库
# 进入postgresql
docker compose exec postgres psql -U dev -d ai_projects
# 备份
docker compose exec postgres pg_dump -U dev ai_projects > backup.sql
# 恢复
docker compose exec -T postgres psql -U dev -d ai_projects < backup.sql
# 测试
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434').read())"
-- 删除
docker compose exec postgres psql -U dev -d ai_projects -c "TRUNCATE content_items, summaries, metric_snapshots, news_clusters, pipeline_runs, user_favorites, subscriptions, users RESTART IDENTITY CASCADE;"                                              
-- 加字段
ALTER TABLE projects ADD COLUMN status VARCHAR(20) DEFAULT 'pending';
-- 删字段
ALTER TABLE projects DROP COLUMN status;
-- 改字段类型
ALTER TABLE projects ALTER COLUMN stargazers_count TYPE BIGINT;
-- 改字段名
ALTER TABLE projects RENAME COLUMN old_name TO new_name;
-- 加非空约束
ALTER TABLE projects ALTER COLUMN name SET NOT NULL;
-- 加索引
CREATE INDEX idx_projects_status ON projects(status);
-- 删表
DROP TABLE table_name;
SELECT * FROM projects LIMIT 10;
SELECT COUNT(*) FROM projects;
DELETE FROM projects WHERE id = 1;
UPDATE projects SET status = 'done' WHERE id = 1;
