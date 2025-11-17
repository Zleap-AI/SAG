"""
事件向量 Document 模型

对应 Elasticsearch 索引：event_vectors
"""

from elasticsearch_dsl import Date, DenseVector, Document, Keyword, Text


class EventVectorDocument(Document):
    """事件向量文档模型"""

    # 字段定义
    event_id = Keyword(required=True)
    source_config_id = Keyword(required=True)
    source_type = Keyword(required=True)
    source_id = Keyword(required=True)

    # 文本字段（使用默认分词器）
    title = Text(fields={"keyword": Keyword()})
    summary = Text()
    content = Text()

    # 向量字段
    title_vector = DenseVector(dims=1024, index=True, similarity="cosine")
    content_vector = DenseVector(dims=1024, index=True, similarity="cosine")

    # 分类和标签
    category = Keyword()
    tags = Keyword(multi=True)  # 数组字段
    entity_ids = Keyword(multi=True)  # 关联的实体ID列表

    # 时间字段
    start_time = Date()
    end_time = Date()
    created_time = Date()

    class Index:
        """索引配置"""

        name = "event_vectors"
        settings = {"number_of_shards": 12, "number_of_replicas": 1}

    def save(self, **kwargs):
        """保存文档"""
        return super().save(**kwargs)
