"""
Prompt模板管理器

支持从YAML文件加载提示词模板，变量替换
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

from sag.exceptions import PromptError
from sag.utils import get_logger

logger = get_logger("prompt.manager")


class PromptTemplate:
    """提示词模板"""

    def __init__(
        self,
        name: str,
        template: str,
        variables: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> None:
        """
        初始化提示词模板

        Args:
            name: 模板名称
            template: 模板内容
            variables: 变量列表
            description: 模板描述
        """
        self.name = name
        self.template = template
        self.variables = variables or []
        self.description = description

    def render(self, **kwargs: Any) -> str:
        """
        渲染模板

        Args:
            **kwargs: 变量值

        Returns:
            渲染后的文本

        Raises:
            PromptError: 缺少必需变量

        Example:
            >>> template = PromptTemplate(
            ...     name="summarize",
            ...     template="请总结以下内容：\n{content}",
            ...     variables=["content"]
            ... )
            >>> prompt = template.render(content="文章内容...")
        """
        # 检查必需变量
        missing = set(self.variables) - set(kwargs.keys())
        if missing:
            raise PromptError(f"模板'{self.name}'缺少必需变量: {', '.join(missing)}")

        try:
            # 使用format进行变量替换
            return self.template.format(**kwargs)
        except KeyError as e:
            raise PromptError(f"模板变量错误: {e}") from e
        except Exception as e:
            raise PromptError(f"模板渲染失败: {e}") from e

    def validate_variables(self, **kwargs: Any) -> bool:
        """
        验证变量是否完整

        Args:
            **kwargs: 变量值

        Returns:
            变量完整返回True
        """
        missing = set(self.variables) - set(kwargs.keys())
        return len(missing) == 0


class PromptManager:
    """提示词管理器"""

    def __init__(self, prompts_dir: Optional[Path] = None) -> None:
        """
        初始化提示词管理器

        Args:
            prompts_dir: 提示词目录路径
        """
        if prompts_dir is None:
            # 默认使用项目根目录下的prompts文件夹
            current_file = Path(__file__)
            project_root = current_file.parent.parent.parent.parent
            prompts_dir = project_root / "prompts"

        self.prompts_dir = Path(prompts_dir)
        self.templates: Dict[str, PromptTemplate] = {}

        # 如果目录存在，加载模板
        if self.prompts_dir.exists():
            self.load_templates()
            logger.info(
                "提示词管理器初始化完成",
                extra={"prompts_dir": str(self.prompts_dir), "count": len(self.templates)},
            )
        else:
            logger.warning(f"提示词目录不存在: {self.prompts_dir}")

    def load_templates(self) -> None:
        """从YAML文件加载所有模板"""
        if not self.prompts_dir.exists():
            logger.warning(f"提示词目录不存在: {self.prompts_dir}")
            return

        yaml_files = list(self.prompts_dir.glob("*.yaml")) + list(
            self.prompts_dir.glob("*.yml")
        )

        for yaml_file in yaml_files:
            try:
                self._load_yaml_file(yaml_file)
            except Exception as e:
                logger.error(f"加载提示词文件失败 {yaml_file}: {e}", exc_info=True)

    def _load_yaml_file(self, yaml_file: Path) -> None:
        """
        从YAML文件加载模板

        Args:
            yaml_file: YAML文件路径
        """
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            logger.warning(f"无效的YAML格式: {yaml_file}")
            return

        for name, config in data.items():
            if not isinstance(config, dict):
                continue

            template_text = config.get("template", "")
            variables = config.get("variables", [])
            description = config.get("description", "")

            template = PromptTemplate(
                name=name,
                template=template_text,
                variables=variables,
                description=description,
            )

            self.templates[name] = template
            logger.debug(f"加载模板: {name}")

    def get(self, name: str) -> PromptTemplate:
        """
        获取模板

        Args:
            name: 模板名称

        Returns:
            模板对象

        Raises:
            PromptError: 模板不存在
        """
        if name not in self.templates:
            raise PromptError(f"模板不存在: {name}")
        return self.templates[name]

    def render(self, name: str, **kwargs: Any) -> str:
        """
        渲染模板

        Args:
            name: 模板名称
            **kwargs: 变量值

        Returns:
            渲染后的文本

        Raises:
            PromptError: 模板不存在或渲染失败
        """
        template = self.get(name)
        return template.render(**kwargs)

    def has(self, name: str) -> bool:
        """
        检查模板是否存在

        Args:
            name: 模板名称

        Returns:
            存在返回True
        """
        return name in self.templates

    def list_templates(self) -> List[str]:
        """
        列出所有模板名称

        Returns:
            模板名称列表
        """
        return list(self.templates.keys())

    def add_template(
        self,
        name: str,
        template: str,
        variables: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> None:
        """
        添加模板

        Args:
            name: 模板名称
            template: 模板内容
            variables: 变量列表
            description: 模板描述
        """
        self.templates[name] = PromptTemplate(
            name=name,
            template=template,
            variables=variables,
            description=description,
        )
        logger.info(f"添加模板: {name}")

    def load_json_config(self, config_path: Union[str, Path]) -> Dict[str, Any]:
        """
        加载 JSON 配置文件

        Args:
            config_path: 配置文件路径，支持：
                - 完整路径: "/path/to/config.json"
                - 相对路径: "agent.json" (相对于 prompts_dir)
                - 文件名: "agent" (自动添加 .json，相对于 prompts_dir)

        Returns:
            配置内容

        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: JSON 格式错误

        Example:
            >>> # 加载默认 agent 配置
            >>> config = manager.load_json_config("agent")
            >>>
            >>> # 加载自定义配置
            >>> config = manager.load_json_config("agent_financial.json")
            >>>
            >>> # 使用完整路径
            >>> config = manager.load_json_config("/path/to/custom.json")
        """
        # 转换为 Path 对象
        path = Path(config_path)

        # 如果是相对路径（不是绝对路径）
        if not path.is_absolute():
            # 相对于 prompts_dir
            path = self.prompts_dir / config_path

            # 如果没有 .json 扩展名，自动添加
            if not path.suffix:
                path = path.with_suffix(".json")

        # 检查文件是否存在
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            logger.info(
                "加载配置成功",
                extra={"path": str(path), "version": data.get("version")}
            )
            return data

        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件格式错误 {path}: {e}") from e
        except Exception as e:
            logger.error(f"加载配置失败 {path}: {e}", exc_info=True)
            raise

    def load_agent_config(self, config_name: str = "agent") -> Dict[str, Any]:
        """
        加载 Agent 配置（便捷方法）

        Args:
            config_name: 配置名称，默认 "agent"

        Returns:
            Agent 配置

        Example:
            >>> # 加载默认配置
            >>> config = manager.load_agent_config()
            >>>
            >>> # 加载自定义配置
            >>> config = manager.load_agent_config("agent_financial")
        """
        return self.load_json_config(config_name)


# 全局管理器实例（单例）
_prompt_manager: Optional[PromptManager] = None


def get_prompt_manager() -> PromptManager:
    """
    获取全局提示词管理器

    Returns:
        PromptManager实例
    """
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager


def reset_prompt_manager() -> None:
    """重置全局提示词管理器"""
    global _prompt_manager
    _prompt_manager = None
