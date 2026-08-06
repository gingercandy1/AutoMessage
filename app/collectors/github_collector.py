import asyncio
import random
import httpx
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC, timezone
from typing import Optional, List, Dict, Any, Callable
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert

from app.api.github_client import GitHubClient, GitHubRateLimitError
from app.collectors.base import BaseCollector
from app.config import settings
from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot
from app.logger import AppLogger

logger = AppLogger.get(__name__)


@dataclass
class GithubConfig:
    language: Optional[str] = None
    max_results: int = 30
    sort_by: str = "growth" # growth | stars

    # growth
    discovery_days: int = 14
    growth_window_days: int = 7
    discover_mode: str = "new"   #  pushed | new | both
    max_stargazer_pages_per_repo: int = 30

    # stars
    max_candidates: int = 60
    min_stars: int = 100


class GitHubCollector(BaseCollector):
    NAME = "github"

    def __init__(self, client: GitHubClient=None):
        self.client = client if client else GitHubClient(settings.github_token)

    async def get_star_growth(self, owner: str, repo: str, days: int, max_pages: int) -> Dict[str, Any]:
        """获取星标增长，带指数退避重试"""
        max_retries = 3
        base_delay = 2.0
        
        async def _fetch_with_retry():
            cutoff = datetime.now(UTC) - timedelta(days=days)
            data, pages = await self.client.list_stargazers_page(owner, repo)
            last_page = pages.get("last")
            if not last_page:
                recent = sum(
                    1 for item in data
                    if datetime.fromisoformat(item["starred_at"].replace("Z", "+00:00")) >= cutoff
                )
                return {"recent_stars": recent, "pages_checked": 1, "estimated": False}

            recent_count = 0
            pages_checked = 0
            page = last_page
            while page >= 1 and pages_checked < max_pages:
                data = await self.client.list_stargazers_page(owner, repo, page=page)
                pages_checked += 1
                hit_cutoff = False
                for item in data:
                    starred_at = datetime.fromisoformat(item["starred_at"].replace("Z", "+00:00"))
                    if starred_at >= cutoff:
                        recent_count += 1
                    else:
                        hit_cutoff = True
                if hit_cutoff:
                    return {"recent_stars": recent_count, "pages_checked": pages_checked, "estimated": False}
                page -= 1
            return {"recent_stars": recent_count, "pages_checked": pages_checked, "estimated": True}

        last_exception = None
        for attempt in range(max_retries):
            try:
                return await _fetch_with_retry()
            except GitHubRateLimitError as e:
                last_exception = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"GitHub 速率限制，{delay:.1f}s 后重试 ({attempt + 1}/{max_retries}): {owner}/{repo}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"GitHub 速率限制，重试 {max_retries} 次后放弃: {owner}/{repo}")
                    raise
            except Exception as e:
                logger.error(f"获取星标增长失败: {owner}/{repo}: {e}")
                raise
        
        raise last_exception

    async def _search_all(self, query: str, sort: str, max_results: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        page = 1
        per_page = min(100, max_results)
        while len(results) < max_results:
            data = await self.client.search_repositories(query, sort=sort, per_page=per_page, page=page)
            items = data.get("items", [])
            if not items:
                break
            results.extend(items)
            page += 1
            if len(items) < per_page:
                break
        return results[:max_results]

    _RANK_STRATEGIES: Dict[str, Callable[[Dict[str, Any]], Any]] = {
        "growth": lambda r: r["star_growth"].get("recent_stars") or 0,
        "stars": lambda r: r["stargazers_count"],
        "commits": lambda r: r["commit_stats"].get("commits_last_4_weeks") or 0,
    }

    def _rank(self, enriched: List[Dict[str, Any]], config: GithubConfig) -> List[Dict[str, Any]]:
        key_fn = self._RANK_STRATEGIES.get(config.sort_by, self._RANK_STRATEGIES["growth"])
        enriched.sort(key=key_fn, reverse=True)
        return enriched[: config.max_results]

    async def fetch(self, config: GithubConfig) -> int:
        candidates = await self.discover_candidates(config)
        logger.info(f"📋 候选池大小: {len(candidates)}，模式: {config.sort_by}")

        semaphore = asyncio.Semaphore(5)
        async def _bounded_enrich(repo):
            async with semaphore:
                try:
                    return await self.enrich_candidate(repo, config)
                except Exception as e:
                    logger.warning(f"处理 {repo.get('full_name')} 失败: {e}")
                    return None

        enriched = await asyncio.gather(*[_bounded_enrich(r) for r in candidates])
        enriched = [r for r in enriched if r is not None]
        ranked = self._rank(enriched, config)
        for r in ranked:
            g = r["star_growth"]
            growth_display = g.get("recent_stars")
            note = " (下限估计)" if g.get("estimated") else ""
            logger.info(
                f"{r['full_name']:40s} 总星数={r['stargazers_count']:<8} "
                f"增长={growth_display}{note:12s} "
                f"近4周提交={r['commit_stats'].get('commits_last_4_weeks')} "
            )
        added = await self.save(ranked)
        return added

    async def discover_candidates(self, config: GithubConfig) -> List[Dict[str, Any]]:
        candidates: Dict[str, Dict[str, Any]] = {}
        base_filters = f"stars:>={config.min_stars}"
        if config.language:
            base_filters += f" language:{config.language.lower()}"

        if config.sort_by == "growth":
            since = (datetime.now(UTC) - timedelta(days=config.discovery_days)).strftime("%Y-%m-%d")
            if config.discover_mode in ("new", "both"):
                query = f"{base_filters} created:>={since}"
                logger.info(f"🔍 [新项目候选] {query}")
                for repo in await self._search_all(query, sort="stars", max_results=config.max_candidates):
                    candidates[repo["full_name"]] = repo

            if config.discover_mode in ("pushed", "both"):
                query = f"{base_filters} pushed:>={since}"
                logger.info(f"🔍 [近期活跃候选] {query}")
                for repo in await self._search_all(query, sort="updated", max_results=config.max_candidates):
                    candidates.setdefault(repo["full_name"], repo)
        else:
            query = base_filters
            logger.info(f"🔍 [{config.sort_by} 模式候选] {query}")
            for repo in await self._search_all(query, sort="stars", max_results=config.max_candidates):
                candidates[repo["full_name"]] = repo

        return list(candidates.values())[: config.max_candidates]

    async def enrich_candidate(self, repo: Dict[str, Any], config: GithubConfig) -> Dict[str, Any]:
        owner, name = repo["owner"]["login"], repo["name"]
        if config.sort_by == "growth":
            tasks = {
                "commit_stats": self.client.get_commit_activity_stats(owner, name),
                "growth": self.get_star_growth(owner, name, config.growth_window_days, config.max_stargazer_pages_per_repo),
                "release": self.client.get_latest_release(owner, name),
            }
        else:
            tasks = {
                "commit_stats": self.client.get_commit_activity_stats(owner, name),
                "release": self.client.get_latest_release(owner, name),
            }

        results = await asyncio.gather(*tasks.values())
        results_map = dict(zip(tasks.keys(), results))
        commit_stats = results_map["commit_stats"]
        release = results_map["release"]
        growth = results_map.get("growth", {"recent_stars": None, "estimated": None})
        return {
            "full_name": repo["full_name"],
            "owner": owner,
            "name": name,
            "url": repo["html_url"],
            "description": repo.get("description"),
            "language": repo.get("language"),
            "license": repo.get("license", {}).get("name") if repo.get("license") else None,
            "stargazers_count": repo.get("stargazers_count"),
            "forks_count": repo.get("forks_count"),
            "watchers_count": repo.get("watchers_count"),
            "created_at": repo.get("created_at"),
            "updated_at": repo.get("updated_at"),
            "star_growth": growth,
            "commit_stats": {"commits_last_4_weeks": self._sum_weeks(commit_stats, 4)},
            "latest_release": release,
            "open_issues": repo.get("open_issues_count", 0),
            "is_fork": repo.get("fork"),
            "stars_gained_window_days": config.growth_window_days
        }

    @staticmethod
    def _sum_weeks(weekly_stats: List[Dict[str, Any]], n: int) -> Optional[int]:
        if not weekly_stats:
            return None
        return sum(w["total"] for w in weekly_stats[-n:])

    async def save(self, ranked: List[Dict[str, Any]]) -> int:
        db = SessionLocal()
        added = 0
        now = datetime.now(timezone.utc)
        try:
            for item in ranked:
                metrics_data = {
                    "stars": item.get("stargazers_count", 0),
                    "forks": item.get("forks_count", 0),
                    "watchers": item.get("watchers_count", 0),
                    "stars_gained_recent": item["star_growth"].get("recent_stars"),
                    "stars_gained_window_days": item.get("stars_gained_window_days"),
                    "commits_last_4_weeks": item["commit_stats"].get("commits_last_4_weeks"),
                    "language": item.get("language"),
                    "license": item.get("license"),
                    "search_type": "stars",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }

                existing = db.query(ContentItem).filter_by(
                    source=self.NAME,
                    external_id=item["full_name"]
                ).first()
                if existing:
                    existing.title = item["name"]
                    existing.url = item["url"]
                    existing.description = item["description"]
                    existing.author = item["owner"]
                    existing.metrics = metrics_data
                    existing.raw_data = item
                    content_item = existing
                else:
                    project = ContentItem(
                        source=self.NAME,
                        external_id=item["full_name"],
                        title=item["name"],
                        url=item["url"],
                        description=item["description"],
                        author=item["owner"],
                        processed=0,
                        discovered_at=datetime.now(timezone.utc),
                        metrics=metrics_data,
                        raw_data=item
                    )
                    content_item = project
                    db.add(project)
                    db.flush()
                    added += 1

                stmt = insert(MetricSnapshot).values(
                    content_item_id=content_item.id,
                    source=self.NAME,
                    external_id=content_item.external_id,
                    category=item.get("language"),
                    rank=None,
                    metrics=metrics_data,
                    captured_date=now.date(),
                    captured_at=now,
                ).on_conflict_do_update(
                    # ADR-0002: one row per (source, external_id, captured_date)
                    index_elements=["source", "external_id", "captured_date"],
                    set_={
                        "content_item_id": content_item.id,
                        "category": item.get("language"),
                        "rank": None,
                        "metrics": metrics_data,
                        "captured_at": now,
                    }
                )
                db.execute(stmt)
            db.commit()
            return added
        finally:
            db.close()

    def configs(self):
        configs = []
        languages = ["python", "javascript", "c++", "go", "rust", "java", "c#", "php", "swift", "kotlin"]
        for lang in languages:
            config = GithubConfig(
                language=lang,
                growth_window_days=7,
                min_stars=100,
                max_results=50,
                sort_by="growth"
            )
            configs.append(config)
        return configs

async def collect_daily(config: GithubConfig):
    collector = GitHubCollector()
    added = await collector.fetch(config)
    print(f"✅ 入库/更新 {added} 个项目")


class GitHubTrendingCollector(BaseCollector):
    NAME = "github"

    BASE_URL = "https://github.com/trending"
    USER_AGENTS = [
        # Windows Chrome
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        # Windows Edge
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        # macOS Chrome
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        # macOS Safari
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        # macOS Firefox
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
        # Windows Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        # Linux Chrome
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    ]

    def __init__(self, client: GitHubClient = None, timeout: float = 15.0):
        self.timeout = timeout
        self.client = client if client else GitHubClient(settings.github_token)

    def _build_headers(self) -> dict:
        ua = random.choice(self.USER_AGENTS)
        is_firefox = "Firefox" in ua
        is_safari = "Safari" in ua and "Chrome" not in ua
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": random.choice([
                "en-US,en;q=0.9",
                "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "zh-CN,zh;q=0.9,en;q=0.8",
            ]),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }
        if not is_firefox and not is_safari:
            headers.update({
                "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"' if "Windows" in ua else '"macOS"' if "Macintosh" in ua else '"Linux"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            })
        return headers

    async def fetch(self, config: GithubConfig = None, since: str = "daily") -> int:
        """
        抓取一次 Trending 列表
        :param config:
        :param since: "daily" / "weekly" / "monthly"
        :return: 本次入库/更新的项目数量（int），供调度器累加统计
        """
        url = self.BASE_URL
        if config.language:
            url += f"/{config.language}"

        params = {"since": since}
        logger.info(f"🔍 抓取 GitHub Trending: language={config.language or '全部'}, since={since}")
        try:
            headers = self._build_headers()
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
        except Exception as e:
            # 抓取失败不应中断调度器：记录并以 0 计入统计
            logger.error(f"❌ 请求 GitHub Trending 失败: {e}")
            return 0

        projects = await self._parse(response.text)
        added = await self.save(projects)
        return added

    async def _parse(self, html: str) -> list[ContentItem]:
        soup = BeautifulSoup(html, "html.parser")
        articles = soup.select("article.Box-row")
        results: list[ContentItem] = []
        for article in articles:
            try:
                repo = await self._parse_one(article)
                if repo:
                    results.append(repo)
            except Exception as e:
                logger.warning(f"⚠️ 解析单条记录失败，跳过: {e}")
                continue

        logger.info(f"📋 本次共解析到 {len(results)} 个项目")
        return results

    async def _parse_one(self, article) -> Optional[ContentItem]:
        title_tag = article.select_one("h2 a")
        if not title_tag:
            return None

        href = title_tag.get("href", "").strip("/")
        if "/" not in href:
            return None

        author, name = href.split("/", 1)
        url = f"https://github.com/{href}"

        desc_tag = article.select_one("p")
        description = desc_tag.get_text(strip=True) if desc_tag else None

        # 编程语言
        lang_tag = article.select_one('[itemprop="programmingLanguage"]')
        language = lang_tag.get_text(strip=True) if lang_tag else None

        # 本周期新增星标，形如 "1,234 stars today" / "... this week" / "... this month"
        stars_gained = 0
        gained_tag = article.select_one("span.d-inline-block.float-sm-right")
        if gained_tag:
            stars_gained = self._parse_number(gained_tag.get_text())

        # 贡献者头像列表
        built_by = []
        avatar_tags = article.select("span.d-inline-block.mr-2 a img")
        for img in avatar_tags:
            alt = img.get("alt", "").lstrip("@")
            if alt:
                built_by.append(alt)

        owner, repo = self.client.parse_github_url(url)
        repo_info = await self.client.get_repo(owner, repo)
        metrics_data = {
            "stars": repo_info.get("stargazers_count", 0) if repo_info else 0,
            "forks": repo_info.get("forks_count", 0) if repo_info else 0,
            "watchers": repo_info.get("watchers_count", 0) if repo_info else 0,
            "stars_gained_recent": stars_gained,
            "stars_gained_window_days": 1 if "today" in (gained_tag.get_text() if gained_tag else "") else 7,
            "language": language,
            "license": repo_info.get("license", {}).get("name") if repo_info and repo_info.get("license") else None,
            "search_type": "trending",
            "created_at": repo_info.get("created_at") if repo_info else None,
            "updated_at": repo_info.get("updated_at") if repo_info else None,
        }

        return ContentItem(
            source=self.NAME,
            external_id=href,
            title=name,
            url=url,
            description=description,
            author=author,
            processed=0,
            discovered_at=datetime.now(timezone.utc),
            metrics=metrics_data,
            raw_data=repo_info
        )

    @staticmethod
    def _parse_number(text: str) -> int:
        text = text.strip().split(" ")[0]
        text = text.replace(",", "")
        if text.lower().endswith("k"):
            try:
                return int(float(text[:-1]) * 1000)
            except ValueError:
                return 0
        try:
            return int(text)
        except ValueError:
            return 0

    async def save(self, records) -> int:
        db = SessionLocal()
        added, updated = 0, 0
        now = datetime.now(timezone.utc)
        try:
            for item in records:
                existing = db.query(ContentItem).filter_by(
                    source=self.NAME,
                    external_id=item.external_id
                ).first()

                if existing:
                    existing.title = item.title
                    existing.url = item.url
                    existing.description = item.description
                    existing.author = item.author
                    existing.metrics = item.metrics
                    existing.raw_data = item.raw_data
                else:
                    db.add(item)
                    db.flush()
                    added += 1

                stmt = insert(MetricSnapshot).values(
                    content_item_id=item.id,
                    source=self.NAME,
                    external_id=item.external_id,
                    category=item.get("language"),
                    rank=None,
                    metrics=item.metrics,
                    captured_date=now.date(),
                    captured_at=now,
                ).on_conflict_do_update(
                    index_elements=["source", "external_id", "category", "captured_date"],
                    set_={
                        "content_item_id": item.id,
                        "rank": None,
                        "metrics": item.metrics,
                        "captured_at": now.date(),
                    }
                )
                db.execute(stmt)
            db.commit()
            return added
        finally:
            db.close()


    def configs(self):
        # Trending 需要非空配置，否则调度循环 for config in []: 会跳过本采集器（死代码）。
        # 涵盖『全部语言』(language=None) 与各语言，复用 GitHubCollector 的语言集以统一趋势口径。
        languages = [None, "python", "javascript", "c++", "go", "rust",
                     "java", "c#", "php", "swift", "kotlin"]
        return [
            GithubConfig(
                language=lang,
                sort_by="stars",
                growth_window_days=1,
                min_stars=0,
                max_results=50,
            )
            for lang in languages
        ]
