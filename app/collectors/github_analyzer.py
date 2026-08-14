import asyncio
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any

from app.api.github_client import GitHubClient
from app.collectors.base import BaseAnalyzer
from app.config import settings
from app.database.models import ContentItem
from app.logger import AppLogger
from app.services.github_service import GithubService

logger = AppLogger.get(__name__)
service = GithubService()

class DataCleaner:

    @staticmethod
    def clean_readme(readme: str, max_length: int = 6000) -> str:
        if not readme:
            return ""

        # 1. 移除 HTML 注释
        readme = re.sub(r'<!--.*?-->', '', readme, flags=re.DOTALL)
        # 2. 移除 HTML 标签
        readme = re.sub(r'<[^>]+>', '', readme)
        # 3. 移除 Markdown 图片语法但保留alt文本
        readme = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', readme)
        # 4. 简化链接：[text](url) -> text，保留文字
        readme = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', readme)
        # 5. 移除 Badge（通常是![badge](url) 或 [![badge](url)](link)）
        readme = re.sub(r'!\[.*?\]\(.*?\)', '', readme)
        # 6. 移除连续的空行（超过2个换行符变为2个）
        readme = re.sub(r'\n{3,}', '\n\n', readme)
        # 7. 移除代码块中的冗长内容但保留代码块标记
        readme = DataCleaner._truncate_code_blocks(readme)
        # 8. 移除表格（可选，因为表格通常较大）
        readme = DataCleaner._remove_or_simplify_tables(readme)
        # 9. 移除只包含链接的行（比如 [更多](url)、[点击这里](url) 等）
        readme = DataCleaner._remove_link_only_lines(readme)
        # 10. 移除连续的特殊字符和分隔线
        readme = re.sub(r'-{5,}', '---', readme)
        readme = re.sub(r'\*{5,}', '***', readme)
        readme = re.sub(r'={5,}', '===', readme)
        # 11. 清理行尾空格
        readme = '\n'.join(line.rstrip() for line in readme.split('\n'))
        # 12. 截断到指定长度
        if len(readme) > max_length:
            readme = readme[:max_length] + "\n[内容已截断...]"
        return readme.strip()

    @staticmethod
    def _truncate_code_blocks(text: str, max_block_length: int = 500) -> str:
        pattern = r'```[\s\S]*?```'
        def truncate_match(match):
            block = match.group(0)
            if len(block) > max_block_length:
                lines = block.split('\n')
                if len(lines) > 10:
                    return '\n'.join(lines[:5]) + '\n...\n' + '\n'.join(lines[-2:])
            return block
        text = re.sub(pattern, truncate_match, text, flags=re.DOTALL)
        return text

    @staticmethod
    def _remove_or_simplify_tables(text: str) -> str:
        """ 简化或移除 Markdown 表格, 保留表头信息，移除详细内容 """
        # 匹配 Markdown 表格
        pattern = r'\|.*?\|[\s\S]*?(?=\n\n|\n[^|]|\Z)'

        def simplify_table(match):
            table = match.group(0)
            lines = table.split('\n')
            if len(lines) > 5:
                # 只保留表头（前2行）
                return '\n'.join(lines[:2]) + '\n...'
            return table

        text = re.sub(pattern, simplify_table, text)
        return text

    @staticmethod
    def _remove_link_only_lines(text: str) -> str:
        """
        移除只包含链接的行
        """
        lines = text.split('\n')
        filtered_lines = []

        for line in lines:
            stripped = line.strip()
            # 如果这一行只包含链接和空格，跳过它
            if re.match(r'^\s*\[.+\]\(.+\)\s*$', stripped) or \
                    re.match(r'^\s*<.*?>\s*$', stripped) or \
                    re.match(r'^\s*https?://.*$', stripped):
                continue
            filtered_lines.append(line)

        return '\n'.join(filtered_lines)

    @staticmethod
    def clean_topic(topic: str) -> str:
        """清理主题标签"""
        topic = topic.strip().lower()
        # 移除特殊字符
        topic = re.sub(r'[^a-z0-9\-]', '', topic)
        return topic

class GitHubAnalyzer(BaseAnalyzer):
    NAME = "github"

    def __init__(self, client: GitHubClient=None):
        self.client = client if client else GitHubClient(settings.github_token)
        self.cleaner = DataCleaner()

    async def _analyze_repository(self, repo_url: str) -> Dict[str, Any]:
        try:
            owner, repo = self.client.parse_github_url(repo_url)
            logger.info(f"🔍 分析项目: {owner}/{repo}")
            tasks = [
                self._get_readme(owner, repo),
                self._get_commits_activity(owner, repo),
                self._get_issues_summary(owner, repo),
                self._get_dependencies(owner, repo),
                self._get_topics(owner, repo),
                self._get_contributors(owner, repo),
                self._get_languages(owner, repo),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            readme, commits_data, issues, deps, topics, contributors, languages = results

            # 清理 README
            cleaned_readme = self.cleaner.clean_readme(readme) if not isinstance(readme, Exception) else ""
            repo_info = service.search_project(f"{owner}/{repo}")

            analysis = {
                "repo_url": f"https://github.com/{owner}/{repo}",
                "owner": owner,
                "name": repo,
                "repo_info": repo_info if repo_info else {},
                "readme": cleaned_readme,
                "commits_activity": commits_data if not isinstance(commits_data, Exception) else {},
                "issues": issues if not isinstance(issues, Exception) else {},
                "dependencies": deps if not isinstance(deps, Exception) else {},
                "topics": topics if not isinstance(topics, Exception) else [],
                "contributors": contributors if not isinstance(contributors, Exception) else [],
                "languages": languages if not isinstance(languages, Exception) else {},
            }
            logger.info(f"✅ 完成数据采集: {owner}/{repo}")
            return analysis
        except Exception as e:
            logger.error(f"❌ 分析失败: {str(e)}")
            raise

    def _build_context_sections(self, repo_info: ContentItem, commits_activity: Dict,  issues: Dict, dependencies: Dict, topics: List[str] = None, contributors: List[Dict] = None, languages: Dict[str, int] = None) -> Dict[str, str]:
        # 基本信息
        basic_info = (
            f"Star数: {repo_info.metrics["stars"]} | "
            f"Fork数: {repo_info.metrics["forks"]} | "
            f"主语言: {repo_info.metrics["language"]} | "
            f"License: {repo_info.metrics["license"]} | "
            f"项目简介: {repo_info.description}\n"
            f"创建时间: {repo_info.metrics["created_at"]} | "
            f"更新时间: {repo_info.metrics["updated_at"]} | "
        )

        # 活跃度
        activity = (
            f"近30天commits数: {commits_activity.get('commits_30_days', 0)} | "
            f"提交频率: {commits_activity.get('commit_frequency', '未知')} | "
            f"是否活跃: {'是' if commits_activity.get('is_active') else '否'} | "
            f"最近一次commit: {commits_activity.get('last_commit_date', '未知')}"
        )

        # Issues
        open_count = issues.get('open_count', 0)
        response_time = issues.get('issue_response_time', '未知')
        recent_issues_titles = '; '.join(
            i.get('title', '') for i in issues.get('recent_issues', [])[:3]
        )
        issues_text = (
            f"未关闭issue数: {open_count} | 平均响应时长: {response_time}\n"
            f"近期issue示例: {recent_issues_titles or '无'}"
        )

        # 依赖
        dependencies_text = (
            f"依赖总数: {dependencies.get('total_dependencies', 0)}"
        )

        # Topics / 贡献者 / 语言占比（#06 分析质量）
        topics_text = "、".join(topics) if topics else "无"
        top_contributors = sorted(
            contributors, key=lambda c: c.get("contributions", 0), reverse=True
        )[:5]
        contributors_text = (
            "、".join(
                f"{c.get('login', '?')}({c.get('contributions', 0)})"
                for c in top_contributors
            )
            if top_contributors else "无"
        )
        total_bytes = sum(languages.values()) if languages else 0
        top_langs = sorted(languages.items(), key=lambda kv: kv[1], reverse=True)[:5]
        languages_text = (
            "、".join(
                f"{name} {int(pct * 100)}%" for name, pct in (
                    (n, b / total_bytes) for n, b in top_langs
                )
            )
            if total_bytes else "无"
        )

        return {
            "basic_info": basic_info,
            "activity": activity,
            "issues": issues_text,
            "dependencies": dependencies_text,
            "topics": topics_text,
            "contributors": contributors_text,
            "languages": languages_text,
        }

    async def _get_topics(self, owner: str, repo: str) -> List[str]:
        try:
            return await self.client.get_topics(owner, repo)
        except Exception as e:
            logger.warning(f"获取 topics 失败: {e}")
            return []

    async def _get_contributors(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        try:
            return await self.client.get_contributors(owner, repo, per_page=10)
        except Exception as e:
            logger.warning(f"获取 contributors 失败: {e}")
            return []

    async def _get_languages(self, owner: str, repo: str) -> Dict[str, int]:
        try:
            return await self.client.get_languages(owner, repo)
        except Exception as e:
            logger.warning(f"获取 languages 失败: {e}")
            return {}

    async def build_context(self, repo_url: str):
        analysis = await self._analyze_repository(repo_url)

        readme = analysis.get("readme", "")
        readme_text = readme[:6000] if len(readme) > 6000 else readme  # 留点余量给其他信息

        repo_info = analysis.get("repo_info", {})
        commits_activity = analysis.get("commits_activity", {})
        issues = analysis.get("issues", {})
        dependencies = analysis.get("dependencies", {})
        topics = analysis.get("topics", [])
        contributors = analysis.get("contributors", [])
        languages = analysis.get("languages", {})

        context_sections = self._build_context_sections(
            repo_info, commits_activity, issues, dependencies,
            topics=topics, contributors=contributors, languages=languages,
        )

        # #06 渐进式 README：保留核心内容，标注已截断，避免一次性塞满上下文
        readme_note = ""
        if len(readme) > 6000:
            readme_note = "\n（README 已截断至前 6000 字符，关键结论请基于下方结构化指标推断）\n"

        prompt = f"""
                你是一位专业的 GitHub 项目分析师，请综合下面提供的多维度数据，仔细分析这个项目，并用**纯 JSON** 格式返回结果，不要包含任何其他文字和 markdown 代码块。

                项目名称：{analysis.get("name", "")}
                仓库地址：{analysis.get("repo_url", "")}

                ## 基本信息
                {context_sections['basic_info']}

                ## 活跃度数据
                {context_sections['activity']}

                ## Issues 概况
                {context_sections['issues']}

                ## 依赖情况
                {context_sections['dependencies']}

                ## 主题标签 (Topics)
                {context_sections['topics']}

                ## 核心贡献者（按贡献数）
                {context_sections['contributors']}

                ## 语言占比
                {context_sections['languages']}

                ## README 内容
                {readme_text}{readme_note}

                **【证据合成硬约束】** 你必须基于上方真实数据（Star/Fork 数、commit 频率、issue 响应、contributors、语言占比、README 原文）交叉验证后给出结论：
                - summary / pros / cons 中每一条判断都要能对应到上方某个**具体数字或事实**（如「近30天 {commits_activity.get('commits_30_days', 0)} 次提交」、「{context_sections['contributors']} 等贡献者」）。
                - 不得臆测 README 之外未给出的能力；宁可保守，不可编造。
                - 若某项数据缺失，明确说明「数据不足」，不要假装有。

                请结合以上**全部信息**（不要只看README），综合评估项目的活跃度、社区健康度、技术成熟度，返回以下 JSON 结构（必须是有效的 JSON，不要包含任何其他内容）：
                {{
                  "one_liner": "一句话介绍（15-25字）",
                  "summary": "项目详细介绍（80-150字，需结合活跃度/社区情况等综合判断，并引用具体数据，不要只复述README）",
                  "use_cases": "主要适用场景，用逗号分隔",
                  "pros": ["优点1（附数据依据）", "优点2（附数据依据）", "优点3"],
                  "cons": ["缺点1（附数据依据）", "缺点2"],
                  "score": 0,
                  "tags": ["tag1", "tag2", "tag3"],
                  "install_step": "简要安装/使用步骤"
                  "maturity_level": "成熟度评估（早期项目 / 稳定可用 / 成熟可靠 / 生产级）"
                }}

                **评分标准（score 满分10分）**：
                - 9.0+：社区活跃、更新频繁、实际使用价值高
                - 7.5-8.9：有潜力但可能存在明显不足
                - 低于7.5：问题较多或发展停滞
                """
        return prompt

    async def _get_readme(self, owner: str, repo: str) -> str:
        try:
            text = await self.client.get_readme(owner, repo)
            return text
        except Exception as e:
            logger.warning(f"获取 README 失败: {e}")
            return ""

    async def _get_commits_activity(self, owner: str, repo: str) -> Dict[str, Any]:
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            commits = await self.client.list_commits(owner, repo,
                                                     since=start_date.isoformat(),
                                                     until=end_date.isoformat(),per_page=100)
            latest_commit = commits[0] if commits else None
            return {
                "commits_30_days": len(commits),
                "is_active": len(commits) > 0,
                "last_commit_date": latest_commit.get("commit").get("author").get("date") if latest_commit else None,
                "last_commit_message": latest_commit.get("commit").get("message") if latest_commit else None,
                "commit_frequency": "high" if len(commits) > 10 else "medium" if len(commits) > 3 else "low"
            }
        except Exception as e:
            logger.warning(f"获取 commits 信息失败: {e}")
            raise

    async def _get_issues_summary(self, owner: str, repo: str) -> Dict[str, Any]:
        try:
            open_issues = await self.client.list_issues(owner, repo, state="open", per_page=10)
            closed_issues = await self.client.list_issues(owner, repo, state="closed", per_page=10, sort="updated")
            return {
                "open_count": len(open_issues),
                "recent_issues": [
                    {
                        "title": issue.get("title"),
                        "state": issue.get("state"),
                        "created_at": issue.get("created_at"),
                        "comments": issue.get("comments")
                    }
                    for issue in open_issues[:5]
                ],
                "issue_response_time": self._estimate_response_time(closed_issues)
            }
        except Exception as e:
            logger.warning(f"获取 issues 信息失败: {e}")
            raise

    async def _get_dependencies(self, owner: str, repo: str) -> Dict[str, Any]:
        try:
            data = await self.client.get_sbom(owner, repo)
            if data:
                components = data.get("sbom", {}).get("components", [])
                return {
                    "total_dependencies": len(components),
                    "language": data.get("sbom", {}).get("name")
                }
            else:
                return {"total_dependencies": 0}

        except Exception as e:
            logger.warning(f"获取依赖信息失败: {e}")
            return {"total_dependencies": 0}

    def _estimate_response_time(self, closed_issues: List) -> str:
        if not closed_issues:
            return "未知"

        response_times = []
        for issue in closed_issues[:5]:
            created_at = issue.get("created_at")
            closed_at = issue.get("closed_at")
            if not created_at or not closed_at:
                continue

            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            delta = (closed - created).days
            response_times.append(delta)

        if response_times:
            avg = sum(response_times) / len(response_times)
            if avg < 1:
                return "< 1 天"
            elif avg < 7:
                return f"{int(avg)} 天"
            else:
                return f"{int(avg / 7)} 周"
        return "未知"
