"""
配置管理模块

使用pydantic-settings管理配置，支持从环境变量和.env文件读取
"""

from functools import lru_cache
from typing import Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ======================
    # 数据库配置
    # ======================
    mysql_host: str = Field(default="localhost", description="MySQL主机")
    mysql_port: int = Field(default=3306, description="MySQL端口")
    mysql_user: str = Field(default="sag", description="MySQL用户")
    mysql_password: str = Field(default="sag_pass", description="MySQL密码")
    mysql_database: str = Field(default="sag", description="MySQL数据库名")

    # ======================
    # Elasticsearch配置
    # ======================
    es_host: str = Field(default="localhost", description="ES主机")
    es_port: int = Field(default=9200, description="ES端口")
    es_username: Optional[str] = Field(default="elastic", description="ES用户名")
    es_password: Optional[str] = Field(
        default=None,
        description="ES密码",
        validation_alias="ELASTIC_PASSWORD"
    )

    # ======================
    # Redis配置
    # ======================
    redis_host: str = Field(default="localhost", description="Redis主机")
    redis_port: int = Field(default=6379, description="Redis端口")
    redis_password: Optional[str] = Field(default=None, description="Redis密码")
    redis_db: int = Field(default=0, description="Redis数据库")

    # ======================
    # LLM配置（使用中转API或OpenAI官方）
    # ======================
    llm_api_key: str = Field(default="", description="LLM API密钥")
    llm_model: str = Field(default="sophnet/Qwen3-30B-A3B-Thinking-2507", description="LLM模型")
    llm_base_url: Optional[str] = Field(
        default=None, description="LLM API基础URL（留空使用OpenAI官方）"
    )
    
    # LLM 行为参数（所有调用的默认值，可被数据库配置覆盖）
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="LLM温度参数")
    llm_max_tokens: int = Field(default=8000, ge=1, description="LLM最大输出token数")
    llm_top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="LLM top_p参数")
    llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="频率惩罚")
    llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="存在惩罚")
    
    # LLM 可靠性参数
    llm_timeout: int = Field(default=600, ge=1, description="LLM超时时间(秒)")
    llm_max_retries: int = Field(default=3, ge=0, description="LLM最大重试次数")
    
    # 数据库配置开关
    use_db_config: bool = Field(default=True, description="是否使用数据库配置")

    # ======================
    # Embedding配置（使用中转API或OpenAI官方）
    # ======================
    embedding_api_key: str = Field(
        default="", description="Embedding API密钥（留空使用llm_api_key）"
    )
    embedding_model_name: str = Field(default="Qwen/Qwen3-Embedding-0.6B", description="Embedding模型")
    embedding_dimensions: Optional[int] = Field(
        default=None,
        description="Embedding维度（可选，留空则使用模型默认维度。text-embedding-3-small默认1536，text-embedding-3-large默认3072）",
    )
    embedding_base_url: Optional[str] = Field(
        default=None, description="Embedding API基础URL（留空使用llm_base_url）"
    )

    # ======================
    # 应用配置
    # ======================
    debug: bool = Field(default=False, description="调试模式")
    log_level: str = Field(default="INFO", description="日志级别")
    log_format: str = Field(default="json", description="日志格式")

    # ======================
    # API 配置
    # ======================
    api_host: str = Field(default="0.0.0.0", description="API 服务地址")
    api_port: int = Field(default=8000, description="API 服务端口")
    api_workers: int = Field(default=4, description="API Worker 数量")

    # ======================
    # 文件上传配置
    # ======================
    upload_dir: str = Field(default="./uploads", description="上传文件目录")
    max_upload_size: int = Field(
        default=100 * 1024 * 1024, description="最大上传大小（字节，默认100MB）"
    )

    # 实体权重配置
    # entity_weights: str = Field(
    #     default="time:0.9,location:1.0,person:1.1,topic:1.5,action:1.2,tags:1.0",
    #     description="实体类型权重",
    # )

    # ======================
    # 性能配置
    # ======================
    db_pool_size: int = Field(default=10, description="数据库连接池大小")
    db_max_overflow: int = Field(default=20, description="数据库连接池最大溢出")
    db_pool_recycle: int = Field(default=3600, description="数据库连接回收时间(秒)")

    # 缓存TTL
    cache_entity_ttl: int = Field(default=86400, description="实体缓存TTL(秒)")
    cache_llm_ttl: int = Field(default=604800, description="LLM缓存TTL(秒)")
    cache_search_ttl: int = Field(default=3600, description="搜索缓存TTL(秒)")

    @property
    def mysql_url(self) -> str:
        """MySQL连接URL"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @property
    def elasticsearch_url(self) -> str:
        """Elasticsearch连接URL"""
        return f"http://{self.es_host}:{self.es_port}"

    @property
    def es_url(self) -> str:
        """Elasticsearch连接URL（兼容旧版本）"""
        return self.elasticsearch_url

    # @property
    # def entity_weights_dict(self) -> Dict[str, float]:
    #     """实体权重字典"""
    #     result = {}
    #     for pair in self.entity_weights.split(","):
    #         if ":" in pair:
    #             key, value = pair.split(":")
    #             try:
    #                 result[key.strip()] = float(value.strip())
    #             except ValueError:
    #                 continue
    #     return result

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """验证日志级别"""
        allowed = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in allowed:
            raise ValueError(f"日志级别必须是: {', '.join(allowed)}")
        return v.upper()


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()
