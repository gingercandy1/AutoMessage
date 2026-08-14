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


### 完善需求
* 完善功能
  * github爬取项目 
      * 支持按不同维度筛选：Today / This Week / This Month
      * 语言筛选（Python、JavaScript、Go、Rust 等）
      * 领域筛选（AI、Web、区块链、DevTools、游戏等）
  * huggingface爬取项目
      * 完善添加获取项目，添加telegram的获取的方法 
  * 完善添加订阅功能，定期推送最新的消息
  * 完善添加频道
  * 

* 定时启动程序
* 添加付费功能

