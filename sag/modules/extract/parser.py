"""
实体值类型解析器（EntityValueParser）

功能：将实体名称文本解析为类型化值
支持类型：int（整数）、float（浮点数）、datetime（时间）、bool（布尔）、enum（枚举）、text（文本）

使用示例：
    parser = EntityValueParser()
    result = parser.parse("199元", entity_type="price")
    # result = {
    #     "type": "float",
    #     "raw": "199元",
    #     "value": 199.0,
    #     "unit": "元",
    #     "confidence": 0.95
    # }
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class EntityValueParser:
    """实体值类型解析器"""

    # 中文数字映射
    CN_NUM_MAP = {
        '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
        '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
        '十': 10, '百': 100, '千': 1000, '万': 10000,
        '亿': 100000000, '兆': 1000000000000
    }

    # 单位倍数映射
    UNIT_MULTIPLIER = {
        # 货币单位
        '元': 1, '美元': 1, 'USD': 1, '$': 1,
        '万': 10000, '万元': 10000,
        '亿': 100000000, '亿元': 100000000,
        # 重量单位
        '克': 0.001, 'g': 0.001, 'kg': 1, '公斤': 1, '千克': 1, '吨': 1000,
        # 长度单位
        '米': 1, 'm': 1, '公里': 1000, 'km': 1000, '厘米': 0.01, 'cm': 0.01,
        # 时间单位
        '秒': 1, 's': 1, '分钟': 60, '小时': 3600, '天': 86400,
    }

    # 布尔值映射（移除数字关键词，避免误判）
    BOOL_TRUE = ['是', '对', '真', 'yes', 'true', '已', '有', '启用', '开启']
    BOOL_FALSE = ['否', '错', '假', 'no', 'false', '未', '无', '禁用', '关闭']

    def parse(
        self,
        text: str,
        entity_type: Optional[str] = None,
        entity_type_category: Optional[str] = None,  # 🆕 属性类型类别（time/person/location等）
        value_constraints: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        解析实体值

        Args:
            text: 原始文本（如 "199元", "2024年1月", "已完成"）
            entity_type: 实体类型（可选，如 "创建时间", "价格"）
            entity_type_category: 属性类型类别（可选，如 "time", "person", "location"）
            value_constraints: 值约束（可选，如枚举列表、类型强制）

        Returns:
            解析结果字典 {type, raw, value, unit, confidence} 或 None

        行为模式：
            - 如果 value_constraints 指定了 type，则严格按该类型解析（严格模式）
            - 否则按优先级自动检测类型（兼容模式）
        """
        if not text or not text.strip():
            return None

        text = text.strip()

        # 🆕 严格模式：如果配置了 value_constraints.type，强制按该类型解析
        if value_constraints and 'type' in value_constraints:
            constraint_type = value_constraints['type']
            result = None

            if constraint_type == 'int':
                result = self._parse_number(text, entity_type, value_constraints, force_int=True)
            elif constraint_type == 'float':
                result = self._parse_number(text, entity_type, value_constraints, force_float=True)
            elif constraint_type == 'enum':
                result = self._parse_enum(text, entity_type, value_constraints)
            elif constraint_type == 'datetime':
                # 🆕 严格datetime模式：先尝试紧凑格式，再尝试标准格式
                result = self._parse_compact_datetime(text) or self._parse_datetime(text, entity_type, value_constraints)
            elif constraint_type == 'bool':
                result = self._parse_bool(text, entity_type, value_constraints)
            elif constraint_type == 'text':
                result = self._parse_text(text)

            # 严格模式下，解析失败返回 None（不回退到其他类型）
            if result:
                result["raw"] = text
            return result

        # 兼容模式：按优先级尝试各种类型解析
        # 🆕 判断是否为时间类型属性（优先识别日期格式）
        time_keywords = ['time', 'date', '时间', '日期', 'datetime']
        is_time_type = (
            # 方式1：属性类型类别匹配
            (entity_type_category and entity_type_category.lower() in time_keywords)
            # 方式2：实体类型名称包含时间关键词
            or (entity_type and any(kw in entity_type.lower() for kw in ['时间', '日期', 'time', 'date']))
        )

        # 🆕 仅在时间类型提示时，优先尝试紧凑日期格式（避免误判）
        if is_time_type:
            compact_result = self._parse_compact_datetime(text)
            if compact_result:
                compact_result["raw"] = text
                return compact_result

        # 根据类型调整解析器顺序
        parsers = [
            self._parse_datetime,  # 日期格式（标准格式）
            self._parse_number,    # 数字格式
            self._parse_enum,      # 枚举
            self._parse_bool,      # 布尔
        ]

        for parser in parsers:
            result = parser(text, entity_type, value_constraints)
            if result:
                result["raw"] = text
                return result

        # 默认返回文本类型
        return {
            "type": "text",
            "raw": text,
            "value": text,
            "unit": None,
            "confidence": 1.0
        }

    def _parse_number(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None,
        force_int: bool = False,  # 🆕 强制解析为整数
        force_float: bool = False  # 🆕 强制解析为浮点数
    ) -> Optional[Dict[str, Any]]:
        """
        解析数值类型（int/float）

        支持格式：
        - 纯数字：123, 3.14
        - 带单位：199元, 3.5亿美元, 50kg
        - 科学计数法：1.5e6
        - 中文数字：三十万
        - 🆕 智能单位匹配：配置单位="订单" 时，"七个订单" → 7

        Args:
            force_int: 严格模式 - 必须能解析为整数，否则返回 None
            force_float: 严格模式 - 必须能解析为浮点数
        """
        # 🆕 智能单位匹配：如果配置了单位，尝试匹配 "数字+量词+单位" 模式
        # 例如：配置单位="订单"，文本="七个订单" → 提取7
        configured_unit = _value_constraints.get('unit') if _value_constraints else None
        if configured_unit:
            unit_match_result = self._try_parse_with_unit(text, configured_unit, force_int, force_float)
            if unit_match_result:
                return unit_match_result

        # 模式1：纯数字或带单位
        pattern = r'^([\d,.]+(?:e[+-]?\d+)?)\s*([a-zA-Z\u4e00-\u9fa5]*?)$'
        match = re.match(pattern, text, re.IGNORECASE)

        if match:
            number_str = match.group(1).replace(',', '')
            unit = match.group(2).strip() or None

            try:
                # 🆕 严格整数模式：拒绝浮点数和科学计数法
                if force_int:
                    if '.' in number_str or 'e' in number_str.lower():
                        return None  # 严格拒绝非整数格式
                    num = int(number_str)
                    # 应用单位倍数
                    if unit and unit in self.UNIT_MULTIPLIER:
                        num = num * self.UNIT_MULTIPLIER[unit]
                    return {
                        "type": "int",
                        "value": int(num),
                        "unit": unit,
                        "confidence": 0.95
                    }

                # 🆕 严格浮点模式
                if force_float:
                    if 'e' in number_str.lower():
                        num = float(number_str)
                    elif '.' in number_str:
                        num = float(number_str)
                    else:
                        num = int(number_str)
                    # 应用单位倍数
                    if unit and unit in self.UNIT_MULTIPLIER:
                        num = num * self.UNIT_MULTIPLIER[unit]
                    return {
                        "type": "float",
                        "value": float(num),
                        "unit": unit,
                        "confidence": 0.95
                    }

                # 自动检测模式（原有逻辑）
                # 解析数字
                if 'e' in number_str.lower():
                    num = float(number_str)
                elif '.' in number_str:
                    num = float(number_str)
                else:
                    num = int(number_str)

                # 应用单位倍数
                if unit and unit in self.UNIT_MULTIPLIER:
                    num = num * self.UNIT_MULTIPLIER[unit]

                # 判断最终类型
                if isinstance(num, int):
                    # 原本就是整数
                    value_type = "int"
                    value = num
                elif isinstance(num, float) and num.is_integer():
                    # 浮点数但是整数值（如 100.0）
                    value_type = "int"
                    value = int(num)
                else:
                    # 浮点数
                    value_type = "float"
                    value = float(num)

                return {
                    "type": value_type,
                    "value": value,
                    "unit": unit,
                    "confidence": 0.95
                }
            except ValueError:
                pass

        # 模式2：中文数字（如 "三千万"）
        cn_result = self._parse_chinese_number(text)
        if cn_result:
            # 🆕 严格模式：中文数字需要符合类型要求
            if force_int and cn_result["type"] != "int":
                return None
            if force_float and cn_result["type"] != "float":
                cn_result["type"] = "float"
                cn_result["value"] = float(cn_result["value"])
            return cn_result

        return None

    def _parse_chinese_number(self, text: str) -> Optional[Dict[str, Any]]:
        """
        解析中文数字（保守策略）

        限制：
        - 长度 ≤ 6 个字符（避免误判如"五六个订单"）
        - 必须是纯中文数字字符（不包含其他文字）
        - 仅支持常见格式如 "三千万", "五十", "十二"
        """
        # 🆕 长度限制：超过6个字符的文本不太可能是纯数字
        # 例如："五六个订单"（5个字符）会被拒绝，"三千万"（3个字符）通过
        if len(text) > 6:
            return None

        # 尝试匹配纯中文数字字符
        pattern = r'^([一二三四五六七八九十百千万亿兆]+)$'  # 🆕 添加 ^ 和 $ 确保精确匹配
        match = re.match(pattern, text)

        if not match:
            return None

        cn_text = match.group(1)
        try:
            # 这里使用简化实现，仅处理常见情况
            # 生产环境建议使用 cn2an 库
            value = self._simple_chinese_to_num(cn_text)
            if value is not None:
                return {
                    "type": "int",
                    "value": value,
                    "unit": None,
                    "confidence": 0.85
                }
        except Exception as e:
            logger.debug(f"中文数字解析失败: {text}, error={e}")

        return None

    def _try_parse_with_unit(
        self,
        text: str,
        configured_unit: str,
        force_int: bool = False,
        force_float: bool = False
    ) -> Optional[Dict[str, Any]]:
        """
        智能单位匹配解析

        当配置了单位时，尝试匹配以下模式：
        - "数字+量词+单位"：七个订单 → 7（配置单位=订单）
        - "数字+单位"：7订单 → 7（配置单位=订单）
        - "中文数字+量词+单位"：三个项目 → 3（配置单位=项目）

        Args:
            text: 原始文本
            configured_unit: 配置的单位（如"订单"、"项目"）
            force_int: 强制整数
            force_float: 强制浮点数

        Returns:
            解析结果或 None
        """
        # 常见量词列表（可扩展）
        quantifiers = ['个', '件', '条', '项', '批', '次', '笔', '单', '组']

        # 模式1：数字 + 量词? + 单位
        # 例如："7个订单", "七个订单", "5订单"
        for quantifier in quantifiers + ['']:  # 包括无量词的情况
            # 尝试阿拉伯数字
            pattern = rf'^([\d,.]+(?:e[+-]?\d+)?){quantifier}{re.escape(configured_unit)}$'
            match = re.match(pattern, text, re.IGNORECASE)

            if match:
                number_str = match.group(1).replace(',', '')
                try:
                    # 根据 force 参数决定类型
                    if force_int:
                        if '.' in number_str or 'e' in number_str.lower():
                            continue  # 尝试下一个模式
                        num = int(number_str)
                        return {
                            "type": "int",
                            "value": num,
                            "unit": configured_unit,
                            "confidence": 0.95
                        }
                    elif force_float:
                        num = float(number_str)
                        return {
                            "type": "float",
                            "value": num,
                            "unit": configured_unit,
                            "confidence": 0.95
                        }
                    else:
                        # 自动判断
                        if '.' in number_str or 'e' in number_str.lower():
                            num = float(number_str)
                            return {
                                "type": "float",
                                "value": num,
                                "unit": configured_unit,
                                "confidence": 0.95
                            }
                        else:
                            num = int(number_str)
                            return {
                                "type": "int",
                                "value": num,
                                "unit": configured_unit,
                                "confidence": 0.95
                            }
                except ValueError:
                    continue

            # 尝试中文数字
            cn_pattern = rf'^([一二三四五六七八九十百千万亿兆]+){quantifier}{re.escape(configured_unit)}$'
            cn_match = re.match(cn_pattern, text)

            if cn_match:
                cn_text = cn_match.group(1)
                try:
                    value = self._simple_chinese_to_num(cn_text)
                    if value is not None:
                        # 根据 force 参数和配置决定类型
                        if force_float:
                            return {
                                "type": "float",
                                "value": float(value),
                                "unit": configured_unit,
                                "confidence": 0.90
                            }
                        else:
                            return {
                                "type": "int",
                                "value": value,
                                "unit": configured_unit,
                                "confidence": 0.90
                            }
                except Exception as e:
                    logger.debug(f"中文数字单位匹配失败: {text}, error={e}")
                    continue

        return None

    def _simple_chinese_to_num(self, cn_text: str) -> Optional[int]:
        """简化的中文数字转换（仅支持常见格式）"""
        # 这里仅实现基础转换，复杂情况建议使用 cn2an
        total = 0
        unit = 1

        for char in reversed(cn_text):
            num = self.CN_NUM_MAP.get(char)
            if num is None:
                return None

            if num >= 10:
                if num > unit:
                    unit = num
                else:
                    unit *= num
            else:
                total += num * unit

        return total if total > 0 else None

    def _parse_datetime(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        解析时间类型

        支持格式：
        - ISO 格式：2024-01-15, 2024-01-15 14:30:00
        - 中文格式：2024年1月15日, 1月15日
        - 描述性：今天, 昨天, 上个月
        """
        # 移除常见时间词
        text_clean = text.replace('年', '-').replace('月', '-').replace('日', '')

        # 模式1：ISO 日期 YYYY-MM-DD
        pattern_iso = r'(\d{4})-(\d{1,2})-(\d{1,2})'
        match = re.search(pattern_iso, text_clean)
        if match:
            try:
                dt = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3))
                )
                return {
                    "type": "datetime",
                    "value": dt,
                    "unit": None,
                    "confidence": 0.98
                }
            except ValueError:
                pass

        # 模式2：YYYY-MM（如 "2024-01" 或 "2024年1月"）
        pattern_month = r'(\d{4})-(\d{1,2})(?:[^\d]|$)'
        match = re.search(pattern_month, text_clean)
        if match:
            try:
                dt = datetime(int(match.group(1)), int(match.group(2)), 1)
                return {
                    "type": "datetime",
                    "value": dt,
                    "unit": None,
                    "confidence": 0.90
                }
            except ValueError:
                pass

        # 模式3：仅年份 YYYY
        pattern_year = r'^(\d{4})(?:[^\d]|$)'
        match = re.match(pattern_year, text_clean)
        if match:
            try:
                dt = datetime(int(match.group(1)), 1, 1)
                return {
                    "type": "datetime",
                    "value": dt,
                    "unit": None,
                    "confidence": 0.85
                }
            except ValueError:
                pass

        return None

    def _parse_compact_datetime(self, text: str) -> Optional[Dict[str, Any]]:
        """
        解析紧凑日期格式（仅在有时间类型提示时调用）

        支持格式：
        - YYYYMMDD (8位)：20230117 → 2023-01-17
        - YYYYMM (6位)：202301 → 2023-01-01

        Args:
            text: 原始文本

        Returns:
            解析结果或 None（日期无效时）
        """
        # 模式1：紧凑日期格式 YYYYMMDD（8位）
        # 例如：20230117 → 2023-01-17
        pattern_compact_full = r'^(\d{8})$'
        match = re.match(pattern_compact_full, text)
        if match:
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                day = int(text[6:8])
                dt = datetime(year, month, day)  # 自动验证日期合理性（如20231332会抛出ValueError）
                return {
                    "type": "datetime",
                    "value": dt,
                    "unit": None,
                    "confidence": 0.95
                }
            except ValueError:
                # 日期无效（如20231332），返回 None
                return None

        # 模式2：紧凑月份格式 YYYYMM（6位）
        # 例如：202301 → 2023-01-01
        pattern_compact_month = r'^(\d{6})$'
        match = re.match(pattern_compact_month, text)
        if match:
            try:
                year = int(text[0:4])
                month = int(text[4:6])
                dt = datetime(year, month, 1)  # 月份的第一天
                return {
                    "type": "datetime",
                    "value": dt,
                    "unit": None,
                    "confidence": 0.90
                }
            except ValueError:
                return None

        return None

    def _parse_bool(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        _value_constraints: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        解析布尔类型（保守策略：仅精确匹配短文本）

        支持格式：
        - 中文：是/否, 对/错, 真/假, 有/无, 启用/禁用
        - 英文：yes/no, true/false

        限制：
        - 长度 ≤ 10 个字符（避免误判长文本）
        - 精确匹配（text == keyword，不使用子串包含）
        """
        # 🆕 长度限制：超过10个字符的文本不太可能是布尔值
        if len(text) > 10:
            return None

        text_lower = text.lower().strip()

        # 🆕 精确匹配真值（使用 == 而不是 in）
        if text_lower in self.BOOL_TRUE:
            return {
                "type": "bool",
                "value": True,
                "unit": None,
                "confidence": 0.95
            }

        # 🆕 精确匹配假值
        if text_lower in self.BOOL_FALSE:
            return {
                "type": "bool",
                "value": False,
                "unit": None,
                "confidence": 0.95
            }

        return None

    def _parse_enum(
        self,
        text: str,
        _entity_type: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        解析枚举类型

        依赖 value_constraints 提供的枚举值列表

        行为：
            - 精确匹配：返回枚举值，置信度 1.0
            - 模糊匹配：返回最相似的枚举值，置信度 0.80
            - 无法匹配：返回 "UNKNOWN"，置信度 0.0
        """
        if not value_constraints or 'enum_values' not in value_constraints:
            return None

        enum_values: List[str] = value_constraints['enum_values']

        # 精确匹配
        if text in enum_values:
            return {
                "type": "enum",
                "value": text,
                "unit": None,
                "confidence": 1.0
            }

        # 模糊匹配（包含关系）
        text_lower = text.lower()
        for enum_val in enum_values:
            if enum_val.lower() in text_lower or text_lower in enum_val.lower():
                return {
                    "type": "enum",
                    "value": enum_val,
                    "unit": None,
                    "confidence": 0.80
                }

        # 🆕 匹配失败：返回 UNKNOWN
        return {
            "type": "enum",
            "value": "UNKNOWN",
            "unit": None,
            "confidence": 0.0
        }

    def _parse_text(self, text: str) -> Optional[Dict[str, Any]]:
        """
        解析为纯文本类型

        Args:
            text: 原始文本

        Returns:
            文本类型的解析结果
        """
        return {
            "type": "text",
            "value": text,
            "unit": None,
            "confidence": 1.0
        }

    def parse_to_typed_fields(
        self,
        text: str,
        entity_type: Optional[str] = None,
        entity_type_category: Optional[str] = None,  # 🆕 属性类型类别
        value_constraints: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        解析为类型化字段（直接映射到数据库字段）

        Returns:
            包含类型化字段的字典：
            {
                "value_type": "int",
                "value_raw": "199元",
                "int_value": 199,
                "float_value": None,
                "datetime_value": None,
                "bool_value": None,
                "enum_value": None,
                "value_unit": "元",
                "value_confidence": 0.95
            }
        """
        result = self.parse(text, entity_type, entity_type_category, value_constraints)

        # 如果解析失败，使用 text 类型兜底（确保所有实体都有值）
        if not result:
            result = {
                "type": "text",
                "raw": text or "",
                "value": text or "",
                "unit": None,
                "confidence": 1.0
            }

        # 初始化所有字段为 None
        typed_fields = {
            "value_type": result["type"],
            "value_raw": result["raw"],
            "int_value": None,
            "float_value": None,
            "datetime_value": None,
            "bool_value": None,
            "enum_value": None,
            "value_unit": result.get("unit"),
            "value_confidence": Decimal(str(result.get("confidence", 1.0)))
        }

        # 根据类型填充对应字段
        value = result.get("value")
        if result["type"] == "int":
            typed_fields["int_value"] = value
        elif result["type"] == "float":
            typed_fields["float_value"] = Decimal(str(value))
        elif result["type"] == "datetime":
            typed_fields["datetime_value"] = value
        elif result["type"] == "bool":
            typed_fields["bool_value"] = value
        elif result["type"] == "enum":
            typed_fields["enum_value"] = value

        return typed_fields
