import asyncio
from typing import Any, Dict

from app.api.huggingface_client import HuggingFaceClient
from app.collectors.base import BaseAnalyzer
from app.collectors.github_analyzer import DataCleaner
from app.config import settings
from app.logger import AppLogger

logger = AppLogger.get(__name__)


class HuggingFaceAnalyzer(BaseAnalyzer):
    NAME = "huggingface"

    def __init__(self, client=None):
        self.client = client if client else HuggingFaceClient(settings.hugging_face_token)
        self.cleaner = DataCleaner()

    async def _analyze_repository(self, repo_url: str) -> Dict[str, Any]:
        try:
            owner, model_name = self.client.parse_hf_url(repo_url)
            logger.info(f"🔍 分析 HF 模型: {owner}/{model_name}")

            tasks = [
                self._get_model_info(owner, model_name),
                self._get_readme(owner, model_name),
                self._get_model_files(owner, model_name),
                self._get_commits_activity(owner, model_name),
                self._get_discussions_summary(owner, model_name),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            model_info, readme, files, commits_data, discussions = results
            cleaned_readme = (
                self.cleaner.clean_readme(readme)
                if not isinstance(readme, Exception) else ""
            )

            analysis = {
                "repo_url": f"https://huggingface.co/{owner}/{model_name}",
                "owner": owner,
                "name": model_name,
                "model_info": model_info if not isinstance(model_info, Exception) else {},
                "readme": cleaned_readme,
                "files": files if not isinstance(files, Exception) else [],
                "commits_activity": commits_data if not isinstance(commits_data, Exception) else {},
                "discussions": discussions if not isinstance(discussions, Exception) else {},
            }

            logger.info(f"✅ 完成 HF 模型數據采集: {owner}/{model_name}")
            return analysis

        except Exception as e:
            logger.error(f"❌ 分析 HF 模型失敗 {repo_url}: {str(e)}")
            raise

    async def _get_model_info(self, owner: str, model_name: str):
        try:
            model_id = f"{owner}/{model_name}"
            return await self.client.get_model_info(model_id)
        except Exception as e:
            logger.warning(f"獲取模型資訊失敗 {owner}/{model_name}: {e}")
            return {}

    async def _get_readme(self, owner: str, model_name: str) -> str:
        try:
            model_id = f"{owner}/{model_name}"
            text = await self.client.get_model_readme(model_id)
            return text or ""
        except Exception as e:
            logger.warning(f"獲取 README 失敗: {e}")
            return ""

    async def _get_model_files(self, owner: str, model_name: str) -> list:
        try:
            model_id = f"{owner}/{model_name}"
            return await self.client.list_model_files(model_id)
        except Exception as e:
            logger.warning(f"獲取模型檔案列表失敗: {e}")
            return []

    async def _get_commits_activity(self, owner: str, model_name: str) -> Dict[str, Any]:
        try:
            model_id = f"{owner}/{model_name}"
            commits = await self.client.list_commits(model_id, limit=50)
            latest = commits[0] if commits else None

            return {
                "commits_count": len(commits),
                "is_active": len(commits) > 0,
                "last_commit_date": latest.get("commit", {}).get("author", {}).get("date") if latest else None,
                "commit_frequency": "high" if len(commits) > 15 else "medium" if len(commits) > 5 else "low"
            }
        except Exception as e:
            logger.warning(f"獲取 commits 失敗: {e}")
            return {"commits_count": 0, "is_active": False}

    async def _get_discussions_summary(self, owner: str, model_name: str) -> Dict[str, Any]:
        try:
            model_id = f"{owner}/{model_name}"
            discussions = await self.client.list_discussions(model_id, limit=10)  # 需在 Client 實作
            open_discussions = [d for d in discussions if d.get("status") != "closed"]

            return {
                "open_count": len(open_discussions),
                "recent_discussions": [
                    {
                        "title": d.get("title"),
                        "status": d.get("status"),
                        "created_at": d.get("created_at")
                    }
                    for d in discussions[:5]
                ]
            }
        except Exception as e:
            logger.warning(f"獲取 Discussions 失敗: {e}")
            return {"open_count": 0, "recent_discussions": []}

    def _build_context_sections(self, model_info: Dict, commits: Dict,
                                discussions: Dict, files: list) -> Dict[str, str]:

        basic_info = (
            f"Likes: {model_info.get('likes', 0)} | "
            f"Downloads: {model_info.get('downloads', 0):,} | "
            f"Pipeline: {model_info.get('pipeline_tag', '未知')} | "
            f"Library: {model_info.get('library_name', '未知')}\n"
            f"最後更新: {model_info.get('lastModified', '未知')}"
        )

        activity = (
            f"近期的 commits 數量: {commits.get('commits_count', 0)} | "
            f"是否活躍: {'是' if commits.get('is_active') else '否'} | "
            f"提交頻率: {commits.get('commit_frequency', 'low')}"
        )

        # Discussions（類似 Issues）
        open_count = discussions.get('open_count', 0)
        recent_titles = '; '.join(
            d.get('title', '') for d in discussions.get('recent_discussions', [])[:3]
        )
        discussions_text = (
            f"未解決討論數: {open_count}\n"
            f"近期討論: {recent_titles or '無'}"
        )

        files_text = f"模型包含檔案數: {len(files)} | "
        if files:
            config = any(f.endswith('config.json') for f in files)
            weights = any(f.endswith(('.bin', '.safetensors', '.pth')) for f in files)
            files_text += f"包含 config: {'是' if config else '否'} | 包含權重: {'是' if weights else '否'}"

        return {
            "basic_info": basic_info,
            "activity": activity,
            "discussions": discussions_text,
            "files": files_text,
        }

    async def build_context(self, repo_url: str):
        analysis = await self._analyze_repository(repo_url)

        readme = analysis.get("readme", "")
        readme_text = readme[:5500] if len(readme) > 5500 else readme

        model_info = analysis.get("model_info", {})
        commits = analysis.get("commits_activity", {})
        discussions = analysis.get("discussions", {})
        files = analysis.get("files", [])

        context_sections = self._build_context_sections(
            model_info, commits, discussions, files
        )

        prompt = f"""
        你是一位專業的 Hugging Face 模型分析師，請綜合所有提供的數據，深入分析這個模型。
        
        模型名稱：{analysis.get("name", "")}
        倉庫地址：{analysis.get("repo_url", "")}
        
        ## 基本信息
        {context_sections['basic_info']}
        
        ## 活躍度
        {context_sections['activity']}
        
        ## 社群討論
        {context_sections['discussions']}
        
        ## 檔案概況
        {context_sections['files']}
        
        ## README 內容
        {readme_text}
        
        請結合**全部資訊**進行綜合評估，並嚴格按照以下 JSON 格式返回（不要包含任何其他文字、說明或 markdown）：
        
        {{
          "one_liner": "一句話介紹（15-25字）",
          "summary": "模型詳細介紹（80-150字，需包含實際用途、優勢、社區情況）",
          "use_cases": "主要適用場景，用逗號分隔",
          "pros": ["優點1", "優點2", "優點3"],
          "cons": ["缺點1", "缺點2"],
          "score": 0,
          "tags": ["tag1", "tag2", "tag3"],
          "install_step": "簡要安裝/使用步驟",
          "maturity_level": "成熟度評估（早期實驗 / 穩定可用 / 成熟可靠 / 生產級）"
        }}
        
        **評分標準（滿分10分）**：
        - 9.0+：下載量高、社區活躍、更新頻繁、實用性強
        - 7.5-8.9：有潛力，但存在部分不足
        - 低於7.5：活躍度低或質量一般
        """
        return prompt